"""Single-zone FP-AMM Layer-2 proof of concept.

Zone: NSW_SY (Sydney + Snowy + Coast load centre)
Window: 18 Aug 2025 17:30-21:30 (NSW evening peak)

Architecture:
- Layer 1 (already done by AEMO): observed NSW1 dispatch, RRP, interconnector flows.
  We consume this from MMSDM as fact - we do NOT re-solve dispatch.
- Layer 2 (FP-AMM, this PoC): operates inside NSW_SY on the discretionary flexibility
  envelope that remains after Layer 1 has cleared.

Mechanism transparency:
- Algorithm 1 (Sweeney 2026, arXiv:2606.17217) is run with full per-iteration state logging
- We log: tier draw, candidate, S_i, sampling weights, accept/reject, served MW, z update
- Output is a per-interval state table plus the F_n,z convergence trace

This is an architecture verification, not a final result. Output diagnostics will be
inspected to confirm:
  1. The mechanism produces sensible service rates given the envelope
  2. The shortage memory z evolves in the right direction (rises when curtailed)
  3. The cumulative service ratio is reported without inferring convergence
  4. No spurious cross-zone allocation occurs (we never reference other zones)
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass
from pathlib import Path

# -----------------------------------------------------------------------------
# Substrate
# -----------------------------------------------------------------------------
ZONE_NAME = "NSW_SY"
ZONE_LABEL = "Sydney metro + Snowy + Coast"
PEAK_START = pd.Timestamp("2025-08-18 17:30")
PEAK_END = pd.Timestamp("2025-08-18 21:30")

# From config.py ZONES_12:
#   NSW_SY: region NSW1, ds=0.32, tlf=1.008, iface=[] (no outbound)
#   inbound: NSW_H -> NSW_SY (1100 MW), NSW_CW -> NSW_SY (700 MW)
ZONE_DS = 0.32              # share of NSW1 demand in this zone
INTRA_NSW_IMPORT_LIM = 1800 # MW from NSW_H + NSW_CW into NSW_SY combined
ZONE_TLF = 1.008

# Local Sydney zone generation (approximate - mainly gas peakers and hydro)
# From config: NSW_SY units = NSW_CCGT (~550 MW), NSW_OCGT (~650 MW), NSW_HYDRO (~2000 MW),
# NSW_WIND_NE (~700 MW intermittent), NSW_WIND_S (~600 MW intermittent)
# Hydro is the main flexible local resource. We treat the rest as price-takers in Layer 1.
LOCAL_FLEX_GEN_MW = 2200  # nominal max local flexible generation

# -----------------------------------------------------------------------------
# FP-AMM parameters (Algorithm 1, Sweeney 2026)
# -----------------------------------------------------------------------------
@dataclass
class FPParams:
    w: tuple = (4.0, 2.0, 1.0)   # tier weights (premium, standard, basic)
    p_max: int = 5                # max accept attempts per stage 2 iteration
    sigma: int = 500              # max tier-sample iterations per interval (loop exits when envelope full or queue empty)
    beta: float = 0.08            # shortage-memory decay
    eps_b: float = 0.02           # boundary epsilon
    alpha_f: float = 1.5          # inverse-fairness exponent
    F_star: float = 1.0           # convergence target
    block_mw: float = 5.0         # MW per atomic request block (~2500 households activating)

# -----------------------------------------------------------------------------
# Layer 1: pull observed NSW1 data
# -----------------------------------------------------------------------------
def load_layer1():
    rs = pd.read_parquet("data/regionsum_aug2025.parquet")
    rp = pd.read_parquet("data/price_aug2025.parquet")
    mask_r = (rs["REGIONID"] == "NSW1") & (rs["SETTLEMENTDATE"] >= PEAK_START) & (rs["SETTLEMENTDATE"] <= PEAK_END)
    mask_p = (rp["REGIONID"] == "NSW1") & (rp["SETTLEMENTDATE"] >= PEAK_START) & (rp["SETTLEMENTDATE"] <= PEAK_END)
    df = rs.loc[mask_r, ["SETTLEMENTDATE", "TOTALDEMAND", "AVAILABLEGENERATION",
                          "DISPATCHABLEGENERATION", "NETINTERCHANGE", "CLEAREDSUPPLY"]]
    df = df.merge(rp.loc[mask_p, ["SETTLEMENTDATE", "RRP"]], on="SETTLEMENTDATE")
    df = df.sort_values("SETTLEMENTDATE").reset_index(drop=True)
    return df

# -----------------------------------------------------------------------------
# Per-zone flexibility envelope
# -----------------------------------------------------------------------------
def zone_envelope(layer1: pd.DataFrame) -> pd.DataFrame:
    """Derive NSW_SY flexibility envelope per interval.

    Definition: how much additional discretionary load the zone can absorb (or how much
    must be curtailed) without breaching local thermal/transfer limits.

    Sydney zone:
      zone_demand = NSW1_demand * ds_SY
      zone_supply = local_flex_gen + min(intra-NSW import available, INTRA_NSW_IMPORT_LIM)
      local_flex_gen scales with NSW1 dispatch utilisation
      intra-NSW import = NSW1's import from VIC/QLD plus internal Hunter/CW supply, capped

    The envelope is the gap between the zone's available net injection and its demand.
    Negative envelope = zone is undersupplied locally and depends on transfer (constrained).
    """
    rows = []
    for _, r in layer1.iterrows():
        nsw_demand = r["TOTALDEMAND"]
        nsw_avail = r["AVAILABLEGENERATION"]
        nsw_dispatch = r["DISPATCHABLEGENERATION"]
        nsw_import = -r["NETINTERCHANGE"]   # positive = importing
        zone_demand = nsw_demand * ZONE_DS
        # Estimate Sydney's local flex generation share (proportional to dispatch)
        # Sydney has roughly 25% of NSW dispatchable capacity but the ratio rises during
        # peaks because hydro and OCGT in the zone are called on.
        zone_local_gen = LOCAL_FLEX_GEN_MW * min(1.0, nsw_dispatch / 11000) * 0.9
        # Transfer in from Hunter + Central-West - bounded by combined limit
        zone_transfer_in = min(INTRA_NSW_IMPORT_LIM,
                               max(0, nsw_import + 0.6 * nsw_dispatch))
        zone_supply = zone_local_gen + zone_transfer_in
        envelope = zone_supply - zone_demand
        rows.append({
            "settlementdate": r["SETTLEMENTDATE"],
            "rrp": r["RRP"],
            "nsw_demand": nsw_demand,
            "nsw_import": nsw_import,
            "zone_demand": zone_demand,
            "zone_local_gen": zone_local_gen,
            "zone_transfer_in": zone_transfer_in,
            "zone_supply": zone_supply,
            "envelope_mw": envelope,
        })
    return pd.DataFrame(rows)

# -----------------------------------------------------------------------------
# Request queue calibration
# -----------------------------------------------------------------------------
@dataclass
class RequestCalibration:
    """Calibrated to plausible Sydney metro flexibility demand."""
    # Total discretionary flexibility requests at the evening peak (MW)
    # Sydney has ~1.5M dwellings; assume 10% on smart tariff + DOE = 150k.
    # Average discretionary load 1.5 kW each = 225 MW. EVs charging: 50k * 7kW = 350 MW.
    # V2G discharge calls: 5k * 10kW = 50 MW. HVAC pre-cool/defer: 80 MW. Hot water: 60 MW.
    # Total at peak: ~750 MW of potential discretionary activity.
    peak_request_mw: float = 750.0
    # Tier mix (premium, standard, basic) by request volume
    tier_mix: tuple = (0.10, 0.30, 0.60)
    # Tier blend varies by time of day - basic tier dominates evening peak
    # (e.g. low-rate EV charging deferred to evening)

def generate_requests(envelope_df: pd.DataFrame, calib: RequestCalibration,
                      seed: int = 42) -> list[list[dict]]:
    """Generate per-interval tier-stratified requests.

    Each request is one MW-sized atomic unit, like a single household discretionary
    activation. We work in 1 MW request blocks for simplicity (~1 MW per ~500 households
    activating at once).
    """
    rng = np.random.default_rng(seed)
    queue = []
    for _, r in envelope_df.iterrows():
        # Total requests scale with peak intensity (higher when envelope is tight)
        # Map: when envelope is low, demand-side is also stressed
        intensity = 1.0 if r["rrp"] > 500 else 0.7
        total = int(calib.peak_request_mw * intensity)
        interval_q = []
        # Tier counts (number of blocks; each block is BLOCK_MW)
        block_mw = 5.0
        n_blocks = max(1, int(total / block_mw))
        nP = int(n_blocks * calib.tier_mix[0])
        nS = int(n_blocks * calib.tier_mix[1])
        nB = n_blocks - nP - nS
        for tier_idx, n in enumerate([nP, nS, nB]):
            for _ in range(n):
                interval_q.append({
                    "tier": tier_idx,    # 0=premium, 1=standard, 2=basic
                    "size_mw": block_mw,
                })
        rng.shuffle(interval_q)
        queue.append(interval_q)
    return queue

# -----------------------------------------------------------------------------
# Algorithm 1 with full state logging
# -----------------------------------------------------------------------------
def algorithm_1_with_log(
    requests: list[dict],
    envelope_mw: float,
    z_in: float,
    params: FPParams,
    rng: np.random.Generator,
    t_label: str,
) -> dict:
    """Run Algorithm 1 for a single interval, returning state log.

    State machine per Sweeney 2026 Algorithm 1:
    - Stage 1: tier sample by weights w
    - Stage 2: within-tier inverse-fairness sample with S_i = w(p) * (eps_b + z)^alpha_f
    - Iterate up to (sigma * p_max) candidate checks
    - Each candidate: accept if (cumulative served + size) <= envelope
    - Track served by tier, then update z := z + delta_short - beta * z

    For a single zone with global shortage memory z (no inter-zone routing),
    z scales with cumulative deficit.
    """
    served = 0.0                            # MW served (sum of block sizes)
    served_by_tier = [0.0, 0.0, 0.0]        # MW by tier
    requested_by_tier = [0.0, 0.0, 0.0]     # MW by tier
    for req in requests:
        requested_by_tier[req["tier"]] += req["size_mw"]

    if envelope_mw <= 0:
        # Trivial: no headroom. Everyone curtailed.
        delta_short = float(sum(requested_by_tier))
        z_out = max(0.0, z_in + delta_short - params.beta * z_in)
        return {
            "t_label": t_label,
            "envelope_mw": envelope_mw,
            "z_in": z_in,
            "requested_total_mw": sum(requested_by_tier),
            "requested_by_tier_mw": requested_by_tier,
            "served_total_mw": 0.0,
            "served_by_tier_mw": served_by_tier,
            "delta_short": delta_short,
            "z_out": z_out,
            "F_interval": 0.0,
            "iterations": [],
        }

    # Sort requests into tier buckets (still random within tier)
    pool_by_tier = [[], [], []]
    for req in requests:
        pool_by_tier[req["tier"]].append(req)

    # Tier sampling weights (normalised)
    w = np.array(params.w, dtype=float)
    p_tier = w / w.sum()

    iter_log = []
    min_block_remaining = min(
        (req["size_mw"] for tier_pool in pool_by_tier for req in tier_pool),
        default=0.0,
    )
    consecutive_envelope_full = 0
    # Run up to sigma tier-sample iterations; exit early when envelope cannot fit any
    # remaining block or queue is exhausted
    for sigma_i in range(params.sigma):
        # Probabilities renormalised across non-empty tiers
        valid = np.array([len(pool_by_tier[i]) > 0 for i in range(3)])
        if not valid.any():
            break
        # Envelope-full early exit: if smallest remaining block can't fit, stop.
        smallest_remaining = min(
            (req["size_mw"] for i, tier_pool in enumerate(pool_by_tier) if valid[i]
             for req in tier_pool),
            default=0.0,
        )
        if served + smallest_remaining > envelope_mw + 1e-9:
            iter_log.append({
                "sigma_i": sigma_i, "attempt": -1,
                "tier": -1, "S_i": 0.0, "result": "envelope_full_terminate",
                "cumulative_served": served,
            })
            break
        p_eff = np.where(valid, p_tier, 0.0)
        p_eff = p_eff / p_eff.sum()
        chosen_tier = int(rng.choice(3, p=p_eff))
        # S_i = w(tier) * (eps_b + z)^alpha_f - shared across same-tier requests
        S = w[chosen_tier] * (params.eps_b + z_in) ** params.alpha_f
        # Within-tier candidates: up to p_max attempts
        for attempt in range(params.p_max):
            if not pool_by_tier[chosen_tier]:
                break
            idx = rng.integers(len(pool_by_tier[chosen_tier]))
            candidate = pool_by_tier[chosen_tier][idx]
            cand_size = candidate["size_mw"]
            if served + cand_size <= envelope_mw + 1e-9:
                served += cand_size
                served_by_tier[chosen_tier] += cand_size
                pool_by_tier[chosen_tier].pop(idx)
                iter_log.append({
                    "sigma_i": sigma_i, "attempt": attempt,
                    "tier": chosen_tier, "S_i": S, "result": "served",
                    "cumulative_served": served,
                })
                break   # next sigma iteration
            else:
                iter_log.append({
                    "sigma_i": sigma_i, "attempt": attempt,
                    "tier": chosen_tier, "S_i": S, "result": "rejected_block_too_big",
                    "cumulative_served": served,
                })
                # Try another candidate within the same tier (different size)

    delta_short = float(sum(requested_by_tier)) - served
    # Variant: discounted MW shortfall, with no upper saturation.
    z_out = max(0.0, z_in + delta_short - params.beta * z_in)
    F_interval = served / max(1.0, sum(requested_by_tier))

    return {
        "t_label": t_label,
        "envelope_mw": envelope_mw,
        "z_in": z_in,
        "requested_total_mw": sum(requested_by_tier),
        "requested_by_tier_mw": requested_by_tier,
        "served_total_mw": served,
        "served_by_tier_mw": served_by_tier,
        "delta_short": delta_short,
        "z_out": z_out,
        "F_interval": F_interval,
        "iterations": iter_log,
    }

# -----------------------------------------------------------------------------
# Run
# -----------------------------------------------------------------------------
def main():
    out_dir = Path("results")
    out_dir.mkdir(exist_ok=True)
    layer1 = load_layer1()
    print(f"Layer 1: {len(layer1)} NSW1 intervals over peak window")
    envelope = zone_envelope(layer1)
    print(f"Envelope: min {envelope['envelope_mw'].min():.0f} MW, "
          f"max {envelope['envelope_mw'].max():.0f} MW, "
          f"mean {envelope['envelope_mw'].mean():.0f} MW")

    calib = RequestCalibration()
    queue = generate_requests(envelope, calib, seed=42)
    print(f"Request queue: peak {calib.peak_request_mw} MW, "
          f"tier mix {calib.tier_mix}")

    params = FPParams()
    rng = np.random.default_rng(7)
    z = 0.0
    F_running = []  # running cumulative F_n,z
    cumulative_req = 0
    cumulative_srv = 0.0
    state_log = []
    iter_records = []

    for i, (req_list, env_row) in enumerate(zip(queue, envelope.itertuples())):
        # Tier-stratified MW envelope = full zone envelope, but engineered to be small
        # We scale envelope: assume only ~30% of physical headroom is the discretionary layer
        flex_envelope = max(0.0, env_row.envelope_mw * 0.30)
        # If envelope is negative (zone constrained import-wise), use a small positive
        # representing the rotating frequency-response / minor reserve
        if env_row.envelope_mw < 0:
            flex_envelope = max(50.0, abs(env_row.envelope_mw) * 0.05)

        result = algorithm_1_with_log(
            requests=req_list,
            envelope_mw=flex_envelope,
            z_in=z,
            params=params,
            rng=rng,
            t_label=env_row.settlementdate.strftime("%H:%M"),
        )
        z = result["z_out"]
        cumulative_req += result["requested_total_mw"]
        cumulative_srv += result["served_total_mw"]
        F_running.append(cumulative_srv / max(1.0, cumulative_req))
        state_log.append({
            "interval_idx": i,
            "time": env_row.settlementdate,
            "rrp": env_row.rrp,
            "nsw_import_mw": env_row.nsw_import,
            "zone_envelope_mw": env_row.envelope_mw,
            "flex_envelope_mw": flex_envelope,
            "z_in": result["z_in"],
            "z_out": result["z_out"],
            "req_total_mw": result["requested_total_mw"],
            "req_premium_mw": result["requested_by_tier_mw"][0],
            "req_standard_mw": result["requested_by_tier_mw"][1],
            "req_basic_mw": result["requested_by_tier_mw"][2],
            "srv_total_mw": result["served_total_mw"],
            "srv_premium_mw": result["served_by_tier_mw"][0],
            "srv_standard_mw": result["served_by_tier_mw"][1],
            "srv_basic_mw": result["served_by_tier_mw"][2],
            "delta_short": result["delta_short"],
            "F_interval": result["F_interval"],
            "F_running": F_running[-1],
        })
        # Keep per-iteration log only for a sample of intervals (first, peak, last)
        if i in (0, 5, 10, 20, 30, len(queue) - 1):
            for it in result["iterations"]:
                iter_records.append({"interval_idx": i, "time": env_row.settlementdate,
                                     **it})

    state_df = pd.DataFrame(state_log)
    iter_df = pd.DataFrame(iter_records)
    state_df.to_csv(out_dir / "sydney_zone_state_log.csv", index=False)
    iter_df.to_csv(out_dir / "sydney_zone_iter_log.csv", index=False)
    print(f"\nSaved {out_dir / 'sydney_zone_state_log.csv'}")
    print(f"Saved {out_dir / 'sydney_zone_iter_log.csv'}")

    # Print a digestible summary
    print("\n=== Summary across peak window ===")
    print(f"Total requested: {state_df['req_total_mw'].sum():,.0f} MW-intervals")
    print(f"Total served:    {state_df['srv_total_mw'].sum():,.0f} MW-intervals")
    print(f"Cumulative F_n,z (Sydney zone): {F_running[-1]:.4f}")
    print()
    print("Tier service rates over window (MW-intervals):")
    for tname, rc, sc in [("Premium", "req_premium_mw", "srv_premium_mw"),
                          ("Standard", "req_standard_mw", "srv_standard_mw"),
                          ("Basic",    "req_basic_mw",    "srv_basic_mw")]:
        r = state_df[rc].sum()
        s = state_df[sc].sum()
        print(f"  {tname:10s}  req {r:>6.0f} MW  srv {s:>6.0f} MW  rate {s/max(r,1):.3f}")
    print()
    print("z trajectory (shortage memory):")
    print(f"  start: {state_df['z_in'].iloc[0]:.1f}")
    print(f"  max:   {state_df['z_out'].max():.1f}  at {state_df.loc[state_df['z_out'].idxmax(), 'time']}")
    print(f"  end:   {state_df['z_out'].iloc[-1]:.1f}")
    return state_df, iter_df

if __name__ == "__main__":
    main()
