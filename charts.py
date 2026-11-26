"""Rebuild four figures from the released traces. Run from the repository root."""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as dates
import numpy as np
import pandas as pd

out = Path("results")
regions = ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]
tiers = ["premium", "std", "basic"]
labels = ["Premium", "Standard", "Basic"]
window = ["2025-08-18 17:30", "2025-08-18 21:30"]
plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.alpha": .18, "font.size": 10})


def trace(kind, mode):
    return pd.read_parquet(out / f"{kind}_aug2025_{mode}.parquet").set_index("settlementdate")


def save(fig, name):
    fig.savefig(out / f"fpamm_{name}.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


a, b = (trace("trace", m) for m in ("on", "off"))
fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True, layout="constrained")
for reg in regions:
    axes[0].plot(a.index, a[f"F_{reg}"] * 100, label=reg)
axes[0].legend(ncol=5)
axes[0].set_ylabel("Cumulative service (%)")
for df, name in [(a, "Fair Play on"), (b, "Fair Play off")]:
    axes[1].plot(df.index, 100 * df.max_fairness_err, label=name)
axes[1].set_ylabel("Worst regional shortfall (%)")
axes[1].legend()
axes[1].xaxis.set_major_formatter(dates.DateFormatter("%d %b"))
fig.suptitle("Five-region simulation | first 5,000 intervals of August 2025")
save(fig, "aug2025_hero")

peak = {m: trace("tier_trace", m).loc[slice(*window)] for m in ("on", "off")}
fig, axes = plt.subplots(1, 3, figsize=(12, 4), sharey=True, layout="constrained")
for ax, tier, label in zip(axes, tiers, labels):
    for offset, (mode, df) in zip([-.18, .18], peak.items()):
        rates = [100 * df[f"srv_{r}_{tier}"].sum() / df[f"req_{r}_{tier}"].sum() for r in regions]
        ax.bar(np.arange(5) + offset, rates, .36, label=mode.upper())
    ax.set(title=label, xticks=np.arange(5), xticklabels=regions, ylim=(0, 105))
    ax.tick_params(axis="x", rotation=35)
axes[0].set_ylabel("Modelled requests served (%)")
axes[-1].legend()
fig.suptitle("18 August evening peak | five-region service by tier")
save(fig, "aug2025_consumer_hero")

fig, (ax, bx) = plt.subplots(2, 1, figsize=(10, 6), sharex=True, layout="constrained")
prices = pd.read_parquet("data/price_aug2025.parquet").pivot(index="SETTLEMENTDATE", columns="REGIONID", values="RRP").loc[slice(*window)]
df = peak["on"]
for tier, label in zip(tiers, labels):
    srv = df[[f"srv_{r}_{tier}" for r in regions]].sum(axis=1)
    req = df[[f"req_{r}_{tier}" for r in regions]].sum(axis=1)
    bx.plot(df.index.to_pydatetime(), 100 * srv / req, label=label)
bx.set_ylabel("Modelled service (%)")
bx.legend(ncol=3)
# Use native Matplotlib date axes on both panels.
for reg in regions:
    ax.plot(prices.index.to_pydatetime(), prices[reg], label=reg)
ax.set_ylabel("Observed RRP ($/MWh)")
ax.legend(ncol=5)
bx.xaxis.set_major_formatter(dates.DateFormatter("%H:%M"))
fig.suptitle("Wholesale price and discretionary allocation | different outcomes, not alternatives")
save(fig, "aug2025_two_scenario")

s = pd.read_csv(out / "sydney_zone_state_log.csv", parse_dates=["time"]).set_index("time")
fig, axes = plt.subplots(3, 2, figsize=(12, 8), sharex=True, layout="constrained")
def panel(ax, title, series):
    for label, v in series.items():
        ax.plot(s.index, v, label=label)
    ax.set_title(title, loc="left")
    if len(series) > 1:
        ax.legend(fontsize=8)
    ax.xaxis.set_major_formatter(dates.DateFormatter("%H:%M"))
panel(axes[0,0], "NSW1 observed RRP ($/MWh)", {"RRP": s.rrp})
panel(axes[0,1], "Assumed zone envelope (MW)", {"Raw": s.zone_envelope_mw, "Discretionary": s.flex_envelope_mw})
panel(axes[1,0], "Synthetic queue (MW)", {"Requested": s.req_total_mw, "Served": s.srv_total_mw})
panel(axes[1,1], "Tier service per interval (%)", {i:100*s[f"srv_{t}_mw"]/s[f"req_{t}_mw"] for i,t in zip(labels,["premium","standard","basic"])})
panel(axes[2,0], "Discounted unmet MW per interval (model state)", {"Memory": s.z_out})
panel(axes[2,1], "Cumulative service (%)", {"Service": 100*s.F_running})
fig.suptitle("Sydney zone proxy | 18 August 2025 | synthetic allocation, not measured curtailment")
save(fig, "sydney_zone_diagnostic")
