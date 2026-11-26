"""Parse the August 2025 DISPATCHREGIONSUM archive into a clean per-interval table.

August 2025 = real NEM winter scarcity (winter peaks, low solar, real interconnector limits).
This replaces the April 2026 + synthetic scarcity approach.
"""
import pandas as pd
import csv
from pathlib import Path

SRC = Path("data/raw/PUBLIC_ARCHIVE#DISPATCHREGIONSUM#FILE01#202508010000.CSV")
OUT = Path("data/regionsum_aug2025.parquet")
OUT.parent.mkdir(parents=True, exist_ok=True)

KEEP = [
    "SETTLEMENTDATE", "REGIONID", "INTERVENTION",
    "TOTALDEMAND",
    "AVAILABLEGENERATION",
    "DISPATCHABLEGENERATION",
    "NETINTERCHANGE",
    "TOTALINTERMITTENTGENERATION",
    "INITIALSUPPLY", "CLEAREDSUPPLY",
]

rows = []
header = None
with open(SRC) as f:
    reader = csv.reader(f)
    for row in reader:
        if not row:
            continue
        if row[0] == "I" and row[2] == "REGIONSUM":
            header = row[4:]
        elif row[0] == "D" and row[2] == "REGIONSUM" and header:
            rec = dict(zip(header, row[4:]))
            rows.append({k: rec.get(k) for k in KEEP})

df = pd.DataFrame(rows)
print(f"Parsed {len(df)} rows")

df["SETTLEMENTDATE"] = pd.to_datetime(df["SETTLEMENTDATE"], format="%Y/%m/%d %H:%M:%S")
for c in KEEP:
    if c not in ("SETTLEMENTDATE", "REGIONID"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

df = df[df["INTERVENTION"] == 0].copy()
df = df.sort_values(["SETTLEMENTDATE", "REGIONID"]).reset_index(drop=True)

df.to_parquet(OUT, index=False)
print(f"Saved {OUT}: {len(df)} rows, {df['SETTLEMENTDATE'].nunique()} intervals")

# Print a quick scarcity sanity check: top 20 hours by NEM-wide reserve ratio (avail/demand)
piv_d = df.pivot(index="SETTLEMENTDATE", columns="REGIONID", values="TOTALDEMAND")
piv_a = df.pivot(index="SETTLEMENTDATE", columns="REGIONID", values="AVAILABLEGENERATION")

nem_demand = piv_d.sum(axis=1)
nem_avail = piv_a.sum(axis=1)
reserve = (nem_avail - nem_demand)
reserve_pct = reserve / nem_demand * 100

print("\n--- Scarcity sanity check ---")
print(f"NEM-wide demand: mean={nem_demand.mean():.0f} MW, max={nem_demand.max():.0f} MW at {nem_demand.idxmax()}")
print(f"NEM-wide reserve %: mean={reserve_pct.mean():.1f}%, min={reserve_pct.min():.1f}% at {reserve_pct.idxmin()}")
print("\nTop 10 tightest intervals (lowest NEM reserve %):")
print(reserve_pct.nsmallest(10).round(1))
