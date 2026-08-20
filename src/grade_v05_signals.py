from __future__ import annotations
import csv, json, os, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
import requests

INFILE=Path(os.getenv('SIGNAL_FILE','results_v05_acceleration/awakening_scan.csv'))
OUT=Path('results_v05_grader'); OUT.mkdir(exist_ok=True)
KEY=os.getenv('ALPACA_API_KEY','').strip(); SECRET=os.getenv('ALPACA_SECRET_KEY','').strip(); FEED=os.getenv('ALPACA_DATA_FEED','iex')
H={'APCA-API-KEY-ID':KEY,'APCA-API-SECRET-KEY':SECRET,'Accept':'application/json'}

def get_bars(sym,start,end,timeframe='5Min'):
 q=urlencode({'timeframe':timeframe,'start':start.isoformat().replace('+00:00','Z'),'end':end.isoformat().replace('+00:00','Z'),'limit':'1000','adjustment':'raw','feed':FEED})
 r=requests.get(f'https://data.alpaca.markets/v2/stocks/{sym}/bars?{q}',headers=H,timeout=30); r.raise_for_status(); return r.json().get('bars') or []
def pct(p,e): return ((p/e)-1)*100 if e else None
def first_after(bars,target):
 for b in bars:
  t=datetime.fromisoformat(b['t'].replace('Z','+00:00'))
  if t>=target:return float(b['c'])
 return None

def main():
 if not KEY or not SECRET: raise SystemExit('Missing Alpaca secrets')
 if not INFILE.exists(): raise SystemExit(f'Missing signal file: {INFILE}')
 rows=list(csv.DictReader(INFILE.open()))
 out=[]; diag=[]
 for r in rows:
  try:
   sym=r['ticker']; entry=float(r['price']); detected=datetime.fromisoformat(r['detected_at_et']).astimezone(timezone.utc)
   bars=get_bars(sym,detected-timedelta(minutes=5),detected+timedelta(days=3))
   after=[b for b in bars if datetime.fromisoformat(b['t'].replace('Z','+00:00'))>=detected]
   prices=[float(b['c']) for b in after]; highs=[float(b['h']) for b in after]; lows=[float(b['l']) for b in after]
   rec=dict(r)
   for mins in (15,30,60):
    p=first_after(after,detected+timedelta(minutes=mins)); rec[f'p_{mins}m']=p; rec[f'ret_{mins}m_pct']=pct(p,entry) if p else None
   same_day=[b for b in after if datetime.fromisoformat(b['t'].replace('Z','+00:00')).date()==detected.date()]
   rec['same_day_close']=float(same_day[-1]['c']) if same_day else None; rec['same_day_close_ret_pct']=pct(rec['same_day_close'],entry) if rec['same_day_close'] else None
   next_dates=sorted({datetime.fromisoformat(b['t'].replace('Z','+00:00')).date() for b in after if datetime.fromisoformat(b['t'].replace('Z','+00:00')).date()>detected.date()})
   nd=[b for b in after if next_dates and datetime.fromisoformat(b['t'].replace('Z','+00:00')).date()==next_dates[0]]
   rec['next_day_high']=max((float(b['h']) for b in nd),default=None); rec['next_day_close']=float(nd[-1]['c']) if nd else None
   rec['next_day_high_ret_pct']=pct(rec['next_day_high'],entry) if rec['next_day_high'] else None; rec['next_day_close_ret_pct']=pct(rec['next_day_close'],entry) if rec['next_day_close'] else None
   rec['max_favorable_pct']=pct(max(highs),entry) if highs else None; rec['max_adverse_pct']=pct(min(lows),entry) if lows else None
   out.append(rec); time.sleep(.12)
  except Exception as e: diag.append([r.get('ticker','?'),type(e).__name__,str(e)])
 fields=[]
 for r in out:
  for k in r:
   if k not in fields: fields.append(k)
 with (OUT/'graded_signals.csv').open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(out)
 (OUT/'graded_signals.json').write_text(json.dumps(out,indent=2))
 with (OUT/'diagnostics.csv').open('w',newline='') as f: csv.writer(f).writerows([['ticker','error_type','detail'],*diag])
 (OUT/'summary.json').write_text(json.dumps({'input_signals':len(rows),'graded':len(out),'errors':len(diag),'feed':FEED,'graded_at_utc':datetime.now(timezone.utc).isoformat()},indent=2))
 print(json.dumps({'input_signals':len(rows),'graded':len(out),'errors':len(diag)},indent=2))
if __name__=='__main__': main()
