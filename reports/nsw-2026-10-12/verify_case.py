"""Offline regression tests from the original cutoff book and independently checked event rows."""
from pathlib import Path
from collections import Counter
from datetime import datetime,timedelta
import csv, hashlib, json, math

P=Path(__file__).resolve().parent
def rows(name):
    with (P/name).open() as f: return list(csv.DictReader(f))
b=rows('day_ahead_trade_book.csv'); a=rows('dispatch_scorecard.csv'); c=rows('consumer_proxies.csv')
m=json.loads((P/'manifest.json').read_text())
digest=hashlib.sha256((P/'day_ahead_trade_book.csv').read_bytes()).hexdigest()
assert digest==m['book_sha256']=='369a3aebe6805133a047d286f87fbe257dbfca3783263b52c99916645ab12080'
for table,step,count in [(b,30,48),(a,5,288)]:
    ends=[datetime.strptime(x['end_aest'],'%Y/%m/%d %H:%M:%S') for x in table]
    assert ends==[datetime(2026,10,12,4)+timedelta(minutes=(i+1)*step) for i in range(count)]
assert all(x['book_end_aest']==b[i//6]['end_aest'] for i,x in enumerate(a))
assert sum(int(x['firm_each_leg_mw']) for x in b)==0
assert sum(int(x['indicative_each_leg_mw']) for x in b)==23
assert Counter((x['forecast_signal'],x['actual_signal']) for x in a)=={('0','0'):139,('0','1'):11,('1','0'):32,('1','1'):106}
assert math.isclose(sum(float(x['gross_spread_aud_mwh'])*int(x['indicative_each_leg_mw'])/2 for x in b),131317.70)
assert math.isclose(sum(float(x['actual_spread'])*int(x['forecast_signal'])/12 for x in a),17355.0625)
assert len(c)==len({(r['end_aest'],r['duid']) for r in c})==576
for u,v,forecast in [('WILLBES1',-1049.05,-33.90),('WALGRV1',23107,119)]:
    r=next(x for x in c if x['duid']==u and x['end_aest']=='2026/10/12 14:20:00')
    assert float(r['actual_proxy'])==v and float(r['forecast_proxy'])==forecast
print('PASS: original cutoff hash, market-day coverage, 48:288 join, book, confusion matrix, marks and both consumer proxies.')
