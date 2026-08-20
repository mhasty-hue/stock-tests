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

OUT = Path("results_v05_acceleration")
OUT.mkdir(exist_ok=True)

P_MIN = float(os.getenv("IGNITION_MIN_PRICE", "2"))
P_MAX = float(os.getenv("IGNITION_MAX_PRICE", "100"))
MAX_SYMBOLS = int(os.getenv("IGNITION_MAX_SYMBOLS", "8000"))
SNAP_BATCH = int(os.getenv("IGNITION_SNAPSHOT_BATCH", "150"))
TOP_DEEP = int(os.getenv("IGNITION_TOP_DEEP", "140"))
MIN_DVOL = float(os.getenv("IGNITION_MIN_DOLLAR_VOL", "750000"))
ALPACA_KEY = os.getenv("ALPACA_API_KEY", "").strip()
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "").strip()
FEED = os.getenv("ALPACA_DATA_FEED", "iex")

ALPACA_HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Accept": "application/json",
}
WEB_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MarketSignalLab/0.5; research)"}
ET = ZoneInfo("America/New_York")


@dataclass
class Candidate:
    ticker: str
    price: float
    move_pct: float
    velocity_5m_pct: float
    velocity_15m_pct: float
    velocity_prev15m_pct: float
    price_accel_pct: float
    volume_accel: float
    volume_impulse: float
    volume_pace: float
    est_dollar_volume: float
    risk_score: float
    opportunity_score: float
    maturity_score: float
    momentum_phase: str
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
                time.sleep(1.25 * (i + 1))
                continue
            r.raise_for_status()
            return r
        except Exception as exc:
            last = exc
            time.sleep(1.25 * (i + 1))
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


def load_universe(diag):
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
    return max(0.04, min(1.0, (now_et - open_dt).total_seconds() / 60.0 / 390.0))


def snapshots(batch):
    q = urlencode({"symbols": ",".join(batch), "feed": FEED})
    payload = request(f"https://data.alpaca.markets/v2/stocks/snapshots?{q}", headers=ALPACA_HEADERS).json()
    if isinstance(payload.get("snapshots"), dict):
        return payload["snapshots"]
    return payload if isinstance(payload, dict) else {}


def stage1(symbols, diag):
    rough = []
    now_et = datetime.now(timezone.utc).astimezone(ET)
    frac = elapsed_regular_fraction(now_et)
    scanned = 0
    for batch in chunks(symbols, SNAP_BATCH):
        try:
            data = snapshots(batch)
            scanned += len(batch)
            for sym, snap in data.items():
                if not isinstance(snap, dict):
                    continue
                lt = snap.get("latestTrade") or {}
                minute = snap.get("minuteBar") or {}
                day = snap.get("dailyBar") or {}
                prev = snap.get("prevDailyBar") or {}
                px = lt.get("p") or minute.get("c") or day.get("c")
                prev_close = prev.get("c")
                today_vol = float(day.get("v") or 0)
                prev_vol = float(prev.get("v") or 0)
                minute_vol = float(minute.get("v") or 0)
                if px is None or prev_close in (None, 0):
                    continue
                px = float(px)
                prev_close = float(prev_close)
                if not (P_MIN <= px <= P_MAX) or int(100 // px) < 1:
                    continue
                move = (px / prev_close - 1.0) * 100.0
                pace = today_vol / max(prev_vol * frac, 1.0) if prev_vol > 0 else 0.0
                dvol = today_vol * px
                if abs(move) < 0.45 and pace < 1.15:
                    continue
                score = max(move, 0.0) * 4.0 + min(pace, 8.0) * 12.0
                score += min(max(math.log10(max(dvol, 1.0)) - 5.0, 0.0), 4.0) * 4.0
                score += min(math.log10(max(minute_vol, 1.0)), 6.0)
                rough.append((score, sym.upper(), px, move, pace, dvol))
            time.sleep(0.1)
        except Exception as exc:
            diag.append(("snapshot_batch_error", f"{batch[0] if batch else '?'}: {type(exc).__name__}: {exc}"))
    rough.sort(reverse=True)
    diag.append(("stage1", f"scanned={scanned}; rough={len(rough)}; selected={min(len(rough), TOP_DEEP)}; feed={FEED}"))
    return rough[:TOP_DEEP]


def bars_5m(symbol):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=3)
    q = urlencode({
        "timeframe": "5Min",
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": now.isoformat().replace("+00:00", "Z"),
        "limit": "1000",
        "adjustment": "raw",
        "feed": FEED,
    })
    payload = request(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?{q}", headers=ALPACA_HEADERS).json()
    return payload.get("bars") or []


def pct(a, b):
    return (a / b - 1.0) * 100.0 if b else 0.0


def classify_phase(move, vel5, vel15, prev15, accel, vacc, impulse):
    if move >= 18 or (vel15 >= 6 and impulse >= 3.5):
        return "EXHAUSTED"
    if vel15 > 0.5 and accel > 0.35 and impulse >= 1.25:
        return "ACCELERATING"
    if vel15 > 0.35 and vacc >= 1.6 and move < 10:
        return "IGNITING"
    if vel15 > 0.15 and move > 1 and accel > -0.35:
        return "EXPANDING"
    if move > 2 and (accel < -0.45 or vel5 < 0 or impulse < 0.7):
        return "DECELERATING"
    if move > 8 and vel15 <= 0:
        return "EXHAUSTED"
    return "WAKING"


def deep(symbol, rough_move, pace, quote_dvol):
    raw = bars_5m(symbol)
    bars = []
    for x in raw:
        if x.get("c") is None or x.get("t") is None:
            continue
        bars.append((x["t"], float(x["c"]), float(x.get("v") or 0)))
    if len(bars) < 14:
        return None

    px = bars[-1][1]
    if not (P_MIN <= px <= P_MAX):
        return None

    vel5 = pct(px, bars[-2][1])
    vel15 = pct(px, bars[-4][1])
    prev15 = pct(bars[-4][1], bars[-7][1]) if len(bars) >= 7 else 0.0
    accel = vel15 - prev15

    recent3 = [v for _, _, v in bars[-3:]]
    prev3 = [v for _, _, v in bars[-6:-3]]
    prior12 = [v for _, _, v in bars[-18:-6]] if len(bars) >= 18 else [v for _, _, v in bars[:-6]]
    recent_avg = sum(recent3) / max(1, len(recent3))
    prev3_avg = sum(prev3) / max(1, len(prev3))
    prior_avg = sum(prior12) / max(1, len(prior12)) if prior12 else 0.0
    impulse = recent_avg / prev3_avg if prev3_avg > 0 else 1.0
    vacc = recent_avg / prior_avg if prior_avg > 0 else 1.0

    avg_bar_vol = sum(v for _, _, v in bars[-78:]) / min(78, len(bars))
    dvol = max(quote_dvol, avg_bar_vol * px * 78.0)
    if dvol < MIN_DVOL:
        return None

    rets = []
    for i in range(max(1, len(bars) - 20), len(bars)):
        rets.append(pct(bars[i][1], bars[i - 1][1]))
    sigma = (sum(x * x for x in rets) / max(1, len(rets))) ** 0.5 if rets else 0.0

    move = rough_move
    phase = classify_phase(move, vel5, vel15, prev15, accel, vacc, impulse)

    risk = sigma * 17.0 + max(0.0, abs(move) - 12.0) * 2.7
    risk += max(0.0, vacc - 10.0) * 2.0
    if phase == "EXHAUSTED":
        risk += 18.0
    risk = min(100.0, risk)

    motion = min(30.0, max(0.0, vel15) * 10.0 + max(0.0, vel5) * 5.0)
    accel_score = min(22.0, max(0.0, accel) * 18.0)
    vol_score = min(22.0, max(0.0, math.log2(max(vacc, 0.25))) * 7.0 + max(0.0, impulse - 1.0) * 5.0)
    pace_score = min(10.0, max(0.0, pace - 1.0) * 5.0)
    liquidity = min(12.0, max(0.0, math.log10(max(dvol, 1.0)) - 6.0) * 6.0)
    stage_bonus = {"ACCELERATING": 18.0, "IGNITING": 14.0, "EXPANDING": 7.0, "WAKING": 3.0}.get(phase, 0.0)
    decel_penalty = 22.0 if phase == "DECELERATING" else 35.0 if phase == "EXHAUSTED" else 0.0
    opportunity = min(100.0, max(0.0, motion + accel_score + vol_score + pace_score + liquidity + stage_bonus - decel_penalty))

    maturity = max(0.0, move * 2.7) + max(0.0, vel15 - 3.0) * 4.0
    maturity += max(0.0, -accel) * 9.0 if move > 3 else 0.0
    if phase == "DECELERATING":
        maturity += 18.0
    if phase == "EXHAUSTED":
        maturity += 32.0
    maturity = min(100.0, maturity)

    if move >= 18 or maturity >= 82 or phase == "EXHAUSTED":
        action = "PASS_TOO_LATE"
    elif risk >= 78:
        action = "PASS_HIGH_RISK"
    elif phase in ("ACCELERATING", "IGNITING") and opportunity >= 68 and maturity < 58 and vel15 > 0:
        action = "BUY_CANDIDATE"
    elif phase in ("ACCELERATING", "IGNITING", "EXPANDING", "WAKING") and opportunity >= 48 and maturity < 68:
        action = "WATCH"
    else:
        action = "PASS"

    return Candidate(
        ticker=symbol,
        price=px,
        move_pct=move,
        velocity_5m_pct=vel5,
        velocity_15m_pct=vel15,
        velocity_prev15m_pct=prev15,
        price_accel_pct=accel,
        volume_accel=vacc,
        volume_impulse=impulse,
        volume_pace=pace,
        est_dollar_volume=dvol,
        risk_score=risk,
        opportunity_score=opportunity,
        maturity_score=maturity,
        momentum_phase=phase,
        action=action,
        whole_shares_with_100=int(100 // px),
        detected_at_et=datetime.now(timezone.utc).astimezone(ET).isoformat(timespec="seconds"),
    )


def write_outputs(rows, diag, universe_count, stage1_count):
    rows.sort(key=lambda c: (
        c.action == "BUY_CANDIDATE",
        c.action == "WATCH",
        c.momentum_phase == "ACCELERATING",
        c.opportunity_score - 0.45 * c.maturity_score - 0.2 * c.risk_score,
    ), reverse=True)
    fields = list(Candidate.__annotations__.keys())
    with open(OUT / "awakening_scan.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(asdict(row))
    with open(OUT / "diagnostics.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["kind", "detail"])
        w.writerows(diag)
    with open(OUT / "awakening_scan.json", "w", encoding="utf-8") as f:
        json.dump([asdict(x) for x in rows], f, indent=2)
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
        "accelerating": sum(x.momentum_phase == "ACCELERATING" for x in rows),
        "igniting": sum(x.momentum_phase == "IGNITING" for x in rows),
        "expanding": sum(x.momentum_phase == "EXPANDING" for x in rows),
        "decelerating": sum(x.momentum_phase == "DECELERATING" for x in rows),
        "exhausted": sum(x.momentum_phase == "EXHAUSTED" for x in rows),
        "run_time_et": datetime.now(timezone.utc).astimezone(ET).isoformat(timespec="seconds"),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    for x in rows[:35]:
        print(f"{x.ticker:6} ${x.price:7.2f} {x.action:15} {x.momentum_phase:13} move={x.move_pct:6.2f}% v5={x.velocity_5m_pct:6.2f}% v15={x.velocity_15m_pct:6.2f}% accel={x.price_accel_pct:6.2f} vacc={x.volume_accel:5.2f} impulse={x.volume_impulse:5.2f} opp={x.opportunity_score:5.1f} mat={x.maturity_score:5.1f} risk={x.risk_score:5.1f}")


def main():
    diag = []
    if not ALPACA_KEY or not ALPACA_SECRET:
        diag.append(("credentials", "missing ALPACA_API_KEY or ALPACA_SECRET_KEY"))
        write_outputs([], diag, 0, 0)
        return
    symbols = load_universe(diag)
    rough = stage1(symbols, diag)
    rows = []
    for _, sym, _, move, pace, dvol in rough:
        try:
            c = deep(sym, move, pace, dvol)
            if c:
                rows.append(c)
        except Exception as exc:
            diag.append(("deep_error", f"{sym}: {type(exc).__name__}: {exc}"))
        time.sleep(0.08)
    write_outputs(rows, diag, len(symbols), len(rough))


if __name__ == "__main__":
    main()
