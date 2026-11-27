from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import FancyBboxPatch
P=Path(__file__).resolve().parent
ink,teal,amber,blue,red="#183549","#00796f","#b96700","#376fa0","#ab3658"
plt.rcParams.update({"font.size":10,"axes.spines.top":False,"axes.spines.right":False,"axes.titlelocation":"left","axes.labelcolor":ink,"text.color":ink,"axes.titleweight":"bold"})
def save(fig,name):
    fig.savefig(P/name,dpi=160,bbox_inches="tight",facecolor="white");plt.close(fig)
def box(ax,x,y,w,h,title,sub,col):
    ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle="round,pad=0.012",facecolor="#f4f8fa",edgecolor=col,lw=1.8))
    ax.text(x+w/2,y+h*.68,title,ha="center",va="center",weight="bold",color=col,fontsize=12)
    ax.text(x+w/2,y+h*.3,sub,ha="center",va="center",fontsize=10)
def arrow(ax,a,b):
    ax.annotate("",xy=b,xytext=a,arrowprops=dict(arrowstyle="->",color=ink,lw=1.6))
def flows():
    for proc in [False,True]:
        fig,ax=plt.subplots(figsize=(12,4.1));ax.set(xlim=(0,1),ylim=(0,1));ax.axis("off")
        if not proc:
            ax.set_title("Data flow | Separate physical inputs from price benchmarks",fontsize=16)
            for y,title,sub in [(.69,"DNSP records","Zone MW, MVA or kVAr"),(.39,"NEMWEB QNI","Target, limits, setter IDs"),(.09,"NEMWEB prices","RRP and generator offset")]:
                box(ax,.02,y,.26,.20,title,sub,teal)
            box(ax,.38,.55,.26,.23,"Directional headroom","MW in / MW out by block",blue)
            box(ax,.38,.10,.26,.22,"Comparator only","Five minutes, then block mean",blue)
            box(ax,.72,.55,.25,.23,"AMM + requests","Shadow price and quantities",amber)
            box(ax,.72,.10,.25,.22,"Score + reprice","Physical tests and cash flows",amber)
            for a,b in [((.28,.79),(.38,.71)),((.28,.49),(.38,.63)),((.28,.19),(.38,.20)),((.64,.66),(.72,.66)),((.84,.55),(.84,.32)),((.64,.21),(.72,.21))]:arrow(ax,a,b)
        else:
            ax.set_title("Process flow | Freeze the book, then test what it promised",fontsize=16)
            for x,y,t,s,c in [(.02,.60,"Freeze inputs","5 Dec clock profile + assumptions",teal),(.37,.60,"Request at $100/MWh","Buy MW minus sell MW",teal),(.72,.60,"Apply path limits","Project net MW into bounds",teal),(.72,.12,"Solve clearing price","Buy and sell curves respond",amber),(.37,.12,"Score frozen schedule","Check against six QNI intervals",amber),(.02,.12,"Compare outcomes","Risk, bills and funding residual",amber)]:
                box(ax,x,y,.25,.24,t,s,c)
            for a,b in [((.27,.72),(.37,.72)),((.62,.72),(.72,.72)),((.845,.60),(.845,.36)),((.72,.24),(.62,.24)),((.37,.24),(.27,.24))]:arrow(ax,a,b)
        save(fig,"process-flow.png" if proc else "data-flow.png")
def charts():
    d=pd.read_csv(P/"interval-results.csv",parse_dates=["end"]);m=pd.read_csv(P/"market-inputs.csv",parse_dates=["end"])
    m=m[m.end>=pd.Timestamp("2024-12-07")];day=pd.Timestamp("2024-12-07");h=(np.arange(48)+1)/2
    fig,axes=plt.subplots(3,1,figsize=(11,8.3),sharex=True,layout="constrained")
    for ax,(site,reg) in zip(axes[:2],[("Palmwoods Central","QLD1"),("Trundle","NSW1")]):
        a=d[(d.site==site)&(d.stage=="observed")&(d.variant=="base")&(d["mode"]=="joint")]
        ax.plot(h,a.zone_MW,label="Real power (MW)",color=teal);ax.plot(h,a.zone_MVA,"--",label="Apparent power (MVA)",color=blue)
        ax.axhline(15 if reg=="QLD1" else 5.5,color=amber,ls=":",label="Experiment import rating (MVA)")
        ax.axhline(0,lw=.7,color=ink);ax.set_title(site+" | measured half-hourly zone loading");ax.legend(ncol=3,fontsize=9,loc="upper left");ax.set_ylabel("MW or MVA")
    a=m[m.region=="QLD1"];t=(a.end-day).dt.total_seconds()/3600;ax=axes[2]
    ax.fill_between(t,a.IMPORTLIMIT,a.EXPORTLIMIT,color="#c4dbde",label="QNI lower to upper bound")
    ax.plot(t,a.MWFLOW,color=ink,lw=1.5,label="NEMDE flow target");ax.set_title("NSW to Queensland | five-minute QNI target and signed limits");ax.set_ylabel("MW");ax.legend(ncol=2,fontsize=9);ax.set_xlabel("7 December 2024, AEST | interval ending")
    for ax in axes:ax.grid(alpha=.16);ax.set_xticks(np.arange(0,25,3));ax.set_xlim(0,24)
    save(fig,"network-inputs.png")
    fig,axes=plt.subplots(2,1,figsize=(12,7),sharex=True,layout="constrained")
    for ax,(site,reg) in zip(axes,[("Palmwoods Central","QLD1"),("Trundle","NSW1")]):
        a=m[m.region==reg];t=(a.end-day).dt.total_seconds()/3600
        ax.plot(t,a.RRP,color=blue,lw=1.2,label=reg+" RRP (5 min)")
        ax.plot(t,a.duid_benchmark,color=amber,lw=1.1,ls="--",label=a.duid.iloc[0]+" generator proxy (5 min)")
        for stage,sty,col in [("frozen","-",teal),("observed",":",red)]:
            a=d[(d.site==site)&(d.stage==stage)&(d.variant=="base")&(d["mode"]=="joint")]
            ax.stairs(a.price,np.arange(49)/2,color=col,linestyle=sty,lw=2,label=stage+" joint AMM")
        ax.set_title(site);ax.set_yscale("symlog",linthresh=50);ax.set_ylabel("$/MWh · symlog");ax.grid(alpha=.2);ax.legend(fontsize=9,ncol=2,loc="upper left")
    axes[-1].set_xlabel("7 December 2024, AEST | RRP and DUID are not household tariffs");axes[-1].set_xticks(np.arange(0,25,3));axes[-1].set_xlim(0,24)
    save(fig,"price-comparison.png")
if __name__=="__main__":flows();charts()
