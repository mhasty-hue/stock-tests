from __future__ import annotations

import csv
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

import requests

SEC_UA = os.getenv("SEC_USER_AGENT", "market-signal-lab research contact@example.com")
HEADERS = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}
YH = {"User-Agent": "Mozilla/5.0 MarketSignalLab/0.1"}
OUT = Path("results")
OUT.mkdir(exist_ok=True)

FORMS = ["8-K", "10-Q", "10-K", "6-K"]
PRICE_MIN = float(os.getenv("IGNITION_MIN_PRICE", "2"))
PRICE_MAX = float(os.getenv("IGNITION_MAX_PRICE", "100"))
MIN_DOLLAR_VOL = float(os.getenv("IGNITION_MIN_DOLLAR_VOL", "5000000"))
MAX_FILINGS = int(os.getenv("IGNITION_MAX_FILINGS", "120"))

@dataclass
class Row:
    ticker: str
    company: str
    form: str
    filing_time: str
    price: float
    move_pct: float
    velocity_15m_pct: float
    volume_accel: float
    est_dollar_volume: float
    opportunity_score: float
    maturity_score: float
    stage: str
    action: str
    filing_url: str


def get_json(url: str, headers=None, timeout=20):
    r = requests.get(url, headers=headers or HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def ticker_map():
    data = get_json("https://www.sec.gov/files/company_tickers.json")
    out = {}
    for v in data.values():
        out[str(v["cik_str"]).zfill(10)] = (v["ticker"], v["title"])
    return out


def latest_filings():
    ns = {"a": "http://www.w3.org/2005/Atom"}
    seen = set()
    rows = []
    for form in FORMS:
        url = (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
            f"&type={form}&company=&dateb=&owner=include&start=0&count=100&output=atom"
        )
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        for e in root.findall("a:entry", ns):
            title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
            updated = (e.findtext("a:updated", default="", namespaces=ns) or "").strip()
            link = e.find("a:link", ns)
            href = link.attrib.get("href", "") if link is not None else ""
            cik = None
            for source in (href, title):
                m = re.search(r"CIK[=:\s]+0*(\d{4,10})", source, re.I)
                if m:
                    cik = m.group(1).zfill(10)
                    break
                m = re.search(r"\((\d{10})\)", source)
                if m:
                    cik = m.group(1)
                    break
            key = (cik, form, href)
            if cik and key not in seen:
                seen.add(key)
                rows.append((cik, form, updated, href, title))
    rows.sort(key=lambda x: x[2], reverse=True)
    return rows[:MAX_FILINGS]


def yahoo_bars(ticker: str):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=5m&range=5d&includePrePost=true&events=div,splits"
    r = requests.get(url, headers=YH, timeout=20)
    if r.status_code != 200:
        return None
    j = r.json().get("chart", {}).get("result")
    if not j:
        return None
    x = j[0]
    ts = x.get("timestamp") or []
    q = ((x.get("indicators") or {}).get("quote") or [{}])[0]
    meta = x.get("meta") or {}
    bars = []
    for i,t in enumerate(ts):
        try:
            c = q.get("close", [])[i]
            v = q.get("volume", [])[i]
        except Exception:
            continue
        if c is None:
            continue
        bars.append((int(t), float(c), float(v or 0)))
    return meta, bars


def score_stock(meta, bars):
    if len(bars) < 8:
        return None
    px = bars[-1][1]
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not prev or prev <= 0:
        prev = bars[max(0, len(bars)-79)][1]
    move = (px / float(prev) - 1) * 100
    p15 = bars[-4][1] if len(bars) >= 4 else bars[0][1]
    vel = (px / p15 - 1) * 100 if p15 else 0.0
    recent_vol = sum(v for _,_,v in bars[-3:]) / 3
    prior_slice = bars[-15:-3] if len(bars) >= 15 else bars[:-3]
    prior_vol = (sum(v for _,_,v in prior_slice) / max(1, len(prior_slice))) if prior_slice else 0
    vacc = recent_vol / prior_vol if prior_vol > 0 else 1.0
    avg_bar_vol = sum(v for _,_,v in bars[-78:]) / min(78, len(bars))
    dvol = avg_bar_vol * px * 78

    catalyst = 35.0
    movement = min(25.0, max(0.0, vel) * 7.0 + max(0.0, move) * 1.4)
    volume = min(25.0, max(0.0, math.log2(max(vacc, 0.25))) * 8.0 + 6.0)
    liquidity = min(15.0, max(0.0, math.log10(max(dvol, 1)) - 6.0) * 7.5)
    opp = max(0.0, min(100.0, catalyst + movement + volume + liquidity))

    maturity = max(0.0, move * 3.0) + max(0.0, vel - 3.0) * 4.0 + max(0.0, vacc - 8.0) * 2.0
    maturity = max(0.0, min(100.0, maturity))

    if move < 1 and vacc < 2:
        stage = "WAKING"
    elif maturity < 35:
        stage = "IGNITION"
    elif maturity < 70:
        stage = "CONFIRMED"
    else:
        stage = "MATURE"

    if opp >= 72 and maturity < 55 and move >= 1:
        action = "BUY_CANDIDATE"
    elif opp >= 58 and maturity < 70:
        action = "WATCH"
    elif maturity >= 70:
        action = "PASS_TOO_LATE"
    else:
        action = "PASS"
    return px, move, vel, vacc, dvol, opp, maturity, stage, action


def main():
    mapping = ticker_map()
    filings = latest_filings()
    out = []
    missing = []
    for cik, form, updated, href, title in filings:
        info = mapping.get(cik)
        if not info:
            continue
        ticker, company = info
        try:
            y = yahoo_bars(ticker)
            if not y:
                missing.append((ticker, "no_market_data"))
                continue
            meta, bars = y
            s = score_stock(meta, bars)
            if not s:
                continue
            px, move, vel, vacc, dvol, opp, maturity, stage, action = s
            if not (PRICE_MIN <= px <= PRICE_MAX):
                continue
            if dvol < MIN_DOLLAR_VOL:
                continue
            out.append(Row(ticker, company, form, updated, px, move, vel, vacc, dvol, opp, maturity, stage, action, href))
        except Exception as e:
            missing.append((ticker, type(e).__name__))
        time.sleep(0.08)

    out.sort(key=lambda r: (r.action == "BUY_CANDIDATE", r.action == "WATCH", r.opportunity_score - .45*r.maturity_score), reverse=True)
    fields = list(Row.__annotations__.keys())
    with open(OUT / "ignition_scan.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in out:
            w.writerow(asdict(r))
    with open(OUT / "ignition_scan.json", "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in out], f, indent=2)
    with open(OUT / "ignition_missing.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["ticker", "reason"]); w.writerows(missing)

    print(f"Scanned {len(filings)} recent filings; {len(out)} affordable/liquid names survived filters.")
    for r in out[:20]:
        print(f"{r.ticker:6} ${r.price:7.2f} {r.action:14} stage={r.stage:9} move={r.move_pct:6.2f}% vel15={r.velocity_15m_pct:6.2f}% vacc={r.volume_accel:5.2f} opp={r.opportunity_score:5.1f} mature={r.maturity_score:5.1f}")

if __name__ == "__main__":
    main()
