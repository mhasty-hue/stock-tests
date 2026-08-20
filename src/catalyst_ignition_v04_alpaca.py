from __future__ import annotations

import csv
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

OUT = Path("results_v04_alpaca")
OUT.mkdir(exist_ok=True)

P_MIN = float(os.getenv("IGNITION_MIN_PRICE", "2"))
P_MAX = float(os.getenv("IGNITION_MAX_PRICE", "100"))
MAX_SYMBOLS = int(os.getenv("IGNITION_MAX_SYMBOLS", "8000"))
SNAP_BATCH = int(os.getenv("IGNITION_SNAPSHOT_BATCH", "150"))
TOP_DEEP = int(os.getenv("IGNITION_TOP_DEEP", "100"))
MIN_DVOL = float(os.getenv("IGNITION_MIN_DOLLAR_VOL", "1000000"))
ALPACA_KEY = os.getenv("ALPACA_API_KEY", "").strip()
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "").strip()
FEED = os.getenv("ALPACA_DATA_FEED", "iex")

ALPACA_HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Accept": "application/json",
}
WEB_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MarketSignalLab/0.4; research)"}
ET = ZoneInfo("America/New_York")


@dataclass
class Candidate:
    ticker: str
    price: float
    move_pct: float
    velocity_15m_pct: float
    volume_accel: float
    volume_pace: float
    est_dollar_volume: float
    risk_score: float
    opportunity_score: float
    maturity_score: float
    stage: str
    action: str
    whole_shares_with_100: int
    detected_at_et: str


def request(url: str, headers=None, tries: int = 3, timeout: int = 25):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers or WEB_HEADERS, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                last = RuntimeError(f"HTTP {r.status_code}: {url}")
                time.sleep(1.5 * (i + 1))
                continue
            r.raise_for_status()
            return r
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (i + 1))
    raise last or RuntimeError(f"request failed: {url}")


def chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_pipe_symbols(text: str, symbol_field: str) -> list[str]:
    lines = [x for x in text.splitlines() if x and not x.startswith("File Creation Time")]
    if not lines:
        return []
    header = lines[0].split("|")
    if symbol_field not in header:
        return []
    idx = header.index(symbol_field)
    out = []
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) <= idx:
            continue
        sym = parts[idx].strip().upper()
        if re.fullmatch(r"[A-Z]{1,5}", sym):
            out.append(sym)
    return out


def load_universe(diag: list[tuple[str, str]]) -> list[str]:
    specs = [
        ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "Symbol"),
        ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", "ACT Symbol"),
    ]
    symbols = set()
    for url, field in specs:
        try:
            got = parse_pipe_symbols(request(url, timeout=25).text, field)
            symbols.update(got)
            diag.append(("universe", f"loaded {len(got)} from {url}"))
        except Exception as exc:
            diag.append(("universe_error", f"{type(exc).__name__}: {exc}"))
    return sorted(symbols)[:MAX_SYMBOLS]


def elapsed_regular_fraction(now_et: datetime) -> float:
    open_dt = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_dt = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    if now_et <= open_dt:
        return 0.04
    if now_et >= close_dt:
        return 1.0
    minutes = (now_et - open_dt).total_seconds() / 60.0
    return max(0.04, min(1.0, minutes / 390.0))


def snapshots(batch: list[str]) -> dict:
    q = urlencode({"symbols": ",".join(batch), "feed": FEED})
    url = f"https://data.alpaca.markets/v2/stocks/snapshots?{q}"
    payload = request(url, headers=ALPACA_HEADERS).json()
    if "snapshots" in payload and isinstance(payload["snapshots"], dict):
        return payload["snapshots"]
    return payload if isinstance(payload, dict) else {}


def stage1(symbols: list[str], diag: list[tuple[str, str]]):
    rough = []
    now_et = datetime.now(timezone.utc).astimezone(ET)
    frac = elapsed_regular_fraction(now_et)
    scanned = 0
    usable = 0

    for batch in chunks(symbols, SNAP_BATCH):
        try:
            data = snapshots(batch)
            scanned += len(batch)
            for sym, snap in data.items():
                if not isinstance(snap, dict):
                    continue
                latest_trade = snap.get("latestTrade") or {}
                minute = snap.get("minuteBar") or {}
                day = snap.get("dailyBar") or {}
                prev = snap.get("prevDailyBar") or {}

                px = latest_trade.get("p") or minute.get("c") or day.get("c")
                prev_close = prev.get("c")
                today_vol = day.get("v") or 0
                prev_vol = prev.get("v") or 0
                minute_vol = minute.get("v") or 0
                if px is None or prev_close in (None, 0):
                    continue
                px = float(px)
                prev_close = float(prev_close)
                if not (P_MIN <= px <= P_MAX) or int(100 // px) < 1:
                    continue

                move = (px / prev_close - 1.0) * 100.0
                today_vol = float(today_vol or 0)
                prev_vol = float(prev_vol or 0)
                minute_vol = float(minute_vol or 0)
                pace = (today_vol / max(prev_vol * frac, 1.0)) if prev_vol > 0 else 0.0
                dvol = today_vol * px

                # Broad first-pass detector: meaningful price displacement OR abnormal volume pace.
                if abs(move) < 0.75 and pace < 1.35:
                    continue

                usable += 1
                price_component = max(move, 0.0) * 5.0
                pace_component = min(pace, 8.0) * 10.0
                liquidity_component = min(max(math.log10(max(dvol, 1.0)) - 5.0, 0.0), 4.0) * 4.0
                minute_component = min(math.log10(max(minute_vol, 1.0)), 6.0)
                rough_score = price_component + pace_component + liquidity_component + minute_component
                rough.append((rough_score, sym.upper(), px, move, pace, dvol))

            time.sleep(0.12)
        except Exception as exc:
            diag.append(("snapshot_batch_error", f"{batch[0] if batch else '?'}: {type(exc).__name__}: {exc}"))

    rough.sort(reverse=True)
    diag.append(("stage1", f"scanned={scanned}; rough={len(rough)}; usable={usable}; feed={FEED}"))
    return rough[:TOP_DEEP]


def bars_5m(symbol: str):
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=12)
    q = urlencode({
        "timeframe": "5Min",
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": now.isoformat().replace("+00:00", "Z"),
        "limit": "1000",
        "adjustment": "raw",
        "feed": FEED,
    })
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?{q}"
    payload = request(url, headers=ALPACA_HEADERS).json()
    return payload.get("bars") or []


def deep(symbol: str, rough_px: float, rough_move: float, pace: float, quote_dvol: float):
    raw = bars_5m(symbol)
    bars = []
    for x in raw:
        c = x.get("c")
        v = x.get("v")
        t = x.get("t")
        if c is None or t is None:
            continue
        bars.append((t, float(c), float(v or 0)))
    if len(bars) < 8:
        return None

    px = bars[-1][1]
    if not (P_MIN <= px <= P_MAX):
        return None
    p15 = bars[-4][1]
    vel = (px / p15 - 1.0) * 100.0 if p15 else 0.0
    move = rough_move

    recent_vol = sum(v for _, _, v in bars[-3:]) / 3.0
    prior = [v for _, _, v in bars[-15:-3]] if len(bars) >= 15 else [v for _, _, v in bars[:-3]]
    prior_vol = sum(prior) / len(prior) if prior else 0.0
    vacc = recent_vol / prior_vol if prior_vol > 0 else 1.0

    avg_bar_vol = sum(v for _, _, v in bars[-78:]) / min(78, len(bars))
    dvol = max(quote_dvol, avg_bar_vol * px * 78.0)
    if dvol < MIN_DVOL:
        return None

    rets = []
    for i in range(max(1, len(bars) - 20), len(bars)):
        a, b = bars[i - 1][1], bars[i][1]
        if a > 0:
            rets.append((b / a - 1.0) * 100.0)
    sigma = (sum(x * x for x in rets) / max(1, len(rets))) ** 0.5 if rets else 0.0

    risk = min(100.0, sigma * 18.0 + max(0.0, abs(move) - 12.0) * 2.5 + max(0.0, vacc - 10.0) * 2.0)
    motion = min(38.0, max(0.0, vel) * 12.0 + min(max(move, 0.0), 8.0) * 2.0)
    volume_score = min(30.0, max(0.0, math.log2(max(vacc, 0.25))) * 10.0 + 5.0)
    pace_score = min(12.0, max(0.0, pace - 1.0) * 6.0)
    liquidity = min(15.0, max(0.0, math.log10(max(dvol, 1.0)) - 6.0) * 7.5)
    early_bonus = 12.0 if 0.5 <= move <= 8.0 and vel > 0 else 0.0
    opportunity = min(100.0, motion + volume_score + pace_score + liquidity + early_bonus)

    maturity = min(100.0, max(0.0, move * 3.0) + max(0.0, vel - 2.5) * 5.0 + max(0.0, vacc - 8.0) * 1.7)

    if move < 0.5 and vacc < 2.0:
        stage = "DORMANT"
    elif maturity < 30:
        stage = "WAKING"
    elif maturity < 55:
        stage = "IGNITION"
    elif maturity < 75:
        stage = "CONFIRMED"
    else:
        stage = "MATURE"

    if maturity >= 75 or move >= 18:
        action = "PASS_TOO_LATE"
    elif risk >= 75:
        action = "PASS_HIGH_RISK"
    elif opportunity >= 72 and maturity < 60 and vel > 0:
        action = "BUY_CANDIDATE"
    elif opportunity >= 55 and maturity < 72:
        action = "WATCH"
    else:
        action = "PASS"

    detected = datetime.now(timezone.utc).astimezone(ET).isoformat(timespec="seconds")
    return Candidate(
        ticker=symbol,
        price=px,
        move_pct=move,
        velocity_15m_pct=vel,
        volume_accel=vacc,
        volume_pace=pace,
        est_dollar_volume=dvol,
        risk_score=risk,
        opportunity_score=opportunity,
        maturity_score=maturity,
        stage=stage,
        action=action,
        whole_shares_with_100=int(100 // px),
        detected_at_et=detected,
    )


def write_outputs(rows: list[Candidate], diag: list[tuple[str, str]], universe_count: int, stage1_count: int):
    rows.sort(
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
        w.writerows(diag)

    summary = {
        "provider": "alpaca",
        "feed": FEED,
        "universe_count": universe_count,
        "stage1_count": stage1_count,
        "deep_count": len(rows),
        "buy_candidates": sum(x.action == "BUY_CANDIDATE" for x in rows),
        "watch": sum(x.action == "WATCH" for x in rows),
        "too_late": sum(x.action == "PASS_TOO_LATE" for x in rows),
        "high_risk": sum(x.action == "PASS_HIGH_RISK" for x in rows),
        "run_time_et": datetime.now(timezone.utc).astimezone(ET).isoformat(timespec="seconds"),
    }
    with open(OUT / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    for x in rows[:30]:
        print(
            f"{x.ticker:6} ${x.price:7.2f} {x.action:15} {x.stage:9} "
            f"move={x.move_pct:6.2f}% vel15={x.velocity_15m_pct:6.2f}% "
            f"vacc={x.volume_accel:5.2f} pace={x.volume_pace:5.2f} "
            f"opp={x.opportunity_score:5.1f} mat={x.maturity_score:5.1f} risk={x.risk_score:5.1f}"
        )


def main():
    diag: list[tuple[str, str]] = []
    if not ALPACA_KEY or not ALPACA_SECRET:
        diag.append(("fatal", "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY"))
        write_outputs([], diag, 0, 0)
        raise SystemExit(2)

    symbols = load_universe(diag)
    rough = stage1(symbols, diag)
    rows = []
    for _, sym, px, move, pace, dvol in rough:
        try:
            c = deep(sym, px, move, pace, dvol)
            if c:
                rows.append(c)
        except Exception as exc:
            diag.append(("deep_error", f"{sym}: {type(exc).__name__}: {exc}"))
        time.sleep(0.08)

    write_outputs(rows, diag, len(symbols), len(rough))


if __name__ == "__main__":
    main()
