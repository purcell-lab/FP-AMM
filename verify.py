"""Validate the release traces and compare a seeded replay against each prefix."""
import numpy as np
import pandas as pd
from fpamm import FPParams, REGIONS, run_backtest
from tier_backtest import run_with_tier_log

x = pd.read_parquet("data/regionsum_aug2025.parquet")
params = FPParams(T=12, req_scale=1, block_base_mw=20., block_pop_coef=40., weak_multiplier=1.3)
for name, fn, n in [("trace", run_backtest, 5000), ("tier_trace", run_with_tier_log, 5200)]:
    for mode in ("on", "off"):
        saved = pd.read_parquet(f"results/{name}_aug2025_{mode}.parquet")
        assert len(saved) == n and saved.settlementdate.is_unique
        _, replay = fn(x, params, mode == "on")
        pd.testing.assert_frame_equal(replay, saved.head(params.T), check_dtype=False,
                                      check_exact=False, atol=1e-7, rtol=1e-8)
        if name == "trace":
            for r in REGIONS:
                assert (saved[f"served_{r}"] <= saved[f"deliver_{r}"] + 1e-6).all()
                np.testing.assert_allclose(saved[f"F_{r}"], saved[f"served_{r}"].cumsum() / saved[f"req_{r}"].cumsum())
            assert (saved.filter(regex="^served_").sum(axis=1) <= saved.pool_mw + 1e-6).all()
s = pd.read_csv("results/sydney_zone_state_log.csv")
assert len(s) == 49 and s.time.is_unique
assert (s.srv_total_mw <= s.flex_envelope_mw + 1e-6).all()
np.testing.assert_allclose(s.z_out, .92*s.z_in + s.delta_short)
np.testing.assert_allclose(s.F_running, s.srv_total_mw.cumsum()/s.req_total_mw.cumsum())
assert (s.srv_total_mw.sum(), s.req_total_mw.sum()) == (2215, 34275)
print("PASS: four regional prefixes, regional capacity and service checks, Sydney state and totals.")
