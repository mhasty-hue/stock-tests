from __future__ import annotations

import csv
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

OUT = Path("results")
OUT.mkdir(exist_ok=True)

SEC_UA = os.getenv(
    "SEC_USER_AGENT",
    "MarketSignalLab/0.2 mhasty-hue@users.noreply.github.com",
)
SEC_HEADERS = {
    "User-Agent": SEC_UA,
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json, application/atom+xml, application/xml, text/xml, */*",
}
YH_HEADERS = {"User-Agent": "Mozilla/5.0 MarketSignalLab/0.2"}

FORMS = ["8-K", "10-Q", "10-K", "6-K"]
PRICE_MIN = float(os.getenv("IGNITION_MIN_PRICE", "2"))
PRICE_MAX = float(os.getenv("IGNITION_MAX_PRICE", "100"))
MIN_DOLLAR_VOL = float(os.getenv("IGNITION_MIN_DOLLAR_VOL", "5000000"))
MAX_FILINGS = int(os.getenv("IGNITION_MAX_FILINGS", "120"))

DIAGNOSTICS: list[dict[str, str]] = []


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


def diag(source: str, status: str, detail: str) -> None:
    DIAGNOSTICS.append({"source": source, "status": status, "detail": detail[:500]})
    print(f"[{source}] {status}: {detail[:300]}")


def get_with_retry(url: str, headers: dict[str, str], attempts: int = 4, timeout: int = 25):
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code not in (403, 429, 500, 502, 503, 504):
                break
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        if i < attempts - 1:
            time.sleep(1.5 * (2 ** i))
    raise RuntimeError(last or "request failed")


def sec_json(url: str):
    r = get_with_retry(url, SEC_HEADERS)
    try:
        return r.json()
    except ValueError as e:
        raise RuntimeError(f"SEC returned non-JSON content-type={r.headers.get('content-type')}") from e


def ticker_map():
    try:
        data = sec_json("https://www.sec.gov/files/company_tickers.json")
        out = {}
        for v in data.values():
            out[str(v["cik_str"]).zfill(10)] = (v["ticker"], v["title"])
        diag("sec_ticker_map", "ok", f"loaded {len(out)} tickers")
        return out
    except Exception as e:
        diag("sec_ticker_map", "error", str(e))
        return {}


def parse_atom(text: str, form: str):
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(text)
    rows = []
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
        if cik:
            rows.append((cik, form, updated, href, title))
    return rows


def latest_filings():
    seen = set()
    rows = []
    for form in FORMS:
        url = (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
            f"&type={form}&company=&dateb=&owner=include&start=0&count=100&output=atom"
        )
        try:
            r = get_with_retry(url, SEC_HEADERS)
            ctype = (r.headers.get("content-type") or "").lower()
            if "html" in ctype and "xml" not in ctype and "atom" not in ctype:
                raise RuntimeError(f"unexpected SEC content-type {ctype}")
            form_rows = parse_atom(r.text, form)
            diag(f"sec_atom_{form}", "ok", f"parsed {len(form_rows)} filings")
            for row in form_rows:
                key = (row[0], row[1], row[3])
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
        except Exception as e:
            diag(f"sec_atom_{form}", "error", str(e))
        time.sleep(0.2)
    rows.sort(key=lambda x: x[2], reverse=True)
    if not rows:
        diag("sec_filings", "degraded", "No filings available from live Atom feeds; workflow will still emit diagnostics.")
    return rows[:MAX_FILINGS]


def yahoo_bars(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{ticker}?interval=5m&range=5d&includePrePost=true&events=div,splits"
    )
    try:
        r = get_with_retry(url, YH_HEADERS, attempts=3, timeout=20)
        j = r.json().get("chart", {}).get("result")
        if not j:
            return None, "empty_chart"
        x = j[0]
        ts = x.get("timestamp") or []
        q = ((x.get("indicators") or {}).get("quote") or [{}])[0]
        meta = x.get("meta") or {}
        bars = []
        closes = q.get("close", [])
        volumes = q.get("volume", [])
        for i, t in enumerate(ts):
            if i >= len(closes):
                break
            c = closes[i]
            v = volumes[i] if i < len(volumes) else 0
            if c is None:
                continue
            bars.append((int(t), float(c), float(v or 0)))
        return (meta, bars), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def score_stock(meta, bars):
    if len(bars) < 8:
        return None
    px = bars[-1][1]
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not prev or float(prev) <= 0:
        prev = bars[max(0, len(bars) - 79)][1]
    move = (px / float(prev) - 1) * 100
    p15 = bars[-4][1] if len(bars) >= 4 else bars[0][1]
    vel = (px / p15 - 1) * 100 if p15 else 0.0
    recent_vol = sum(v for _, _, v in bars[-3:]) / 3
    prior_slice = bars[-15:-3] if len(bars) >= 15 else bars[:-3]
    prior_vol = sum(v for _, _, v in prior_slice) / max(1, len(prior_slice)) if prior_slice else 0
    vacc = recent_vol / prior_vol if prior_vol > 0 else 1.0
    lookback = bars[-78:]
    avg_bar_vol = sum(v for _, _, v in lookback) / max(1, len(lookback))
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


def write_outputs(rows, missing):
    fields = list(Row.__annotations__.keys())
    with open(OUT / "ignition_scan.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(asdict(row))
    with open(OUT / "ignition_scan.json", "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, indent=2)
    with open(OUT / "ignition_missing.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "reason"])
        w.writerows(missing)
    with open(OUT / "ignition_diagnostics.json", "w", encoding="utf-8") as f:
        json.dump(DIAGNOSTICS, f, indent=2)
    with open(OUT / "ignition_diagnostics.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["source", "status", "detail"])
        w.writeheader()
        w.writerows(DIAGNOSTICS)


def main():
    rows = []
    missing = []
    try:
        mapping = ticker_map()
        filings = latest_filings()
        for cik, form, updated, href, _title in filings:
            info = mapping.get(cik)
            if not info:
                continue
            ticker, company = info
            market_data, err = yahoo_bars(ticker)
            if not market_data:
                missing.append((ticker, err or "no_market_data"))
                continue
            meta, bars = market_data
            s = score_stock(meta, bars)
            if not s:
                missing.append((ticker, "insufficient_bars"))
                continue
            px, move, vel, vacc, dvol, opp, maturity, stage, action = s
            if not (PRICE_MIN <= px <= PRICE_MAX):
                continue
            if dvol < MIN_DOLLAR_VOL:
                continue
            rows.append(Row(ticker, company, form, updated, px, move, vel, vacc, dvol, opp, maturity, stage, action, href))
            time.sleep(0.10)
    except Exception as e:
        diag("scanner", "fatal_caught", f"{type(e).__name__}: {e}")
    finally:
        rows.sort(
            key=lambda r: (
                r.action == "BUY_CANDIDATE",
                r.action == "WATCH",
                r.opportunity_score - 0.45 * r.maturity_score,
            ),
            reverse=True,
        )
        write_outputs(rows, missing)

    diag("summary", "ok", f"emitted {len(rows)} candidates and {len(missing)} market-data misses")
    write_outputs(rows, missing)
    print(f"Candidates: {len(rows)}")
    for r in rows[:20]:
        print(
            f"{r.ticker:6} ${r.price:7.2f} {r.action:14} stage={r.stage:9} "
            f"move={r.move_pct:6.2f}% vel15={r.velocity_15m_pct:6.2f}% "
            f"vacc={r.volume_accel:5.2f} opp={r.opportunity_score:5.1f} mature={r.maturity_score:5.1f}"
        )


if __name__ == "__main__":
    main()
