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
from typing import Iterable

import requests

OUT = Path("results_v02")
OUT.mkdir(exist_ok=True)

PRICE_MIN = float(os.getenv("IGNITION_MIN_PRICE", "2"))
PRICE_MAX = float(os.getenv("IGNITION_MAX_PRICE", "100"))
MIN_DOLLAR_VOL = float(os.getenv("IGNITION_MIN_DOLLAR_VOL", "1000000"))
MAX_SYMBOLS = int(os.getenv("IGNITION_MAX_SYMBOLS", "2500"))
MAX_CATALYSTS = int(os.getenv("IGNITION_MAX_CATALYSTS", "120"))
BATCH_SIZE = int(os.getenv("IGNITION_BATCH_SIZE", "30"))
SEC_UA = os.getenv("SEC_USER_AGENT", "MarketSignalLab research contact@example.com")

SEC_HEADERS = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate", "Host": "www.sec.gov"}
WEB_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MarketSignalLab/0.2; research)"}


@dataclass
class Candidate:
    ticker: str
    price: float
    move_pct: float
    velocity_15m_pct: float
    volume_accel: float
    est_dollar_volume: float
    risk_score: float
    catalyst_score: float
    opportunity_score: float
    maturity_score: float
    stage: str
    action: str
    whole_shares_with_100: int
    catalyst_form: str = ""
    catalyst_time: str = ""
    catalyst_url: str = ""


def request(url: str, headers=None, timeout=20, tries=3):
    err = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers or WEB_HEADERS, timeout=timeout)
            if r.status_code in (403, 429, 500, 502, 503, 504):
                err = RuntimeError(f"HTTP {r.status_code} for {url}")
                time.sleep(1.5 * (i + 1))
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            err = e
            time.sleep(1.5 * (i + 1))
    raise err or RuntimeError(f"request failed: {url}")


def chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_pipe_symbols(text: str, symbol_field: str) -> list[str]:
    lines = [x for x in text.splitlines() if x and not x.startswith("File Creation Time")]
    if not lines:
        return []
    header = lines[0].split("|")
    try:
        idx = header.index(symbol_field)
    except ValueError:
        return []
    out = []
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) <= idx:
            continue
        sym = parts[idx].strip().upper()
        if not sym or sym.startswith("$"):
            continue
        if re.fullmatch(r"[A-Z]{1,5}", sym):
            out.append(sym)
    return out


def load_universe(diagnostics: list[tuple[str, str]]) -> list[str]:
    urls = [
        ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "Symbol"),
        ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", "ACT Symbol"),
    ]
    symbols = set()
    for url, field in urls:
        try:
            r = request(url, timeout=25)
            got = parse_pipe_symbols(r.text, field)
            symbols.update(got)
            diagnostics.append(("universe", f"loaded {len(got)} from {url}"))
        except Exception as e:
            diagnostics.append(("universe_error", f"{type(e).__name__}: {e}"))
    return sorted(symbols)[:MAX_SYMBOLS]


def yahoo_spark(symbols: list[str]):
    joined = ",".join(symbols)
    url = (
        "https://query1.finance.yahoo.com/v7/finance/spark"
        f"?symbols={joined}&range=1d&interval=5m&indicators=close,volume"
        "&includeTimestamps=true&includePrePost=true"
    )
    r = request(url, headers=WEB_HEADERS, timeout=25, tries=2)
    return r.json().get("spark", {}).get("result") or []


def score_series(ticker: str, response: dict):
    meta = response.get("meta") or {}
    timestamps = response.get("timestamp") or []
    quote = ((response.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    vols = quote.get("volume") or []
    bars = []
    for i, ts in enumerate(timestamps):
        if i >= len(closes):
            break
        c = closes[i]
        if c is None:
            continue
        v = vols[i] if i < len(vols) and vols[i] is not None else 0
        bars.append((int(ts), float(c), float(v)))
    if len(bars) < 8:
        return None

    px = bars[-1][1]
    if not (PRICE_MIN <= px <= PRICE_MAX):
        return None
    shares = int(100 // px)
    if shares < 1:
        return None

    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not prev or prev <= 0:
        prev = bars[0][1]
    move = (px / float(prev) - 1) * 100
    p15 = bars[-4][1]
    vel = (px / p15 - 1) * 100 if p15 else 0.0

    recent = [v for _, _, v in bars[-3:]]
    prior = [v for _, _, v in bars[-15:-3]] if len(bars) >= 15 else [v for _, _, v in bars[:-3]]
    recent_vol = sum(recent) / max(1, len(recent))
    prior_vol = sum(prior) / max(1, len(prior)) if prior else 0
    vacc = recent_vol / prior_vol if prior_vol > 0 else 1.0

    avg_vol = sum(v for _, _, v in bars[-78:]) / min(78, len(bars))
    dvol = avg_vol * px * 78
    if dvol < MIN_DOLLAR_VOL:
        return None

    rets = []
    for i in range(max(1, len(bars) - 20), len(bars)):
        a, b = bars[i - 1][1], bars[i][1]
        if a > 0:
            rets.append((b / a - 1) * 100)
    sigma = (sum(x * x for x in rets) / max(1, len(rets))) ** 0.5 if rets else 0.0
    risk = min(100.0, sigma * 18 + max(0.0, abs(move) - 12) * 2.5 + max(0.0, vacc - 10) * 2.0)

    motion = min(35.0, max(0.0, vel) * 11.0 + min(max(move, 0.0), 8.0) * 1.8)
    volscore = min(30.0, max(0.0, math.log2(max(vacc, 0.25))) * 10.0 + 5.0)
    liquidity = min(15.0, max(0.0, math.log10(max(dvol, 1)) - 6.0) * 7.5)
    early_bonus = 12.0 if 0.5 <= move <= 8 and vel > 0 else 0.0
    base_opp = min(100.0, motion + volscore + liquidity + early_bonus)

    maturity = max(0.0, move * 3.0) + max(0.0, vel - 2.5) * 5.0 + max(0.0, vacc - 8.0) * 1.7
    maturity = min(100.0, maturity)

    if move < 0.5 and vacc < 2:
        stage = "DORMANT"
    elif maturity < 30:
        stage = "WAKING"
    elif maturity < 55:
        stage = "IGNITION"
    elif maturity < 75:
        stage = "CONFIRMED"
    else:
        stage = "MATURE"

    return Candidate(
        ticker=ticker,
        price=px,
        move_pct=move,
        velocity_15m_pct=vel,
        volume_accel=vacc,
        est_dollar_volume=dvol,
        risk_score=risk,
        catalyst_score=0.0,
        opportunity_score=base_opp,
        maturity_score=maturity,
        stage=stage,
        action="PASS",
        whole_shares_with_100=shares,
    )


def market_first(symbols: list[str], diagnostics: list[tuple[str, str]]) -> dict[str, Candidate]:
    found: dict[str, Candidate] = {}
    for batch in chunks(symbols, BATCH_SIZE):
        try:
            rows = yahoo_spark(batch)
            for item in rows:
                ticker = (item.get("symbol") or "").upper()
                responses = item.get("response") or []
                if not ticker or not responses:
                    continue
                c = score_series(ticker, responses[0])
                if not c:
                    continue
                if c.velocity_15m_pct >= 0.35 or c.volume_accel >= 1.8 or c.move_pct >= 1.0:
                    found[ticker] = c
            time.sleep(0.15)
        except Exception as e:
            diagnostics.append(("market_batch_error", f"{batch[0] if batch else '?'}: {type(e).__name__}: {e}"))
    diagnostics.append(("market_first", f"{len(found)} awakening names from {len(symbols)} symbols"))
    return found


def sec_catalysts(diagnostics: list[tuple[str, str]]):
    ticker_map = {}
    catalysts = {}
    try:
        r = request("https://www.sec.gov/files/company_tickers.json", headers=SEC_HEADERS, timeout=20)
        data = r.json()
        for v in data.values():
            ticker_map[str(v["cik_str"]).zfill(10)] = v["ticker"].upper()
    except Exception as e:
        diagnostics.append(("sec_ticker_map_error", f"{type(e).__name__}: {e}"))
        return catalysts

    ns = {"a": "http://www.w3.org/2005/Atom"}
    for form, weight in (("8-K", 30.0), ("6-K", 25.0), ("10-Q", 18.0), ("10-K", 15.0)):
        try:
            url = (
                "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
                f"&type={form}&company=&dateb=&owner=include&start=0&count=100&output=atom"
            )
            r = request(url, headers=SEC_HEADERS, timeout=20)
            root = ET.fromstring(r.text)
            count = 0
            for e in root.findall("a:entry", ns):
                title = e.findtext("a:title", default="", namespaces=ns) or ""
                updated = e.findtext("a:updated", default="", namespaces=ns) or ""
                link = e.find("a:link", ns)
                href = link.attrib.get("href", "") if link is not None else ""
                m = re.search(r"CIK[=:\s]+0*(\d{4,10})", href + " " + title, re.I)
                if not m:
                    m = re.search(r"\((\d{10})\)", title)
                if not m:
                    continue
                cik = m.group(1).zfill(10)
                ticker = ticker_map.get(cik)
                if not ticker:
                    continue
                catalysts[ticker] = {"score": weight, "form": form, "time": updated, "url": href}
                count += 1
                if len(catalysts) >= MAX_CATALYSTS:
                    break
            diagnostics.append(("sec_feed", f"{form}: {count}"))
        except Exception as e:
            diagnostics.append(("sec_feed_error", f"{form}: {type(e).__name__}: {e}"))
    return catalysts


def finalize(candidates: dict[str, Candidate], catalysts: dict[str, dict]):
    for ticker, c in candidates.items():
        cat = catalysts.get(ticker)
        if cat:
            c.catalyst_score = float(cat["score"])
            c.catalyst_form = str(cat["form"])
            c.catalyst_time = str(cat["time"])
            c.catalyst_url = str(cat["url"])
        c.opportunity_score = min(100.0, c.opportunity_score + c.catalyst_score)

        if c.maturity_score >= 75 or c.move_pct >= 18:
            c.action = "PASS_TOO_LATE"
        elif c.risk_score >= 75:
            c.action = "PASS_HIGH_RISK"
        elif c.opportunity_score >= 72 and c.maturity_score < 60 and c.velocity_15m_pct > 0:
            c.action = "BUY_CANDIDATE"
        elif c.opportunity_score >= 55 and c.maturity_score < 72:
            c.action = "WATCH"
        else:
            c.action = "PASS"


def write_outputs(candidates: dict[str, Candidate], diagnostics: list[tuple[str, str]], universe_count: int, catalyst_count: int):
    rows = sorted(
        candidates.values(),
        key=lambda c: (
            c.action == "BUY_CANDIDATE",
            c.action == "WATCH",
            c.opportunity_score - 0.45 * c.maturity_score - 0.25 * c.risk_score,
        ),
        reverse=True,
    )
    fields = list(Candidate.__annotations__.keys())
    with open(OUT / "awakening_scan.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(asdict(row))
    with open(OUT / "awakening_scan.json", "w", encoding="utf-8") as f:
        json.dump([asdict(x) for x in rows], f, indent=2)
    with open(OUT / "diagnostics.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["kind", "detail"])
        w.writerows(diagnostics)
    summary = {
        "universe_count": universe_count,
        "catalyst_count": catalyst_count,
        "candidate_count": len(rows),
        "buy_candidates": sum(x.action == "BUY_CANDIDATE" for x in rows),
        "watch": sum(x.action == "WATCH" for x in rows),
        "too_late": sum(x.action == "PASS_TOO_LATE" for x in rows),
        "high_risk": sum(x.action == "PASS_HIGH_RISK" for x in rows),
    }
    with open(OUT / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))
    for x in rows[:25]:
        print(
            f"{x.ticker:6} ${x.price:7.2f} {x.action:15} stage={x.stage:9} "
            f"move={x.move_pct:6.2f}% vel15={x.velocity_15m_pct:6.2f}% "
            f"vacc={x.volume_accel:5.2f} opp={x.opportunity_score:5.1f} "
            f"maturity={x.maturity_score:5.1f} risk={x.risk_score:5.1f}"
        )


def main():
    diagnostics: list[tuple[str, str]] = []
    universe = load_universe(diagnostics)
    candidates = market_first(universe, diagnostics) if universe else {}
    catalysts = sec_catalysts(diagnostics)
    finalize(candidates, catalysts)
    write_outputs(candidates, diagnostics, len(universe), len(catalysts))


if __name__ == "__main__":
    main()
