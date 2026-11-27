"""Run offline on the two published case extracts; no credentials or live controls."""
from pathlib import Path
import numpy as np
import pandas as pd

P=Path(__file__).resolve().parent
SITES={"Palmwoods Central":("QLD1",15.,30.), "Trundle":("NSW1",5.5,13.)}
DATES={"frozen":"2024-12-05","observed":"2024-12-07"}
def inputs(z,m,site,date,rating):
    reg=SITES[site][0];t=pd.Timestamp(date)
    h=pd.date_range(t+pd.Timedelta("30min"),periods=48,freq="30min")
    five=pd.date_range(t+pd.Timedelta("5min"),periods=288,freq="5min")
    a=z[z.site==site].set_index("end").loc[h].copy()
    b=m[m.region==reg].set_index("end").loc[five].copy()
    assert a.index.is_unique and b.index.is_unique and len(b)==288
    # QNI reference direction NSW to Queensland; bounds are signed.
    up=(b.EXPORTLIMIT-b.MWFLOW).clip(lower=0);dn=(b.MWFLOW-b.IMPORTLIMIT).clip(lower=0)
    b["ti"],b["te"]=(up,dn) if reg=="QLD1" else (dn,up)
    b["block"]=np.repeat(np.arange(48),6);g=b.groupby("block")
    d=g[["ti","te"]].min()
    d["rrp"]=g.RRP.mean(); d["local"]=g.duid_benchmark.mean()
    d["p"]=a.MW.values;d["s"]=a.MVA.values
    assert (d.s+1e-6>=abs(d.p)).all()
    q2=(d.s**2-d.p**2).clip(lower=0)
    d["di"]=(np.sqrt((rating**2-q2).clip(lower=0))-d.p).clip(lower=0)
    # Do not deepen reverse flow; do not require relief of an existing exceedance.
    d["de"]=d.p.clip(lower=0)
    return d,b

def book(i):
    hour=i/2
    return (2.2 if 17<=hour<21 else 1.0,2.4 if 8<=hour<16 else .30)

def clear(b,s,ti,te,di,de,mode,slope=150.):
    inf=float("inf")
    hi=min(ti if mode in ("transmission","joint") else inf,di if mode in ("distribution","joint") else inf)
    lo=-min(te if mode in ("transmission","joint") else inf,de if mode in ("distribution","joint") else inf)
    # Symmetric elastic response, with a single price for buys and sells.
    # On the central segment net request is linear; the root is exact.
    x=np.clip(b-s,lo,hi); shift=slope*(b-s-x)/(b+s); price=100+shift
    buy=b*(1-shift/slope);sell=s*(1+shift/slope)
    assert min(buy,sell)>-1e-9 and abs(buy-sell-x)<1e-7
    return price,buy,sell,x

def run():
    z=pd.read_csv(P/"zone-inputs.csv",parse_dates=["end"]);m=pd.read_csv(P/"market-inputs.csv",parse_dates=["end"])
    rows=[];scores=[];bills=[]
    for site,(reg,firm,wide) in SITES.items():
        obs,b5=inputs(z,m,site,DATES["observed"],firm)
        for stage,date in DATES.items():
            for variant,rating,slope in [("base",firm,150),("wide",wide,150),("flatter_price",firm,75)]:
                frame,_=inputs(z,m,site,date,rating)
                for mode in ["unconstrained","transmission","distribution","joint"]:
                    for i,v in frame.iterrows():
                        b,s=book(i);pr,buy,sell,x=clear(b,s,v.ti,v.te,v.di,v.de,mode,slope)
                        stamp=pd.Timestamp("2024-12-07 00:30")+pd.Timedelta(minutes=i*30)
                        rows.append(dict(site=site,end=stamp,stage=stage,variant=variant,mode=mode,
                                         request_buy_MW=b,request_sell_MW=s,tx_import_MW=v.ti,tx_export_MW=v.te,
                                         dn_import_MW=v.di,dn_export_MW=v.de,zone_MW=v.p,zone_MVA=v.s,
                                         buy_MW=buy,sell_MW=sell,net_MW=x,price=pr,rrp=obs.loc[i,"rrp"],local=obs.loc[i,"local"]))
    out=pd.DataFrame(rows);out.to_csv(P/"interval-results.csv",index=False,float_format="%.8f")
    for site in SITES:
        obs,five=inputs(z,m,site,DATES["observed"],SITES[site][1])
        for stage in DATES:
            for variant in ["base","wide","flatter_price"]:
                for mode in ["unconstrained","transmission","distribution","joint"]:
                    a=out[(out.site==site)&(out.stage==stage)&(out["mode"]==mode)&(out.variant==variant)].reset_index(drop=True)
                    x=np.repeat(a.net_MW.to_numpy(),6)
                    ob,_=inputs(z,m,site,DATES["observed"],SITES[site][2] if variant=="wide" else SITES[site][1])
                    hi=np.minimum(five.ti,np.repeat(ob.di.to_numpy(),6));lo=-np.minimum(five.te,np.repeat(ob.de.to_numpy(),6))
                    over=np.maximum(0,np.maximum(x-hi,lo-x)).to_numpy()
                    scores.append(dict(site=site,stage=stage,variant=variant,mode=mode,
                        moved_blocks=sum(abs(a.price-100)>.01),buy_MWh=.5*a.buy_MW.sum(),sell_MWh=.5*a.sell_MW.sum(),
                        net_redispatched_MWh=.5*abs(a.request_buy_MW-a.request_sell_MW-a.net_MW).sum(),
                        price_mean=a.price.mean(),price_min=a.price.min(),price_max=a.price.max(),rrp_mean=a.rrp.mean(),local_mean=a.local.mean(),
                        blocks_outside=sum(over.reshape(48,6).max(axis=1)>.001),excess_MWh=over.sum()/12,
                        # positive = extra money needed for net wholesale imports less AMM collections
                        wholesale_shortfall_AUD=.5*((a.rrp-a.price)*a.net_MW).sum()))
                if variant!="base":continue
                a=out[(out.site==site)&(out.stage==stage)&(out["mode"]=="joint")&(out.variant==variant)].reset_index(drop=True)
                hr=np.arange(48)/2
                for profile,load in {"flat importer":np.ones(48),"solar household":np.where((hr>=9)&(hr<15),-2.,np.where((hr>=17)&(hr<21),2.,.5))}.items():
                    # Fixed whole-meter net kW. Counterfactual retail exposure only.
                    costs={k:.5*np.dot(load,a[col])/1000 for k,col in [("rrp","rrp"),("duid","local"),("amm","price")]}
                    bills.append(dict(site=site,stage=stage,profile=profile,import_kWh=.5*np.maximum(load,0).sum(),
                        export_kWh=.5*np.maximum(-load,0).sum(),**costs,delta_vs_RRP=costs["amm"]-costs["rrp"]))
    pd.DataFrame(scores).to_csv(P/"scorecard.csv",index=False,float_format="%.8f")
    pd.DataFrame(bills).to_csv(P/"consumer.csv",index=False,float_format="%.8f")
    return out
if __name__=="__main__":run()
