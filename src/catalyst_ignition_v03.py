from __future__ import annotations

import csv, json, math, os, time
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlencode
import requests

OUT=Path('results_v03'); OUT.mkdir(exist_ok=True)
P_MIN=float(os.getenv('IGNITION_MIN_PRICE','2')); P_MAX=float(os.getenv('IGNITION_MAX_PRICE','100'))
MAX_SYMBOLS=int(os.getenv('IGNITION_MAX_SYMBOLS','5000')); TOP_DEEP=int(os.getenv('IGNITION_TOP_DEEP','120'))
MIN_DVOL=float(os.getenv('IGNITION_MIN_DOLLAR_VOL','1000000')); BATCH=int(os.getenv('IGNITION_BATCH_SIZE','50'))
H={'User-Agent':'Mozilla/5.0 (compatible; MarketSignalLab/0.3; research)'}

@dataclass
class C:
    ticker:str; price:float; move_pct:float; velocity_15m_pct:float; volume_accel:float; quote_volume_ratio:float
    est_dollar_volume:float; risk_score:float; opportunity_score:float; maturity_score:float; stage:str; action:str
    whole_shares_with_100:int

def req(url,tries=3,timeout=20):
    last=None
    for i in range(tries):
        try:
            r=requests.get(url,headers=H,timeout=timeout)
            if r.status_code in (403,429,500,502,503,504):
                last=RuntimeError(f'HTTP {r.status_code} {url}'); time.sleep(1.5*(i+1)); continue
            r.raise_for_status(); return r
        except Exception as e:
            last=e; time.sleep(1.5*(i+1))
    raise last or RuntimeError('request failed')

def parse_pipe(txt,field):
    lines=[x for x in txt.splitlines() if x and not x.startswith('File Creation Time')]
    if not lines:return []
    h=lines[0].split('|')
    if field not in h:return []
    idx=h.index(field); out=[]
    for line in lines[1:]:
        p=line.split('|')
        if len(p)<=idx:continue
        s=p[idx].strip().upper()
        if s.isalpha() and 1<=len(s)<=5: out.append(s)
    return out

def universe(diag):
    specs=[('https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt','Symbol'),('https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt','ACT Symbol')]
    syms=set()
    for u,f in specs:
        try:
            got=parse_pipe(req(u,timeout=25).text,f); syms.update(got); diag.append(('universe',f'{len(got)} {u}'))
        except Exception as e: diag.append(('universe_error',f'{type(e).__name__}: {e}'))
    return sorted(syms)[:MAX_SYMBOLS]

def quote_batch(batch):
    q=urlencode({'symbols':','.join(batch),'formatted':'false','region':'US','lang':'en-US'})
    u='https://query1.finance.yahoo.com/v7/finance/quote?'+q
    return req(u,tries=2,timeout=20).json().get('quoteResponse',{}).get('result',[])

def stage1(syms,diag):
    rough=[]
    for i in range(0,len(syms),BATCH):
        b=syms[i:i+BATCH]
        try:
            for x in quote_batch(b):
                s=(x.get('symbol') or '').upper(); px=x.get('regularMarketPrice'); ch=x.get('regularMarketChangePercent')
                vol=x.get('regularMarketVolume') or 0; av=x.get('averageDailyVolume10Day') or x.get('averageDailyVolume3Month') or 0
                if not s or px is None or ch is None: continue
                px=float(px); ch=float(ch); vol=float(vol or 0); av=float(av or 0)
                if not (P_MIN<=px<=P_MAX) or int(100//px)<1: continue
                qvr=vol/av if av>0 else 0.0; dvol=vol*px
                # Broad awakening threshold. We intentionally keep this permissive.
                if abs(ch)>=1.0 or qvr>=0.25:
                    rough_score=max(ch,0)*4 + min(qvr,3)*12 + min(max(math.log10(max(dvol,1))-5,0),4)*4
                    rough.append((rough_score,s,px,ch,qvr,dvol))
            time.sleep(.12)
        except Exception as e: diag.append(('quote_batch_error',f'{b[0] if b else "?"}: {type(e).__name__}: {e}'))
    rough.sort(reverse=True); diag.append(('stage1',f'{len(rough)} rough candidates from {len(syms)} symbols'))
    return rough[:TOP_DEEP]

def chart(sym):
    q=urlencode({'range':'1d','interval':'5m','includePrePost':'true','events':'div,splits'})
    u=f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?{q}'
    j=req(u,tries=2,timeout=20).json().get('chart',{}).get('result')
    if not j:return None
    x=j[0]; ts=x.get('timestamp') or []; qt=((x.get('indicators') or {}).get('quote') or [{}])[0]
    cl=qt.get('close') or []; vo=qt.get('volume') or []; bars=[]
    for i,t in enumerate(ts):
        if i>=len(cl) or cl[i] is None: continue
        v=vo[i] if i<len(vo) and vo[i] is not None else 0
        bars.append((int(t),float(cl[i]),float(v)))
    return x.get('meta') or {},bars

def deep(sym,quote_px,quote_ch,qvr,quote_dvol):
    z=chart(sym)
    if not z:return None
    meta,b=z
    if len(b)<8:return None
    px=b[-1][1]
    if not(P_MIN<=px<=P_MAX):return None
    p15=b[-4][1]; vel=(px/p15-1)*100 if p15 else 0
    prev=meta.get('chartPreviousClose') or meta.get('previousClose') or b[0][1]; move=(px/float(prev)-1)*100
    rv=sum(v for _,_,v in b[-3:])/3
    prior=[v for _,_,v in b[-15:-3]] if len(b)>=15 else [v for _,_,v in b[:-3]]
    pv=sum(prior)/len(prior) if prior else 0; vacc=rv/pv if pv>0 else 1
    av=sum(v for _,_,v in b[-78:])/min(78,len(b)); dvol=max(quote_dvol,av*px*78)
    if dvol<MIN_DVOL:return None
    rr=[]
    for i in range(max(1,len(b)-20),len(b)):
        a=b[i-1][1]; c=b[i][1]
        if a>0: rr.append((c/a-1)*100)
    sig=(sum(x*x for x in rr)/max(1,len(rr)))**.5 if rr else 0
    risk=min(100,sig*18+max(0,abs(move)-12)*2.5+max(0,vacc-10)*2)
    motion=min(38,max(0,vel)*12+min(max(move,0),8)*2)
    vscore=min(30,max(0,math.log2(max(vacc,.25)))*10+5)
    liq=min(15,max(0,math.log10(max(dvol,1))-6)*7.5)
    early=12 if .5<=move<=8 and vel>0 else 0
    opp=min(100,motion+vscore+liq+early)
    mat=min(100,max(0,move*3)+max(0,vel-2.5)*5+max(0,vacc-8)*1.7)
    if move<.5 and vacc<2: stage='DORMANT'
    elif mat<30: stage='WAKING'
    elif mat<55: stage='IGNITION'
    elif mat<75: stage='CONFIRMED'
    else: stage='MATURE'
    if mat>=75 or move>=18: act='PASS_TOO_LATE'
    elif risk>=75: act='PASS_HIGH_RISK'
    elif opp>=72 and mat<60 and vel>0: act='BUY_CANDIDATE'
    elif opp>=55 and mat<72: act='WATCH'
    else: act='PASS'
    return C(sym,px,move,vel,vacc,qvr,dvol,risk,opp,mat,stage,act,int(100//px))

def main():
    diag=[]; syms=universe(diag); rough=stage1(syms,diag); out=[]
    for _,s,px,ch,qvr,dv in rough:
        try:
            c=deep(s,px,ch,qvr,dv)
            if c: out.append(c)
        except Exception as e: diag.append(('chart_error',f'{s}: {type(e).__name__}: {e}'))
        time.sleep(.08)
    out.sort(key=lambda c:(c.action=='BUY_CANDIDATE',c.action=='WATCH',c.opportunity_score-.45*c.maturity_score-.25*c.risk_score),reverse=True)
    flds=list(C.__annotations__.keys())
    with open(OUT/'awakening_scan.csv','w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=flds); w.writeheader(); [w.writerow(asdict(x)) for x in out]
    with open(OUT/'diagnostics.csv','w',newline='',encoding='utf-8') as f:
        w=csv.writer(f); w.writerow(['kind','detail']); w.writerows(diag)
    summary={'universe_count':len(syms),'stage1_count':len(rough),'deep_count':len(out),'buy_candidates':sum(x.action=='BUY_CANDIDATE' for x in out),'watch':sum(x.action=='WATCH' for x in out),'too_late':sum(x.action=='PASS_TOO_LATE' for x in out),'high_risk':sum(x.action=='PASS_HIGH_RISK' for x in out)}
    with open(OUT/'summary.json','w') as f: json.dump(summary,f,indent=2)
    print(json.dumps(summary,indent=2))
    for x in out[:30]: print(f'{x.ticker:6} ${x.price:7.2f} {x.action:15} {x.stage:9} move={x.move_pct:6.2f}% vel15={x.velocity_15m_pct:6.2f}% vacc={x.volume_accel:5.2f} qvr={x.quote_volume_ratio:5.2f} opp={x.opportunity_score:5.1f} mat={x.maturity_score:5.1f} risk={x.risk_score:5.1f}')

if __name__=='__main__': main()
