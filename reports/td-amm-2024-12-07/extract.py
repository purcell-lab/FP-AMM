"""Extract two case days. Usage: python extract.py /path/to/cache (see README)."""
from pathlib import Path
import sys, hashlib, json, zipfile
import pandas as pd
import numpy as np
import requests

OUT = Path(__file__).resolve().parent
BASE = "https://nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/2024/MMSDM_2024_12/MMSDM_Historical_Data_SQLLoader/DATA/"
URL = {n: BASE + "PUBLIC_ARCHIVE%23"+n+"%23FILE01%23202412010000.zip" for n in
       ["DISPATCHPRICE","DISPATCH_LOCAL_PRICE","DISPATCHINTERCONNECTORRES"]}
URL.update(energex="https://www.energex.com.au/__data/assets/file/0010/1728325/Energex-Network-Substation-Load-Data-2024-25.zip",
           essential="https://www.essentialenergy.com.au/ext/schools/EE-Zone-Substation-Load-Data-2024-25.zip")
SITES = {"Palmwoods Central": ("QLD1","TARONG#1"), "Trundle": ("NSW1","PARSF1")}
DAYS = [pd.Timestamp("2024-12-05"),pd.Timestamp("2024-12-07")]

def main(cache):
    cache.mkdir(exist_ok=True, parents=True)
    manifest = {}
    for name,url in URL.items():
        p=cache/(name+".zip")
        if not p.exists():
            r=requests.get(url,timeout=180); r.raise_for_status(); p.write_bytes(r.content)
        assert zipfile.is_zipfile(p), name
        manifest[name]=dict(url=url,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
    def table(name,cols):
        with zipfile.ZipFile(cache/(name+".zip")) as z, z.open(z.namelist()[0]) as f:
            d=pd.read_csv(f,skiprows=1,usecols=cols,low_memory=False).dropna(subset=["SETTLEMENTDATE"])
        d["end"]=pd.to_datetime(d.SETTLEMENTDATE); return d
    price=table("DISPATCHPRICE",["SETTLEMENTDATE","INTERVENTION","REGIONID","RRP"])
    lp=table("DISPATCH_LOCAL_PRICE",["SETTLEMENTDATE","DUID","LOCAL_PRICE_ADJUSTMENT","LOCALLY_CONSTRAINED"])
    q=table("DISPATCHINTERCONNECTORRES",["SETTLEMENTDATE","INTERVENTION","INTERCONNECTORID","MWFLOW","IMPORTLIMIT","EXPORTLIMIT","IMPORTGENCONID","EXPORTGENCONID"])
    q=q[(q.INTERCONNECTORID=="NSW1-QLD1") & (q.INTERVENTION==0)].set_index("end")
    with zipfile.ZipFile(cache/"energex.zip") as z:
        eg=pd.read_csv(z.open(next(x for x in z.namelist() if x.startswith("Palmwoods Central_"))))
    eg.index=pd.to_datetime(eg.Date+" "+eg.Time)
    with zipfile.ZipFile(cache/"essential.zip") as z, z.open(z.namelist()[0]) as f:
        ee=pd.concat([d[d.Name=="Trundle"] for d in pd.read_csv(f,chunksize=200000)])
    # Publisher describes AEST; the text clock on IntervalEnd advances the date
    # at midnight whereas the separate Date column labels the preceding day.
    ee.index=pd.to_datetime(ee.IntervalEnd.str[:19])
    ee["MW"]=ee.kW/1000; ee["MVA"]=np.hypot(ee.kW,ee.kVAr)/1000
    zones=[]; markets=[]
    for day in DAYS:
        h=pd.date_range(day+pd.Timedelta(minutes=30),periods=48,freq="30min")
        t=pd.date_range(day+pd.Timedelta(minutes=5),periods=288,freq="5min")
        for site,(region,duid) in SITES.items():
            zone=(eg if site=="Palmwoods Central" else ee).loc[h,["MW","MVA"]].copy()
            zone["site"]=site; zone["end"]=h; zone["signed_MVAr"]=ee.loc[h,"kVAr"].values/1000 if site=="Trundle" else np.nan
            zones.append(zone)
            r=price[(price.REGIONID==region)&(price.INTERVENTION==0)].set_index("end").loc[t,["RRP"]]
            local=lp[lp.DUID==duid].set_index("end")[["LOCAL_PRICE_ADJUSTMENT","LOCALLY_CONSTRAINED"]]
            assert local.index.is_unique
            a=r.join(local).join(q.loc[t,["MWFLOW","IMPORTLIMIT","EXPORTLIMIT","EXPORTGENCONID","IMPORTGENCONID"]])
            a["local_row"]=a.LOCAL_PRICE_ADJUSTMENT.notna()
            a["duid_benchmark"]=a.RRP+a.LOCAL_PRICE_ADJUSTMENT.fillna(0)
            a["end"]=t; a["region"]=region; a["duid"]=duid; markets.append(a)
    z=pd.concat(zones); m=pd.concat(markets)
    assert len(z)==192 and len(m)==1152 and not z[["MW","MVA"]].isna().any().any()
    assert not m[["RRP","MWFLOW","IMPORTLIMIT","EXPORTLIMIT"]].isna().any().any()
    z.to_csv(OUT/"zone-inputs.csv",index=False);m.to_csv(OUT/"market-inputs.csv",index=False)
    (OUT/"sources.json").write_text(json.dumps(manifest,indent=2)+"\n")

if __name__=="__main__": main(Path(sys.argv[1]))
