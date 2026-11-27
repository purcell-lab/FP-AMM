"""Render the reproducible PNG figures and A4-proportioned one-page briefing."""
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from matplotlib.patches import FancyBboxPatch
import pandas as pd
import numpy as np

P=Path(__file__).resolve().parent
a=pd.read_csv(P/'dispatch_scorecard.csv'); b=pd.read_csv(P/'day_ahead_trade_book.csv')
c=pd.read_csv(P/'consumer_proxies.csv')
navy='#152f43'; teal='#00796d'; rust='#b44a24'; gray='#4f626d'; grid='#d8dfdf'
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11,'text.color':navy,'axes.labelcolor':gray,
                     'axes.edgecolor':grid,'xtick.color':gray,'ytick.color':gray,'axes.spines.top':False,'axes.spines.right':False})
x=np.arange(1,289)/12+4; ends=np.arange(1,49)/2+4
def axes(ax,ylabel):
    ax.set_xlim(4,28); ax.set_xticks(range(4,29,3),[f'{h%24:02d}:00' for h in range(4,29,3)])
    ax.set_yscale('symlog',linthresh=100); ax.set_ylabel(ylabel); ax.grid(axis='y',color=grid,linewidth=.7)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v,_:f'{v:,.0f}'))
def heading(fig,kicker,title,sub):
    fig.text(.09,.955,kicker,color=teal,weight='bold',fontsize=10)
    fig.text(.09,.885,title,color=navy,weight='bold',fontsize=21)
    fig.text(.09,.835,sub,color=gray,fontsize=11)
def foot(fig):
    fig.text(.09,.024,'AEMO NEMWEB  /  NSW1 market day, AEST  /  DUID prices are proxies, not household settlement.',fontsize=9,color=gray)
fig,aa=plt.subplots(2,1,figsize=(11,7.3),sharex=True)
fig.subplots_adjust(left=.09,right=.98,top=.77,bottom=.13,hspace=.34)
heading(fig,'FIGURE 01   /   FORECAST AUDIT','The spread signalled the corridor before the spike',
        'Across 48 half-hours, the forecast overstated how long extreme prices would persist.')
for ax,act,fc,ylabel in [(aa[0],'actual_rrp','forecast_nsw_rrp','NSW RRP  ·  $/MWh'),(aa[1],'actual_spread','gross_spread_aud_mwh','Pair spread  ·  $/MWh')]:
    ax.step(np.r_[4,x],np.r_[a[act].iloc[0],a[act]],where='pre',color=navy,lw=1.45,label='Five-minute outcome')
    ax.stairs(b[fc],np.r_[4,ends],baseline=None,color=rust,lw=1.8,label='Day-ahead forecast')
    axes(ax,ylabel);ax.legend(loc='upper left',ncol=2,frameon=False,fontsize=9)
aa[1].set_xlabel('Interval ending, AEST  ·  symmetric logarithmic price axes');foot(fig)
fig.savefig(P/'forecast-and-dispatch.png',dpi=165);plt.close(fig)
fig,ax=plt.subplots(figsize=(11,5.8));fig.subplots_adjust(left=.11,right=.98,top=.74,bottom=.17)
heading(fig,'FIGURE 02   /   CONSUMER LOCATION','One region, two different incentives',
        'The 14:20 divide is between DUID proxies. A feeder or household need not share that sensitivity.')
for u,label,col in [('WALGRV1','Sydney  /  Wallgrove',navy),('WILLBES1','Canberra  /  Williamsdale',teal)]:
    v=c[c.duid==u].actual_proxy; ax.step(x,v,where='pre',c=col,lw=1.8,label=label)
axes(ax,'Local proxy  ·  $/MWh');ax.axvline(14+1/3,c=rust,ls=':',lw=1.4);ax.legend(loc='upper left',frameon=False)
ax.set_xlabel('Interval ending, AEST  ·  symmetric logarithmic price axis');foot(fig)
fig.savefig(P/'sydney-canberra-proxies.png',dpi=165);plt.close(fig)

fig=plt.figure(figsize=(8.27,11.69),facecolor='#f4f5f1')
def txt(x,y,s,size=12,col=navy,bold=False):
    fig.text(x,y,s,va='top',fontsize=size,color=col,fontweight='bold' if bold else 'normal',linespacing=1.35)
def card(x,y,w,h,color):
    fig.add_artist(FancyBboxPatch((x,y),w,h,boxstyle='round,pad=0.008,rounding_size=0.011',
                                 transform=fig.transFigure,facecolor=color,edgecolor='none',zorder=-1))
txt(.07,.956,'FIELD NOTE   /   NSW ELECTRICITY   /   12 OCTOBER 2026',11,teal,True)
txt(.07,.909,'Two sides of one constraint',28,navy,True)
txt(.07,.855,'A day-ahead enquiry found the right direction. It did not secure a trade.',12)
card(.065,.687,.87,.11,navy)
for xx,big,small in [(.092,'48','half-hours tested'),(.38,'23','paired enquiries'),(.675,'0 MW','firm volume')]:
    txt(xx,.774,big,24,'white',True);txt(xx,.726,small,10,'#c4dcdf')
txt(.07,.652,'14:20 AEST   /   FIVE-MINUTE HOUSEHOLD ILLUSTRATION',10,teal,True)
for xx,city,action,price,dollars,clr in [(.07,'SYDNEY','Export 5 kW','$23.107/kWh','$9.63 gross credit',navy),(.52,'CANBERRA','Absorb 5 kW','−$1.049/kWh','$0.44 gross credit',teal)]:
    card(xx,.447,.405,.17,'white');txt(xx+.016,.602,city,10,clr,True);txt(xx+.016,.571,action,18,clr,True)
    txt(xx+.016,.525,price,15);txt(xx+.016,.485,dollars,12)
txt(.07,.418,'Same 0.417 kWh per leg. Illustrative local settlement only: Wallgrove and\nWilliamsdale proxies, not tariffs, metered responses or payments.',11,gray)
txt(.07,.343,'How well did the day-ahead signal travel?',17,navy,True)
bx=fig.add_axes([.074,.278,.84,.039]);bx.barh(0,106,color=teal,height=.62);bx.barh(0,32,left=106,color='#e5ae81',height=.62);bx.set_xlim(0,138);bx.axis('off')
txt(.075,.274,'106 confirmed',12,teal,True);txt(.67,.274,'32 false alarms',12,rust,True)
txt(.075,.239,'Of 138 flagged five-minute intervals; another 11 qualifying intervals were missed.',10,gray)
card(.065,.097,.87,.106,navy)
txt(.091,.185,'WHAT THIS PROVES',10,'#a6dace',True)
txt(.091,.155,'Seek two firm offers. Keep NEMDE as the dispatch baseline.\nNo lower system cost, bill saving or emissions benefit was proved.',11.4,'white')
txt(.07,.068,'Next test: two community hubs, separate envelopes and a named payer. Design reference: Power of the Many.\nData: AEMO predispatch, DispatchIS and next-day dispatch.  |  github.com/purcell-lab/FP-AMM',8,gray)
fig.savefig(P/'one-page-infographic.png',dpi=200);plt.close(fig)
