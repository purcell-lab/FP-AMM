"""Per-region per-tier backtest for consumer-impact analysis.

Re-runs the FP-AMM with the same parameters as the hero chart, but logs
per-(region, tier) requested and served MW each interval. Output goes to
results/tier_trace_aug2025_{on,off}.parquet.

Window: T=5200 intervals to include the 18 Aug 2025 evening peak (idx 5118).
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from pathlib import Path
import sys

from fpamm import (
    FPParams, FPState, REGIONS, REG_IDX, N, INTERCONNECTORS,
    pooled_feasibility, update_z, synthesise_requests, scarcity_signal,
)


def fair_play_clear_with_tier_log(Q, S, z, params, rng):
    """Same as fpamm.fair_play_clear but also returns per-region-per-tier MW."""
    n_tiers = len(params.w)
    served_rt = np.zeros((N, n_tiers))   # [region, tier]
    requested_rt = np.zeros((N, n_tiers))

    if len(Q) == 0:
        return Q.iloc[0:0].copy(), np.zeros(N), np.zeros(n_tiers), served_rt, requested_rt

    Q = Q.copy()
    region_idx = Q["region_idx"].values
    tier_idx = Q["tier"].values
    powers = Q["power_mw"].values
    # Pre-compute requested per (region, tier)
    for r in range(N):
        for c in range(n_tiers):
            mask = (region_idx == r) & (tier_idx == c)
            requested_rt[r, c] = powers[mask].sum()

    served_by_region = np.zeros(N)
    served_by_tier = np.zeros(n_tiers)
    pool, deliver_cap = S
    pool_residual = float(pool)
    deliver_residual = deliver_cap.copy()
    accepted_mask = np.zeros(len(Q), dtype=bool)

    tier_weight_per_req = params.w[tier_idx]
    Si = tier_weight_per_req * (params.eps_b + z[region_idx]) ** params.alpha_f

    active = np.ones(len(Q), dtype=bool)

    base_tier_probs = params.w / params.w.sum()
    max_iter = len(Q) * 4
    it = 0
    while active.any() and pool_residual > 0 and it < max_iter:
        it += 1
        tier_avail = np.array([(active & (tier_idx == c)).any() for c in range(n_tiers)])
        if not tier_avail.any():
            break
        tier_probs = base_tier_probs * tier_avail
        tier_probs = tier_probs / tier_probs.sum()
        c = rng.choice(n_tiers, p=tier_probs)

        in_tier = active & (tier_idx == c)
        scores = Si[in_tier]
        s_sum = scores.sum()
        if s_sum <= 0:
            probs = np.ones_like(scores) / len(scores)
        else:
            probs = scores / s_sum
        candidates = np.where(in_tier)[0]
        i = rng.choice(candidates, p=probs)

        r = region_idx[i]
        P_i = powers[i]
        if pool_residual >= P_i and deliver_residual[r] >= P_i:
            pool_residual -= P_i
            deliver_residual[r] -= P_i
            served_by_region[r] += P_i
            served_by_tier[c] += P_i
            served_rt[r, c] += P_i
            accepted_mask[i] = True
            active[i] = False
        else:
            active[i] = False

    accepted = Q[accepted_mask].copy()
    return accepted, served_by_region, served_by_tier, served_rt, requested_rt


def run_with_tier_log(df, params, fair_play_on=True):
    rng = np.random.default_rng(params.seed)
    state = FPState.zero(N)
    eff_alpha = params.alpha_f if fair_play_on else 0.0
    eff_params = FPParams(**{**params.__dict__, "alpha_f": eff_alpha})

    intervals = sorted(df["SETTLEMENTDATE"].unique())[: params.T]
    df = df[df["SETTLEMENTDATE"].isin(set(intervals))].copy()
    df_grouped = {t: g for t, g in df.groupby("SETTLEMENTDATE")}

    trace = []
    for ti, t in enumerate(intervals):
        g = df_grouped[t].set_index("REGIONID")
        try:
            g = g.loc[REGIONS]
        except KeyError:
            continue

        demand = g["TOTALDEMAND"].values.astype(float)
        supply = g["AVAILABLEGENERATION"].values.astype(float)
        net_int = g["NETINTERCHANGE"].values.astype(float)

        for wr in eff_params.weak_regions:
            demand[REG_IDX[wr]] *= eff_params.weak_multiplier

        S = pooled_feasibility(demand, supply, net_int)
        delta = demand - supply

        Q = synthesise_requests(ti, demand, eff_params, rng)
        E_des_t = Q.groupby("region_idx")["power_mw"].sum().reindex(range(N), fill_value=0.0).values

        accepted, served_r, served_t, served_rt, requested_rt = fair_play_clear_with_tier_log(
            Q, S, state.z, eff_params, rng
        )

        with np.errstate(divide="ignore", invalid="ignore"):
            ell_t = np.where(E_des_t > 0,
                             np.clip((E_des_t - served_r) / E_des_t, 0.0, 1.0),
                             0.0)
        state.E_del += served_r
        state.E_des += E_des_t
        state.z = update_z(state.z, ell_t, eff_params.beta)

        row = {
            "t": ti, "settlementdate": t,
            "pool_mw": float(S[0]),
            **{f"z_{r}": state.z[i] for i, r in enumerate(REGIONS)},
        }
        for ri, region in enumerate(REGIONS):
            for ci, tier_name in enumerate(["premium", "std", "basic"]):
                row[f"req_{region}_{tier_name}"]  = requested_rt[ri, ci]
                row[f"srv_{region}_{tier_name}"]  = served_rt[ri, ci]
        trace.append(row)

    return state, pd.DataFrame(trace)


if __name__ == "__main__":
    DATA = "data/regionsum_aug2025.parquet"
    df = pd.read_parquet(DATA)

    params = FPParams(
        T=5200, req_scale=1.0,
        block_base_mw=20.0, block_pop_coef=40.0,
        weak_multiplier=1.30,
    )

    OUT = Path("results")
    OUT.mkdir(parents=True, exist_ok=True)

    print("=== Fair Play ON (per-tier logging) ===")
    state_on, tr_on = run_with_tier_log(df, params, fair_play_on=True)
    tr_on.to_parquet(OUT / "tier_trace_aug2025_on.parquet", index=False)
    for i, r in enumerate(REGIONS):
        print(f"  {r}: F = {state_on.F[i]:.4f}")

    print("\n=== Fair Play OFF (per-tier logging) ===")
    state_off, tr_off = run_with_tier_log(df, params, fair_play_on=False)
    tr_off.to_parquet(OUT / "tier_trace_aug2025_off.parquet", index=False)
    for i, r in enumerate(REGIONS):
        print(f"  {r}: F = {state_off.F[i]:.4f}")
    print(f"\nSaved {OUT}")
