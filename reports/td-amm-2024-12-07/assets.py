"""Price-taking asset illustrations, not bids, dispatch or actual plant earnings."""
from pathlib import Path
import numpy as np
import pandas as pd

P=Path(__file__).resolve().parent
def optimise(price):
    # State in MWh; start and finish half full. Only one action per interval.
    e=np.arange(41)*.05; delta=e[None,:]-e[:,None]; eta=np.sqrt(.9)
    bought=np.maximum(delta,0)/eta; sold=np.maximum(-delta,0)*eta
    allowed=(bought<=.5+1e-9)&(sold<=.5+1e-9)
    end=np.full(41,-np.inf);end[20]=0;path=[]
    for p in price[::-1]:
        gain=np.where(allowed,(sold-bought)*p-10*sold+end[None,:],-np.inf)
        path.append(gain.argmax(axis=1));end=gain.max(axis=1)
    state=20;rows=[]
    for step in reversed(path):
        n=step[state];rows.append((bought[state,n],sold[state,n],e[n]));state=n
    assert state==20
    return np.array(rows)

def run():
    d=pd.read_csv(P/"interval-results.csv");rows=[];dispatch=[]
    for site,g in d[(d["mode"]=="joint")&(d.variant=="base")].groupby("site"):
        a=g[g.stage=="frozen"];prices={k:a[c].to_numpy() for k,c in [("frozen_AMM","price"),("RRP","rrp"),("DUID","local")]}
        psolar=.5*((np.arange(48)>=18)&(np.arange(48)<30))
        for k,price in prices.items():
            rows.append(dict(site=site,asset="solar strip",schedule="fixed",valuation=k,import_MWh=0,export_MWh=6,net_AUD=psolar@price))
        for sched,price in prices.items():
            b,s,e=optimise(price).T
            for time,bi,si,ei in zip(a.end,b,s,e):
                dispatch.append(dict(site=site,schedule=sched,end=time,charge_MWh=bi,discharge_MWh=si,end_SoC_MWh=ei))
            for k,v in prices.items():
                rows.append(dict(site=site,asset="battery",schedule=sched,valuation=k,import_MWh=b.sum(),export_MWh=s.sum(),net_AUD=(s-b)@v-10*s.sum()))
    pd.DataFrame(rows).to_csv(P/"asset-values.csv",index=False,float_format="%.8f")
    pd.DataFrame(dispatch).to_csv(P/"battery-schedules.csv",index=False,float_format="%.8f")
if __name__=="__main__":run()
