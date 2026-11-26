"""Parse DISPATCHPRICE Aug 2025 -> regional 5-min RRP timeseries."""
import pandas as pd
import csv
from pathlib import Path

SRC = Path("data/raw/PUBLIC_ARCHIVE#DISPATCHPRICE#FILE01#202508010000.CSV")
OUT = Path("data/price_aug2025.parquet")

KEEP = ["SETTLEMENTDATE", "REGIONID", "INTERVENTION", "RRP"]
rows = []
header = None
with open(SRC) as f:
    reader = csv.reader(f)
    for row in reader:
        if not row:
            continue
        if row[0] == "I" and row[2] == "PRICE":
            header = row[4:]
        elif row[0] == "D" and row[2] == "PRICE" and header:
            rec = dict(zip(header, row[4:]))
            rows.append({k: rec.get(k) for k in KEEP})

df = pd.DataFrame(rows)
df["SETTLEMENTDATE"] = pd.to_datetime(df["SETTLEMENTDATE"], format="%Y/%m/%d %H:%M:%S")
df["RRP"] = pd.to_numeric(df["RRP"], errors="coerce")
df["INTERVENTION"] = pd.to_numeric(df["INTERVENTION"], errors="coerce")
df = df[df["INTERVENTION"] == 0].copy()
df = df.sort_values(["SETTLEMENTDATE", "REGIONID"]).reset_index(drop=True)
df.to_parquet(OUT, index=False)
print(f"Saved {OUT}: {len(df)} rows, {df['SETTLEMENTDATE'].nunique()} intervals")
print("Peak window sample (NSW1 18 Aug 17:30-19:00):")
mask = (df["REGIONID"]=="NSW1") & (df["SETTLEMENTDATE"]>="2025-08-18 17:30") & (df["SETTLEMENTDATE"]<="2025-08-18 19:00")
print(df.loc[mask].to_string(index=False))
