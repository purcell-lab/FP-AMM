"""FP-AMM PoC for the NEM at 5-region scope.

Implements:
- Saturated scarcity signal p_t = p_max * tanh(Delta_t / sigma)
- Shared pool and nominal regional allowances; not a six-link network flow solve
- Algorithm 1 two-stage stochastic clearing (tier sample, inverse-fairness sample)
- Saturated-integrator shortage memory: z_{t+1} = clip((1-beta) z_t + beta ell_t)
- 3 service tiers with paper-default weights w = (4, 2, 1)

Reference: Sweeney (2026), arXiv:2606.17217, Algorithm 1 and equations (5)-(11).
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from pathlib import Path

# ----------------------------------------------------------------------------
# Region setup
# ----------------------------------------------------------------------------
REGIONS = ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]
N = len(REGIONS)
REG_IDX = {r: i for i, r in enumerate(REGIONS)}

# Population weights — proxy for active-request volume
# Source: ABS ERP estimates (NSW 8.5M, VIC 7M, QLD 5.7M, SA 1.9M, TAS 0.6M)
# Normalised to NSW = 1.0; we use this to scale per-region request volumes.
POP_WEIGHT = np.array([1.00, 0.67, 0.82, 0.22, 0.07])  # NSW, QLD, VIC, SA, TAS

# Interconnector limits (paper's "feasibility set" U(x_t))
# Each is (from_region, to_region, MW limit forward, MW limit reverse)
# These approximate the typical 5-min dispatch envelopes; in production
# they would come from the AEMO constraint library each interval.
INTERCONNECTORS = [
    # (from, to, fwd_MW, rev_MW)
    ("QLD1", "NSW1", 1300, 1078),   # QNI nominal
    ("NSW1", "VIC1", 1600, 1300),   # VIC-NSW
    ("VIC1", "SA1",  600,  500),    # Heywood
    ("VIC1", "TAS1", 478,  594),    # Basslink (note: TAS export wider in some windows)
    ("NSW1", "QLD1", 600,  600),    # Terranora (limited)
    ("SA1",  "VIC1", 500,  600),    # Murraylink-equivalent (reverse Heywood)
]


# ----------------------------------------------------------------------------
# Mechanism parameters (paper Table I defaults)
# ----------------------------------------------------------------------------
@dataclass
class FPParams:
    # Mechanism
    w: np.ndarray = field(default_factory=lambda: np.array([4.0, 2.0, 1.0]))  # tier weights
    p_max: float = 5.0     # scarcity signal saturation
    sigma: float = 10.0    # scarcity slope (normalised demand units)
    beta: float = 0.08     # shortage-memory step size
    eps_b: float = 0.02    # fairness baseline (prevents starvation of well-served)
    alpha_f: float = 1.5   # fairness exponent (sensitivity to deficits)
    F_star: float = 1.0    # fairness target

    # Backtest
    T: int = 5000          # number of intervals
    seed: int = 42

    # Weak-region multiplier (paper sets weak buses to 1.8x demand)
    # We apply this to TAS1 and SA1 to simulate structural fairness disadvantage —
    # weaker regions in the NEM (small islands or import-constrained) face higher
    # effective demand relative to their pool of locally usable flexibility.
    # Real winter scarcity (Aug 2025) drives the supply side; we no longer use
    # synthetic scarcity_windows or recovery_windows.
    weak_regions: tuple = ("TAS1", "SA1")
    weak_multiplier: float = 1.4   # less aggressive than paper's 1.8 (NEM is smaller)

    # Request scaling: per-region base count multiplier (used in synthesise_requests)
    req_scale: float = 1.0
    # Per-request power block size (MW). Smaller = finer granularity = more
    # intervals clear fully. Default scales by region population.
    block_base_mw: float = 2.0
    block_pop_coef: float = 4.0


# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------
@dataclass
class FPState:
    # Cumulative delivered and desired energy per region
    E_del: np.ndarray         # shape (N,)
    E_des: np.ndarray         # shape (N,)
    # Shortage-memory state z in [0,1]^N
    z: np.ndarray             # shape (N,)
    # Per-tier service log (for Figure 6 reproduction)
    tier_served: list = field(default_factory=list)
    tier_short: list = field(default_factory=list)
    # Interval trace
    history: list = field(default_factory=list)

    @classmethod
    def zero(cls, N: int):
        return cls(
            E_del=np.zeros(N),
            E_des=np.zeros(N),
            z=np.zeros(N),
        )

    @property
    def F(self) -> np.ndarray:
        """Historical fairness ratio per region (paper eq. 3)."""
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(self.E_des > 0, self.E_del / self.E_des, 0.0)


# ----------------------------------------------------------------------------
# Scarcity signal (paper eq. 2)
# ----------------------------------------------------------------------------
def scarcity_signal(delta: np.ndarray, params: FPParams) -> np.ndarray:
    """Bounded Lipschitz scarcity signal: p_t = p_max * tanh(Delta_t / sigma)."""
    return params.p_max * np.tanh(delta / params.sigma)


# ----------------------------------------------------------------------------
# Feasibility set: shared NEM-wide pool with per-region deliverability caps
# ----------------------------------------------------------------------------
def residual_capacity(demand: np.ndarray, supply: np.ndarray,
                      net_interchange: np.ndarray) -> np.ndarray:
    """Local residual flexible capacity per region (pre-interconnector pool).

    S_t,n = max(0, AVAILABLEGENERATION_n + IMPORTS_n - TOTALDEMAND_n)

    Positive imports = energy coming into region (negative net_interchange = importing).
    """
    imports = -net_interchange  # convention: net_interchange > 0 means exporting
    headroom = supply + imports - demand
    return np.maximum(headroom, 0.0)


def pooled_feasibility(demand: np.ndarray, supply: np.ndarray,
                       net_interchange: np.ndarray) -> tuple[float, np.ndarray]:
    """Pooled NEM-wide spare + per-region deliverability cap.

    Returns:
      pool_mw: total NEM-wide flexible capacity (single shared number)
      deliver_cap_n: per-region cap on how much of the pool can be delivered
                    to region n given existing import headroom on interconnectors

    The pool models that mainland regions can support stressed regions through
    the interconnectors. Each region has a deliverability cap = local headroom
    plus residual interconnector import margin (forward limit minus current
    net import). Aggregate of deliverability caps therefore exceeds the pool;
    the pool is the binding NEM-wide constraint.
    """
    imports = -net_interchange
    local = np.maximum(supply + imports - demand, 0.0)
    pool = float(local.sum())

    # Per-region deliverability cap: local headroom + residual import margin
    # via the sum of interconnector forward limits into the region minus current
    # imports. We approximate using a simple lookup against INTERCONNECTORS.
    import_cap = np.zeros(N)
    for (frm, to, fwd, rev) in INTERCONNECTORS:
        # Forward direction: frm -> to. Residual import margin into 'to' on this
        # link ≈ fwd - max(0, imports flowing this direction).
        # We approximate "current flow this direction" using net_interchange of 'frm':
        # if frm is exporting (net_int > 0) some of that lands in 'to'.
        # For simplicity we credit the full forward limit as deliverability
        # margin available IF the link is not already saturated.
        frm_i, to_i = REG_IDX[frm], REG_IDX[to]
        # Headroom in forward direction
        # Use a conservative estimate: half the nameplate limit.
        import_cap[to_i] += 0.5 * fwd

    deliver_cap = local + import_cap
    return pool, deliver_cap


# ----------------------------------------------------------------------------
# Algorithm 1: Stage 1 (tier sample) + Stage 2 (inverse-fairness sample)
# ----------------------------------------------------------------------------
def fair_play_clear(
    Q: pd.DataFrame,           # active requests (one row per request)
    S: np.ndarray,             # residual capacity per region
    z: np.ndarray,             # shortage-memory state
    params: FPParams,
    rng: np.random.Generator,
) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """Run Algorithm 1 for one market interval.

    Returns:
      accepted_idx: DataFrame indices of accepted requests
      served_by_region: MW served per region
      served_by_tier: MW served per tier
    """
    if len(Q) == 0:
        return pd.DataFrame(columns=Q.columns), np.zeros(N), np.zeros(len(params.w))

    Q = Q.copy()
    n_tiers = len(params.w)
    served_by_region = np.zeros(N)
    served_by_tier = np.zeros(n_tiers)
    # S is now (pool_mw_total, deliver_cap_n[N])
    pool, deliver_cap = S
    pool_residual = float(pool)
    deliver_residual = deliver_cap.copy()
    accepted_mask = np.zeros(len(Q), dtype=bool)

    # Stage 2 prep: priority scores S_i,t per request (paper eq. 6)
    region_idx = Q["region_idx"].values
    tier_idx = Q["tier"].values  # 0-indexed
    tier_weight_per_req = params.w[tier_idx]
    Si = tier_weight_per_req * (params.eps_b + z[region_idx]) ** params.alpha_f

    # Active mask: not yet decided + feasible-in-principle
    active = np.ones(len(Q), dtype=bool)

    # Tier-sample with replacement until tier empties; then resample tier from
    # the set of tiers that still have active requests. Avoids wasted draws
    # when a popular tier (per w) is exhausted but other tiers still have load.
    base_tier_probs = params.w / params.w.sum()
    max_iter = len(Q) * 4  # generous safety bound
    it = 0
    while active.any() and pool_residual > 0 and it < max_iter:
        it += 1
        # Re-derive available tiers each pass (a tier may have just emptied)
        tier_avail = np.array([(active & (tier_idx == c)).any() for c in range(n_tiers)])
        if not tier_avail.any():
            break
        # Stage 1: tier sample, renormalised over non-empty tiers
        tier_probs = base_tier_probs * tier_avail
        tier_probs = tier_probs / tier_probs.sum()
        c = rng.choice(n_tiers, p=tier_probs)

        # Stage 2: sample request within tier c
        in_tier = active & (tier_idx == c)
        scores = Si[in_tier]
        # Defensive against all-zero scores (e.g. eps_b=0 and z=0)
        s_sum = scores.sum()
        if s_sum <= 0:
            probs = np.ones_like(scores) / len(scores)
        else:
            probs = scores / s_sum
        candidates = np.where(in_tier)[0]
        i = rng.choice(candidates, p=probs)

        # Feasibility check: pool AND deliverability into region n(i)
        r = region_idx[i]
        P_i = Q["power_mw"].iloc[i]
        if pool_residual >= P_i and deliver_residual[r] >= P_i:
            # Accept
            pool_residual -= P_i
            deliver_residual[r] -= P_i
            served_by_region[r] += P_i
            served_by_tier[c] += P_i
            accepted_mask[i] = True
            active[i] = False
        else:
            # Mark infeasible for this interval; remove from active queue
            active[i] = False

    accepted = Q[accepted_mask].copy()
    return accepted, served_by_region, served_by_tier


# ----------------------------------------------------------------------------
# Shortage-memory update (paper eq. 10)
# ----------------------------------------------------------------------------
def update_z(z: np.ndarray, ell: np.ndarray, beta: float) -> np.ndarray:
    """Saturated-integrator update of the shortage-memory state.

    z_{t+1} = clip_[0,1]((1-beta) * z_t + beta * ell_t)
    where ell_t = (E_des - E_del) / E_des per interval, normalised in [0,1].
    """
    return np.clip((1 - beta) * z + beta * ell, 0.0, 1.0)


# ----------------------------------------------------------------------------
# Request queue synthesis
# ----------------------------------------------------------------------------
def synthesise_requests(
    t: int,
    demand: np.ndarray,
    params: FPParams,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Synthesise a queue of flexibility requests for interval t.

    Volume per region scales with population. Tier mix is 60% basic, 30% std,
    10% premium (rough mapping to AS4755 + future residential tier consultation).
    Power per request is small (typical EV charger / HWS / AC burst).
    """
    # Total requests this interval: ~ 0.5% of demand expressed as kW-equivalent
    # (i.e. ~0.5% of demand is "flexible" at any moment).
    # Per region, scale by population.
    base_count = max(int(100 * getattr(params, "req_scale", 1.0)), 5)
    n_per_region = np.maximum(
        (POP_WEIGHT * base_count).astype(int),  # base count per region
        np.array([5, 5, 5, 5, 5]),               # minimum to avoid empty queues
    )

    block_base = getattr(params, "block_base_mw", 2.0)
    block_coef = getattr(params, "block_pop_coef", 4.0)

    rows = []
    tier_probs = np.array([0.10, 0.30, 0.60])   # premium / std / basic
    for r in range(N):
        n_req = n_per_region[r]
        if n_req == 0:
            continue
        tiers = rng.choice(3, size=n_req, p=tier_probs)
        # Per-request power block (MW). Each block represents an aggregator's
        # parcel of small flexible devices. Bigger regions get larger blocks
        # (consistent with bulk DERMS dispatch). Tunable via block_base_mw.
        block_size = block_base + block_coef * POP_WEIGHT[r]   # MW
        powers = rng.uniform(0.5 * block_size, 1.5 * block_size, size=n_req)
        for j in range(n_req):
            rows.append({
                "region_idx": r,
                "region": REGIONS[r],
                "tier": int(tiers[j]),
                "power_mw": powers[j],
            })

    Q = pd.DataFrame(rows)
    return Q


# ----------------------------------------------------------------------------
# Main backtest loop
# ----------------------------------------------------------------------------
def run_backtest(
    df: pd.DataFrame,       # the parsed regionsum
    params: FPParams,
    fair_play_on: bool = True,
) -> tuple[FPState, pd.DataFrame]:
    """Run the FP-AMM on T intervals.

    Args:
        df: parsed DISPATCHREGIONSUM with columns SETTLEMENTDATE, REGIONID,
            TOTALDEMAND, AVAILABLEGENERATION, NETINTERCHANGE
        params: mechanism parameters
        fair_play_on: if False, set alpha_f to 0 effectively disabling the
                      inverse-fairness weighting (paper's "ON vs OFF" comparison)
    """
    rng = np.random.default_rng(params.seed)
    state = FPState.zero(N)

    # Resolve effective alpha (paper's "Fair Play OFF" = alpha_f = 0)
    eff_alpha = params.alpha_f if fair_play_on else 0.0
    eff_params = FPParams(**{**params.__dict__, "alpha_f": eff_alpha})

    # Build interval index aligned to settlement times
    intervals = sorted(df["SETTLEMENTDATE"].unique())[: params.T]
    intervals_set = set(intervals)
    df = df[df["SETTLEMENTDATE"].isin(intervals_set)].copy()
    df_grouped = {t: g for t, g in df.groupby("SETTLEMENTDATE")}

    trace = []

    for ti, t in enumerate(intervals):
        g = df_grouped[t].set_index("REGIONID")
        # Reorder rows to REGIONS canonical order
        try:
            g = g.loc[REGIONS]
        except KeyError:
            # Skip intervals missing a region (rare data gaps)
            continue

        demand = g["TOTALDEMAND"].values.astype(float)
        supply = g["AVAILABLEGENERATION"].values.astype(float)
        net_int = g["NETINTERCHANGE"].values.astype(float)

        # Apply weak-region multiplier (structural fairness disadvantage)
        for wr in eff_params.weak_regions:
            demand[REG_IDX[wr]] *= eff_params.weak_multiplier

        # No synthetic scarcity: real Aug 2025 supply/demand drives the signal.
        sup_mult = 1.0

        # Pooled residual capacity (after essential-load allocation; we treat
        # TOTALDEMAND as the essential floor). Returns (pool_total, per_region_deliverability_cap).
        S = pooled_feasibility(demand, supply, net_int)

        # Scarcity signal (informational, used in trace)
        delta = demand - supply
        p_signal = scarcity_signal(delta, eff_params)

        # Synthesise this interval's request queue
        Q = synthesise_requests(ti, demand, eff_params, rng)

        # Total requested energy per region (= desired E for this interval)
        E_des_t = Q.groupby("region_idx")["power_mw"].sum().reindex(range(N), fill_value=0.0).values

        # Run FP-AMM clearing
        accepted, served_by_region, served_by_tier = fair_play_clear(
            Q, S, state.z, eff_params, rng
        )

        # Shortage per region this interval: ell_n,t in [0,1] normalised
        with np.errstate(divide="ignore", invalid="ignore"):
            ell_t = np.where(
                E_des_t > 0,
                np.clip((E_des_t - served_by_region) / E_des_t, 0.0, 1.0),
                0.0,
            )

        # Update cumulative state
        state.E_del += served_by_region
        state.E_des += E_des_t
        state.z = update_z(state.z, ell_t, eff_params.beta)

        # Log tier-level service
        served_essential_by_tier = np.zeros(len(eff_params.w))  # placeholder
        # Tier shortfall = requested - served per tier
        tier_requested = Q.groupby("tier")["power_mw"].sum().reindex(range(len(eff_params.w)), fill_value=0.0).values
        tier_shortfall = np.maximum(tier_requested - served_by_tier, 0.0)
        state.tier_served.append(served_by_tier.copy())
        state.tier_short.append(tier_shortfall.copy())

        # Trace
        pool_total, deliver_cap = S
        trace.append({
            "t": ti,
            "settlementdate": t,
            **{f"F_{r}": state.F[i] for i, r in enumerate(REGIONS)},
            **{f"z_{r}": state.z[i] for i, r in enumerate(REGIONS)},
            **{f"served_{r}": served_by_region[i] for i, r in enumerate(REGIONS)},
            **{f"req_{r}": E_des_t[i] for i, r in enumerate(REGIONS)},
            **{f"deliver_{r}": deliver_cap[i] for i, r in enumerate(REGIONS)},
            "pool_mw": pool_total,
            "p_signal_max": p_signal.max(),
            "supply_mult": sup_mult,
            "max_fairness_err": (eff_params.F_star - state.F).max(),
        })

    trace_df = pd.DataFrame(trace)
    return state, trace_df


if __name__ == "__main__":
    import sys
    DATA = "data/regionsum_aug2025.parquet"
    df = pd.read_parquet(DATA)
    print(f"Loaded {len(df)} rows, {df['SETTLEMENTDATE'].nunique()} intervals from {DATA}")

    # Pooled-feasibility regime: request volume sized so peaks bind on the
    # shared NEM pool but off-peak clears. ~6 GW total request volume bites
    # the Aug 18 18:35 peak (where NEM-wide spare drops to ~5 GW).
    params = FPParams(
        T=5000, req_scale=1.0,
        block_base_mw=20.0, block_pop_coef=40.0,
        weak_multiplier=1.30,
    )

    print("\n=== Fair Play ON ===")
    state_on, trace_on = run_backtest(df, params, fair_play_on=True)
    for i, r in enumerate(REGIONS):
        print(f"  {r}: F = {state_on.F[i]:.4f}, z = {state_on.z[i]:.4f}")
    print(f"  max |F* - F_n|: {(params.F_star - state_on.F).max():.4f}")
    print(f"  min_n F_n:      {state_on.F.min():.4f}")

    print("\n=== Fair Play OFF ===")
    state_off, trace_off = run_backtest(df, params, fair_play_on=False)
    for i, r in enumerate(REGIONS):
        print(f"  {r}: F = {state_off.F[i]:.4f}, z = {state_off.z[i]:.4f}")
    print(f"  max |F* - F_n|: {(params.F_star - state_off.F).max():.4f}")
    print(f"  min_n F_n:      {state_off.F.min():.4f}")

    # Save traces
    OUT = Path("results")
    OUT.mkdir(parents=True, exist_ok=True)
    trace_on.to_parquet(OUT / "trace_aug2025_on.parquet", index=False)
    trace_off.to_parquet(OUT / "trace_aug2025_off.parquet", index=False)
    print(f"\nSaved traces to {OUT}")
