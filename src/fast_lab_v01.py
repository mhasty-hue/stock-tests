from __future__ import annotations
import csv, json, math, os, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
import requests

OUT=Path('results_fast_lab_v01'); OUT.mkdir(exist_ok=True)
KEY=os.getenv('ALPACA_API_KEY','').strip(); SECRET=os.getenv('ALPACA_SECRET_KEY','').strip(); FEED=os.getenv('ALPACA_DATA_FEED','iex')
H={'APCA-API-KEY-ID':KEY,'APCA-API-SECRET-KEY':SECRET,'Accept':'application/json'}
SYMBOLS=[s.strip().upper() for s in os.getenv('LAB_SYMBOLS','').split(',') if s.strip()]
START=os.getenv('LAB_START','2025-01-02'); END=os.getenv('LAB_END','2025-03-31')

def bars(sym):
 q=urlencode({'timeframe':'5Min','start':START+'T09:00:00Z','end':END+'T23:59:59Z','limit':10000,'adjustment':'raw','feed':FEED})
 r=requests.get(f'https://data.alpaca.markets/v2/stocks/{sym}/bars?{q}',headers=H,timeout=40); r.raise_for_status(); return r.json().get('bars') or []
def ret(a,b): return (a/b-1)*100 if b else 0
def safe_div(a,b): return a/b if b else 0

def events(sym,bs):
 out=[]
 # session-local features only; no future data enters feature calculation
 byday={}
 for b in bs: byday.setdefault(b['t'][:10],[]).append(b)
 for day,x in byday.items():
  if len(x)<18: continue
  day_open=float(x[0]['o'])
  vols=[float(z['v']) for z in x]
  for i in range(6,len(x)-13,3): # observations every 15m
   p=float(x[i]['c']);
   if not 2<=p<=100: continue
   v5=ret(p,float(x[i-1]['c'])); v15=ret(p,float(x[i-3]['c'])); prev15=ret(float(x[i-3]['c']),float(x[i-6]['c'])); accel=v15-prev15
   recent=sum(vols[i-2:i+1]); prior=sum(vols[i-5:i-2]); vol_acc=safe_div(recent,prior)
   move=ret(p,day_open)
   future=x[i+1:i+13]
   if not future: continue
   highs=[float(z['h']) for z in future]; lows=[float(z['l']) for z in future]
   mfe=ret(max(highs),p); mae=ret(min(lows),p); close60=float(future[min(11,len(future)-1)]['c']); r60=ret(close60,p)
   # hypothesis score, deliberately simple; grid evaluation below tests thresholds rather than fitting coefficients
   out.append({'ticker':sym,'day':day,'detected_at':x[i]['t'],'price':round(p,4),'move_pct':round(move,3),'vel5':round(v5,3),'vel15':round(v15,3),'prev15':round(prev15,3),'accel':round(accel,3),'vol_accel':round(vol_acc,3),'recent_volume':int(recent),'mfe60_pct':round(mfe,3),'mae60_pct':round(mae,3),'ret60_pct':round(r60,3),'future_5':int(mfe>=5),'future_10':int(mfe>=10)})
 return out

def evaluate(rows):
 grids=[]
 for amin in (0.15,0.3,0.5,0.8):
  for vmin in (1.2,1.5,2,3):
   for mature in (3,6,10):
    q=[r for r in rows if r['accel']>=amin and r['vol_accel']>=vmin and -2<=r['move_pct']<=mature]
    if len(q)<5: continue
    grids.append({'accel_min':amin,'vol_accel_min':vmin,'max_move_pct':mature,'signals':len(q),'future5_rate':round(sum(r['future_5'] for r in q)/len(q),4),'future10_rate':round(sum(r['future_10'] for r in q)/len(q),4),'avg_mfe60':round(sum(r['mfe60_pct'] for r in q)/len(q),3),'avg_mae60':round(sum(r['mae60_pct'] for r in q)/len(q),3),'avg_ret60':round(sum(r['ret60_pct'] for r in q)/len(q),3)})
 grids.sort(key=lambda z:(z['future10_rate'],z['future5_rate'],z['avg_ret60']),reverse=True); return grids

def main():
 if not KEY or not SECRET: raise SystemExit('Missing Alpaca secrets')
 if not SYMBOLS: raise SystemExit('Set LAB_SYMBOLS')
 rows=[]; errors=[]
 for n,s in enumerate(SYMBOLS,1):
  try: rows.extend(events(s,bars(s)))
  except Exception as e: errors.append({'ticker':s,'error':f'{type(e).__name__}: {e}'})
  print(n,s,'events',len(rows)); time.sleep(.12)
 fields=list(rows[0]) if rows else []
 with (OUT/'events.csv').open('w',newline='') as f:
  if fields: w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
 grids=evaluate(rows)
 with (OUT/'threshold_grid.csv').open('w',newline='') as f:
  if grids: w=csv.DictWriter(f,fieldnames=list(grids[0])); w.writeheader(); w.writerows(grids)
 summary={'symbols_requested':len(SYMBOLS),'events':len(rows),'errors':errors,'start':START,'end':END,'feed':FEED,'best_grids':grids[:20]}
 (OUT/'summary.json').write_text(json.dumps(summary,indent=2)); print(json.dumps(summary,indent=2))
if __name__=='__main__': main()
