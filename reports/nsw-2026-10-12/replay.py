"""Python 3, standard library. Usage: python replay.py /path/to/downloads
Download the four ZIP archives named in SOURCES (see manifest.json).
Outputs alongside this script. No network, control or trading interface."""
import collections, csv, hashlib, io, json, sys, zipfile
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent
RAW = Path(sys.argv[1])
WEEK = 'PUBLIC_PREDISPATCHIS_20261011_20261017.zip'
NEXT = 'PUBLIC_NEXT_DAY_DISPATCH_20261012_0000000542594175.zip'
DAILY = ['PUBLIC_DISPATCHIS_202610%s.zip' % d for d in ('12','13')]
BASE = 'https://nemweb.com.au/Reports/'
SOURCES = [BASE+'ARCHIVE/PredispatchIS_Reports/'+WEEK,
           *[BASE+'ARCHIVE/DispatchIS_Reports/'+d for d in DAILY],
           BASE+'CURRENT/Next_Day_Dispatch/'+NEXT]
start = datetime(2026,10,12,4)
cut = datetime(2026,10,11,13)
fmt = '%Y/%m/%d %H:%M:%S'
C = 'N::N_CNCW_2'
A,B = 'TUMUT3','WALGRV1'
ACT = 'WILLBES1'  # retrospective consumer illustration only; never changes the book

def times(n,minutes):
    return [(start+timedelta(minutes=i*minutes)).strftime(fmt) for i in range(1,n+1)]

def write(name, rows):
    with (OUT/name).open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

def parse(z, tables):
    headers={}
    with z.open(z.namelist()[0]) as f:
        for l in csv.reader(io.TextIOWrapper(f,encoding='latin1')):
            if l[0]=='I': headers[l[2]]=l[4:]
            if l[0]=='D' and l[2] in tables:
                d=dict(zip(headers[l[2]],l[4:]))
                if d.get('INTERVENTION','0')=='0': yield l[2],d

def sha(b): return hashlib.sha256(b).hexdigest()
with zipfile.ZipFile(RAW/WEEK) as z:
    candidates=sorted((datetime.strptime(i.filename[-18:-4],'%Y%m%d%H%M%S'),datetime(*i.date_time),i.filename) for i in z.infolist())
    generated,archived,name=max(x for x in candidates if max(x[:2]) <=cut)
    following=next(x for x in candidates if x[0]>cut)
    buf=z.read(name)
p=zipfile.ZipFile(io.BytesIO(buf))
hdr=next(csv.reader([p.open(p.namelist()[0]).readline().decode()]))
assert datetime.strptime(' '.join(hdr[5:7]),fmt)==generated
T=times(48,30); V=times(288,5)
pd={t:{} for t in T}
for table,d in parse(p,{'REGION_PRICES','REGION_SOLUTION','LOCAL_PRICE','CONSTRAINT_SOLUTION','INTERCONNECTOR_SOLN'}):
    t=d['DATETIME']
    if t not in pd: continue
    row=pd[t]; unit=d.get('DUID')
    if table=='REGION_PRICES' and d['REGIONID']=='NSW1': row['rrp']=float(d['RRP'])
    if table=='REGION_SOLUTION' and d['REGIONID']=='NSW1':
        for k in ('TOTALDEMAND','AVAILABLEGENERATION','NETINTERCHANGE'): row[k]=float(d[k])
    if table=='LOCAL_PRICE' and unit in (A,B,ACT): row[unit]=float(d['LOCAL_PRICE_ADJUSTMENT'])
    if table=='CONSTRAINT_SOLUTION' and d['CONSTRAINTID']==C:
        for k in ('MARGINALVALUE','RHS','LHS'): row[k]=float(d[k])
    if table=='INTERCONNECTOR_SOLN' and d['INTERCONNECTORID']=='VIC1-NSW1': row['flow']=float(d['MWFLOW'])

book=[]
for t,r in pd.items():
    a,b=r.get(A,0),r.get(B,0); spread=round(b-a,5)
    signal=spread>0.01 and abs(r.get('MARGINALVALUE',0))>0.01
    book.append(dict(end_aest=t,forecast_nsw_rrp=r['rrp'],absorb_proxy=A,export_proxy=B,
        absorb_lpa_row=int(A in r),export_lpa_row=int(B in r),absorb_proxy_price=round(r['rrp']+a,5),
        export_proxy_price=round(r['rrp']+b,5),gross_spread_aud_mwh=spread,
        constraint=C,constraint_row=int('RHS' in r),constraint_mv=r.get('MARGINALVALUE',''),
        constraint_lhs=r.get('LHS',''),constraint_rhs=r.get('RHS',''),nsw_demand_mw=r['TOTALDEMAND'],
        nsw_available_generation_mw=r['AVAILABLEGENERATION'],nsw_netinterchange_mw=r['NETINTERCHANGE'],
        vic1_nsw1_flow_mw=r['flow'],indicative_each_leg_mw=int(signal),firm_each_leg_mw=0,
        status='Seek both offers; no firm orders or verified envelopes' if signal else 'No qualifying forecast signal'))
write('day_ahead_trade_book.csv',book)
manifest=dict(sources=SOURCES,snapshot=name,header=hdr,member_zip_sha256=sha(buf),generated_aest=str(generated),
              archive_member_timestamp=str(archived),next_generated_aest=str(following[0]),cutoff_aest=str(cut),
              book_sha256=sha((OUT/'day_ahead_trade_book.csv').read_bytes()))
(OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
# The book is written and hashed before opening any dispatch files.
actual={t:{} for t in V}
for day in DAILY:
    with zipfile.ZipFile(RAW/day) as z:
        for n in z.namelist():
            t=datetime.strptime(n.split('_')[2],'%Y%m%d%H%M').strftime(fmt)
            if t not in actual: continue
            r=actual[t]
            with zipfile.ZipFile(io.BytesIO(z.read(n))) as sub:
                for table,d in parse(sub,{'PRICE','CONSTRAINT','LOCAL_PRICE'}):
                    assert d['SETTLEMENTDATE']==t
                    if table=='LOCAL_PRICE' and d['DUID'] in (A,B,ACT): r[d['DUID']]=float(d['LOCAL_PRICE_ADJUSTMENT'])
                    if table=='PRICE' and d['REGIONID']=='NSW1': r['rrp']=float(d['RRP'])
                    if table=='CONSTRAINT' and d['CONSTRAINTID']==C: r['mv']=float(d['MARGINALVALUE'])
with zipfile.ZipFile(RAW/NEXT) as z:
    for table,d in parse(z,{'LOCAL_PRICE','UNIT_SOLUTION','CONSTRAINT'}):
        t=d['SETTLEMENTDATE']; u=d.get('DUID'); r=actual[t]
        if table=='CONSTRAINT' and d['CONSTRAINTID']==C:
            mv=float(d['MARGINALVALUE']); assert r.get('mv',mv)==mv; r['mv']=mv
        if table=='LOCAL_PRICE' and u in (A,B,ACT):
            v=float(d['LOCAL_PRICE_ADJUSTMENT']); assert r.get(u,v)==v; r[u]=v
        if table=='UNIT_SOLUTION' and u==B:
            r['export_target']=float(d['TOTALCLEARED']); r['export_available']=float(d['AVAILABILITY'])

score=[]
for i,(t,r) in enumerate(actual.items()):
    x=book[i//6]; s=r.get(B,0)-r.get(A,0); mv=r.get('mv',0)
    score.append(dict(end_aest=t,book_end_aest=x['end_aest'],forecast_rrp=x['forecast_nsw_rrp'],
        actual_rrp=r['rrp'],forecast_spread=x['gross_spread_aud_mwh'],actual_spread=round(s,5),
        actual_absorb_row=int(A in r),actual_export_row=int(B in r),
        actual_absorb_proxy_price=round(r['rrp']+r.get(A,0),5),actual_export_proxy_price=round(r['rrp']+r.get(B,0),5),
        actual_constraint_row=int('mv' in r),actual_mv=r.get('mv',''),forecast_signal=x['indicative_each_leg_mw'],
        actual_signal=int(s>0.01 and abs(mv)>0.01),export_target_mw=r['export_target'],export_availability_mw=r['export_available']))
assert len(score)==288 and all(sum(y['book_end_aest']==x['end_aest'] for y in score)==6 for x in book)
write('dispatch_scorecard.csv',score)
consumer=[]
for i,(t,r) in enumerate(actual.items()):
    x=book[i//6]; f=pd[x['end_aest']]
    for u in (B,ACT):
        consumer.append(dict(end_aest=t,duid=u,forecast_block_end=x['end_aest'],
            forecast_lpa_row=int(u in f),forecast_lpa=f.get(u,0),
            forecast_proxy=round(f['rrp']+f.get(u,0),5),
            actual_lpa_row=int(u in r),actual_lpa=r.get(u,0),
            actual_proxy=round(r['rrp']+r.get(u,0),5)))
write('consumer_proxies.csv',consumer)
print('signal confusion (forecast,actual)',collections.Counter((x['forecast_signal'],x['actual_signal']) for x in score))
print('max RRP and spread',max(score,key=lambda x:x['actual_rrp']),max(score,key=lambda x:x['actual_spread']))
print('forecast proposals',sum(x['indicative_each_leg_mw'] for x in book),'max RRP',max(x['forecast_nsw_rrp'] for x in book))
print('half-hour mean absolute error',sum(abs(x['forecast_nsw_rrp']-sum(y['actual_rrp'] for y in score[i*6:i*6+6])/6) for i,x in enumerate(book))/48)
