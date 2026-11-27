from pathlib import Path
import numpy as np
import pandas as pd
from replay import clear
P=Path(__file__).resolve().parent
def main():
    z=pd.read_csv(P/"zone-inputs.csv");m=pd.read_csv(P/"market-inputs.csv");d=pd.read_csv(P/"interval-results.csv")
    s=pd.read_csv(P/"scorecard.csv");c=pd.read_csv(P/"consumer.csv");a=pd.read_csv(P/"battery-schedules.csv")
    assert len(z)==192 and not z.duplicated(["site","end"]).any()
    assert len(m)==1152 and not m.duplicated(["region","end"]).any()
    np.testing.assert_allclose(m.duid_benchmark,m.RRP+m.LOCAL_PRICE_ADJUSTMENT.fillna(0))
    assert len(d)==2304 and len(s)==48 and len(c)==8
    assert not d.duplicated(["end","site","stage","mode","variant"]).any()
    np.testing.assert_allclose(d.buy_MW-d.sell_MW,d.net_MW,atol=2e-8)
    j=d[d["mode"]=="joint"]
    assert (j.net_MW<=j[["tx_import_MW","dn_import_MW"]].min(axis=1)+1e-7).all()
    assert (-j.net_MW<=j[["tx_export_MW","dn_export_MW"]].min(axis=1)+1e-7).all()
    assert s[(s["mode"]=="joint")&(s.stage=="observed")].blocks_outside.eq(0).all()
    for (site,stage),g in j[j.variant=="base"].groupby(["site","stage"]):
        cc=c[(c.site==site)&(c.stage==stage)&(c.profile=="flat importer")].iloc[0]
        assert abs(cc.amm-g.price.mean()*24/1000)<1e-7
        assert abs(cc.rrp-g.rrp.mean()*24/1000)<1e-7
    for _,g in a.groupby(["site","schedule"]):
        assert g.end_SoC_MWh.between(0,2).all() and g.end_SoC_MWh.iloc[-1]==1
        assert max(g.charge_MWh.max(),g.discharge_MWh.max())<=.5
        assert not ((g.charge_MWh>0)&(g.discharge_MWh>0)).any()
        np.testing.assert_allclose(np.diff(np.r_[1,g.end_SoC_MWh]),g.charge_MWh*np.sqrt(.9)-g.discharge_MWh/np.sqrt(.9),atol=1e-8)
    for buy,sell in [(2,.3),(.3,2)]:
        p,b,e,x=clear(buy,sell,0,0,0,0,"joint")
        assert abs(x)<1e-8 and abs(b-e)<1e-8 and (p-100)*(buy-sell)>0
    print("PASS: input keys, coverage, sparse offsets, 2304 clears, directional feasibility, scoring, household cash flows and 6 battery schedules.")
if __name__=="__main__":main()
