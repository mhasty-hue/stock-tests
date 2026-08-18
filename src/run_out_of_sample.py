from __future__ import annotations

import argparse
import io
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd
import requests

STARTING_CASH = 100.0
SLIPPAGE = 0.0005
MAX_SLOTS = 3
TARGET_PCT = 0.06
ATR_STOP_MULT = 2.0
MAX_SESSIONS = 4  # Entry session counts as session 1.
MIN_DOLLAR_VOLUME = 20_000_000.0
MIN_REL_VOLUME = 1.20

HISTORICAL_MEMBERS_URL = (
    "https://raw.githubusercontent.com/hanshof/sp500_constituents/master/"
    "sp_500_historical_components.csv"
)
# Fallback path in case the repo default branch is main.
HISTORICAL_MEMBERS_URL_FALLBACK = (
    "https://raw.githubusercontent.com/hanshof/sp500_constituents/main/"
    "sp_500_historical_components.csv"
)

# A public per-ticker adjusted OHLCV archive. It is a fallback only because it may not
# contain every company that subsequently left the S&P 500.
HF_TICKER_URL = "https://huggingface.co/datasets/guloyy/sp500_csv/resolve/main/{ticker}.csv?download=true"

USER_AGENT = "market-signal-lab/0.4 research"


def canonical_symbol(symbol: str) -> str:
    """Canonical symbol used inside the engine."""
    return str(symbol).strip().upper().replace(".", "-")


def yahoo_symbol(symbol: str) -> str:
    return canonical_symbol(symbol)


def hf_symbol(symbol: str) -> str:
    # HF archive uses dot notation for share classes in some datasets. Try both later.
    return canonical_symbol(symbol)


def get_url(url: str, timeout: int = 60, retries: int = 3) -> requests.Response:
    last = None
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=timeout, headers=headers)
            if r.status_code == 200:
                return r
            last = RuntimeError(f"HTTP {r.status_code}: {url}")
        except Exception as exc:  # pragma: no cover - network dependent
            last = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last}")


def load_membership_history(cache_dir: Path) -> pd.DataFrame:
    cache = cache_dir / "sp500_historical_components.csv"
    if not cache.exists():
        for url in (HISTORICAL_MEMBERS_URL, HISTORICAL_MEMBERS_URL_FALLBACK):
            try:
                cache.write_bytes(get_url(url).content)
                break
            except Exception:
                continue
        if not cache.exists():
            raise RuntimeError("Could not download historical S&P 500 membership file")
    df = pd.read_csv(cache)
    if not {"date", "tickers"}.issubset(df.columns):
        raise ValueError(f"Unexpected membership columns: {list(df.columns)}")
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df = df.sort_values("date").drop_duplicates("date", keep="last")
    return df


def memberships_by_date(history: pd.DataFrame, dates: Sequence[pd.Timestamp]) -> Dict[pd.Timestamp, Set[str]]:
    rows = history.set_index("date")["tickers"]
    hist_dates = rows.index.values
    out: Dict[pd.Timestamp, Set[str]] = {}
    for d in dates:
        d = pd.Timestamp(d).normalize()
        pos = hist_dates.searchsorted(np.datetime64(d), side="right") - 1
        if pos < 0:
            raise ValueError(f"No membership snapshot on or before {d.date()}")
        raw = rows.iloc[int(pos)]
        out[d] = {canonical_symbol(x) for x in str(raw).split(",") if str(x).strip()}
    return out


def fetch_yahoo_chart(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> Optional[pd.DataFrame]:
    """Fetch split/dividend-adjusted daily bars from Yahoo's public chart endpoint.

    This avoids a hard dependency on yfinance and is easy to cache. Yahoo can return
    histories for many delisted symbols, but not all; missing symbols are surfaced.
    """
    s = yahoo_symbol(symbol)
    p1 = int(pd.Timestamp(start, tz="UTC").timestamp())
    p2 = int(pd.Timestamp(end + pd.Timedelta(days=1), tz="UTC").timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{s}"
        f"?period1={p1}&period2={p2}&interval=1d&events=div%2Csplits&includeAdjustedClose=true"
    )
    try:
        r = get_url(url, timeout=30, retries=2)
        obj = r.json()
        result = obj.get("chart", {}).get("result")
        if not result:
            return None
        z = result[0]
        ts = z.get("timestamp") or []
        quote = (z.get("indicators", {}).get("quote") or [{}])[0]
        adj = (z.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")
        if not ts or not adj:
            return None
        q = pd.DataFrame(
            {
                "date": pd.to_datetime(ts, unit="s", utc=True).tz_convert(None).normalize(),
                "open_raw": quote.get("open"),
                "high_raw": quote.get("high"),
                "low_raw": quote.get("low"),
                "close_raw": quote.get("close"),
                "volume": quote.get("volume"),
                "adj_close": adj,
            }
        )
        # Back-adjust OHLC so stops/targets and historical dollar moves are coherent.
        factor = q["adj_close"] / q["close_raw"]
        for name in ("open", "high", "low", "close"):
            raw_col = f"{name}_raw"
            q[name] = q[raw_col] * factor
        q = q[["date", "open", "high", "low", "close", "volume"]]
        q = q.dropna(subset=["date", "open", "high", "low", "close", "volume"])
        q = q[(q["volume"] >= 0) & (q["close"] > 0)]
        return q.sort_values("date").drop_duplicates("date")
    except Exception:
        return None


def fetch_hf_csv(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> Optional[pd.DataFrame]:
    candidates = [hf_symbol(symbol), hf_symbol(symbol).replace("-", ".")]
    for s in dict.fromkeys(candidates):
        url = HF_TICKER_URL.format(ticker=s)
        try:
            r = get_url(url, timeout=45, retries=2)
            q = pd.read_csv(io.BytesIO(r.content))
            expected = {"date", "open", "high", "low", "close", "volume"}
            if not expected.issubset(q.columns):
                continue
            q["date"] = pd.to_datetime(q["date"]).dt.normalize()
            q = q[["date", "open", "high", "low", "close", "volume"]].copy()
            q = q[(q["date"] >= start) & (q["date"] <= end)]
            q = q.dropna().sort_values("date").drop_duplicates("date")
            if len(q):
                return q
        except Exception:
            continue
    return None


def load_ticker(symbol: str, start: pd.Timestamp, end: pd.Timestamp, cache_dir: Path) -> Tuple[Optional[pd.DataFrame], str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"{canonical_symbol(symbol)}.csv"
    if cache.exists():
        try:
            q = pd.read_csv(cache)
            q["date"] = pd.to_datetime(q["date"]).dt.normalize()
            if q["date"].min() <= start + pd.Timedelta(days=10) and q["date"].max() >= end - pd.Timedelta(days=10):
                return q, "cache"
        except Exception:
            pass
    q = fetch_yahoo_chart(symbol, start, end)
    source = "yahoo"
    if q is None or len(q) < 40:
        q = fetch_hf_csv(symbol, start, end)
        source = "huggingface"
    if q is None or len(q) < 40:
        return None, "missing"
    q.to_csv(cache, index=False)
    return q, source


def prep_stock(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("date").copy()
    c = g["close"].astype(float)
    pc = c.shift(1)
    g["hi20"] = g["high"].shift(1).rolling(20).max()
    g["vol20"] = g["volume"].rolling(20).mean()
    g["ret5"] = c.pct_change(5)
    g["volratio"] = g["volume"] / g["vol20"]
    g["dollar_vol"] = c * g["volume"]
    tr = pd.concat(
        [g["high"] - g["low"], (g["high"] - pc).abs(), (g["low"] - pc).abs()], axis=1
    ).max(axis=1)
    g["atr14"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    g["breakout"] = (
        (c > g["hi20"])
        & (g["volratio"] >= MIN_REL_VOLUME)
        & (g["ret5"] > 0)
        & (g["dollar_vol"] >= MIN_DOLLAR_VOLUME)
    )
    g["score"] = (
        70
        + np.minimum(15, np.maximum(0, (g["volratio"] - 1.2) * 10))
        + np.minimum(15, np.maximum(0, g["ret5"] * 100))
    )
    return g


def prep_spy(spy: pd.DataFrame) -> pd.DataFrame:
    q = spy.sort_values("date").copy()
    q["spy_ma50"] = q["close"].rolling(50).mean()
    q["market_ok"] = q["close"] > q["spy_ma50"]
    return q[["date", "close", "spy_ma50", "market_ok"]].rename(columns={"close": "spy_close"})


@dataclass
class Position:
    ticker: str
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    score: float
    entry: float
    shares: float
    cost: float
    stop: float
    target: float
    bars: int = 0


def exit_on_bar(p: Position, rr: pd.Series) -> Tuple[Optional[float], Optional[str]]:
    p.bars += 1
    lo = float(rr.low)
    hi = float(rr.high)
    # Conservative daily-bar ambiguity rule: if both could have been hit, stop first.
    if lo <= p.stop:
        return min(float(rr.open), p.stop) * (1 - SLIPPAGE), "STOP"
    if hi >= p.target:
        return p.target * (1 - SLIPPAGE), "TARGET"
    if p.bars >= MAX_SESSIONS:
        return float(rr.close) * (1 - SLIPPAGE), "TIME"
    return None, None


def simulate(
    prepared: pd.DataFrame,
    membership: Dict[pd.Timestamp, Set[str]],
    year: int,
) -> Tuple[dict, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    start_date = pd.Timestamp(f"{year}-01-01")
    end_date = pd.Timestamp(f"{year}-12-31")
    trade_days = sorted(d for d in prepared["date"].unique() if start_date <= d <= end_date)
    if not trade_days:
        raise ValueError(f"No trading days found for {year}")

    by = {(t, d): r for (t, d), r in prepared.set_index(["ticker", "date"]).iterrows()}
    candidates = prepared[
        prepared["breakout"]
        & prepared["atr14"].notna()
        & prepared["market_ok"].fillna(False)
        & prepared["date"].between(start_date, end_date)
    ].copy()
    candidates["member"] = [
        t in membership.get(pd.Timestamp(d).normalize(), set())
        for t, d in zip(candidates["ticker"], candidates["date"])
    ]
    candidates = candidates[candidates["member"]]
    candidates = candidates.sort_values(["date", "score", "ticker"], ascending=[True, False, True])
    cbd = {d: g for d, g in candidates.groupby("date")}

    def mark(t: str, d: pd.Timestamp, fallback: float) -> float:
        r = by.get((t, d))
        return float(r.close) if r is not None else fallback

    cash = STARTING_CASH
    positions: List[Position] = []
    pending: Dict[pd.Timestamp, list] = {}
    trades: List[dict] = []
    equity_rows: List[dict] = []

    for i, date in enumerate(trade_days):
        date = pd.Timestamp(date).normalize()
        survivors: List[Position] = []
        for p in positions:
            rr = by.get((p.ticker, date))
            if rr is None:
                survivors.append(p)
                continue
            xp, reason = exit_on_bar(p, rr)
            if xp is None:
                survivors.append(p)
                continue
            proceeds = p.shares * xp
            cash += proceeds
            trades.append(
                dict(
                    ticker=p.ticker,
                    signal_date=p.signal_date,
                    entry_date=p.entry_date,
                    exit_date=date,
                    score=p.score,
                    entry=p.entry,
                    exit=xp,
                    return_pct=(xp / p.entry - 1) * 100,
                    pnl=proceeds - p.cost,
                    reason=reason,
                    bars=p.bars,
                )
            )
        positions = survivors

        todays = pending.pop(date, [])
        avail = MAX_SLOTS - len(positions)
        if avail > 0 and cash > 0 and todays:
            todays = [x for x in todays if all(p.ticker != x.ticker for p in positions)][:avail]
            eq_pre = cash + sum(p.shares * mark(p.ticker, date, p.entry) for p in positions)
            desired = eq_pre / MAX_SLOTS
            for x in todays:
                rr = by.get((x.ticker, date))
                amt = min(desired, cash)
                if rr is None or amt < 1:
                    continue
                en = float(rr.open) * (1 + SLIPPAGE)
                sh = amt / en
                cash -= amt
                p = Position(
                    ticker=x.ticker,
                    signal_date=pd.Timestamp(x.date).normalize(),
                    entry_date=date,
                    score=float(x.score),
                    entry=en,
                    shares=sh,
                    cost=amt,
                    stop=en - ATR_STOP_MULT * float(x.atr14),
                    target=en * (1 + TARGET_PCT),
                )
                xp, reason = exit_on_bar(p, rr)  # Entry day counts immediately.
                if xp is None:
                    positions.append(p)
                else:
                    proceeds = sh * xp
                    cash += proceeds
                    trades.append(
                        dict(
                            ticker=x.ticker,
                            signal_date=pd.Timestamp(x.date).normalize(),
                            entry_date=date,
                            exit_date=date,
                            score=float(x.score),
                            entry=en,
                            exit=xp,
                            return_pct=(xp / en - 1) * 100,
                            pnl=proceeds - amt,
                            reason=reason,
                            bars=p.bars,
                        )
                    )

        equity = cash + sum(p.shares * mark(p.ticker, date, p.entry) for p in positions)
        equity_rows.append(dict(date=date, equity=equity, cash=cash, open_positions=len(positions)))

        if i + 1 < len(trade_days) and date in cbd:
            next_day = pd.Timestamp(trade_days[i + 1]).normalize()
            pending[next_day] = [x for _, x in cbd[date].iterrows()]

    last = pd.Timestamp(trade_days[-1]).normalize()
    for p in positions:
        rr = by.get((p.ticker, last))
        xp = (float(rr.close) if rr is not None else p.entry) * (1 - SLIPPAGE)
        proceeds = p.shares * xp
        cash += proceeds
        trades.append(
            dict(
                ticker=p.ticker,
                signal_date=p.signal_date,
                entry_date=p.entry_date,
                exit_date=last,
                score=p.score,
                entry=p.entry,
                exit=xp,
                return_pct=(xp / p.entry - 1) * 100,
                pnl=proceeds - p.cost,
                reason="YEAR_END",
                bars=p.bars,
            )
        )

    tr = pd.DataFrame(trades)
    eq = pd.DataFrame(equity_rows)
    if len(eq):
        eq.loc[eq.index[-1], "equity"] = cash
    dd = (eq.equity / eq.equity.cummax() - 1).min() * 100 if len(eq) else np.nan
    summary = dict(
        year=year,
        starting_cash=STARTING_CASH,
        ending_cash=cash,
        return_pct=(cash / STARTING_CASH - 1) * 100,
        trades=len(tr),
        win_rate_pct=(tr.return_pct > 0).mean() * 100 if len(tr) else np.nan,
        avg_trade_pct=tr.return_pct.mean() if len(tr) else np.nan,
        max_drawdown_pct=dd,
        target_exits=int((tr.reason == "TARGET").sum()) if len(tr) else 0,
        stop_exits=int((tr.reason == "STOP").sum()) if len(tr) else 0,
        time_exits=int((tr.reason == "TIME").sum()) if len(tr) else 0,
        same_day_exits=int((tr.entry_date == tr.exit_date).sum()) if len(tr) else 0,
        rules="breakout + SPY>50DMA; next-open; 3 slots; +6% target; 2xATR stop; 4 sessions; 5bp/side",
    )
    return summary, tr, eq, candidates


def main() -> None:
    ap = argparse.ArgumentParser(description="Frozen v0.4 out-of-sample backtest")
    ap.add_argument("--year", type=int, default=2022)
    ap.add_argument("--cache-dir", type=Path, default=Path(".cache/market-data"))
    ap.add_argument("--results-dir", type=Path, default=Path("results"))
    ap.add_argument("--max-tickers", type=int, default=0, help="Debug only: limit tickers; 0 = all")
    args = ap.parse_args()

    year = args.year
    warmup_start = pd.Timestamp(f"{year-1}-03-01")
    fetch_end = pd.Timestamp(f"{year}-12-31")
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.results_dir.mkdir(parents=True, exist_ok=True)

    mh = load_membership_history(args.cache_dir)
    daily_dates = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D")
    members = memberships_by_date(mh, daily_dates)
    symbols = sorted(set().union(*members.values()))
    if args.max_tickers:
        symbols = symbols[: args.max_tickers]

    # SPY is used only for the already-frozen market-regime rule.
    spy, spy_source = load_ticker("SPY", warmup_start, fetch_end, args.cache_dir / "prices")
    if spy is None:
        raise RuntimeError("Could not retrieve SPY history for market-regime filter")
    spy_feat = prep_spy(spy)

    pieces = []
    audit = []
    for n, symbol in enumerate(symbols, 1):
        q, source = load_ticker(symbol, warmup_start, fetch_end, args.cache_dir / "prices")
        if q is None:
            audit.append(dict(ticker=symbol, status="MISSING", source=source, rows=0))
        else:
            z = prep_stock(q)
            z["ticker"] = symbol
            pieces.append(z)
            audit.append(dict(ticker=symbol, status="OK", source=source, rows=len(z)))
        if n % 50 == 0:
            print(f"loaded {n}/{len(symbols)} tickers", flush=True)

    audit_df = pd.DataFrame(audit)
    audit_df.to_csv(args.results_dir / f"{year}_data_audit.csv", index=False)
    missing = audit_df[audit_df.status != "OK"]
    if len(pieces) < max(20, int(len(symbols) * 0.80)):
        raise RuntimeError(
            f"Insufficient historical coverage: loaded {len(pieces)}/{len(symbols)}; "
            f"see {year}_data_audit.csv"
        )

    data = pd.concat(pieces, ignore_index=True)
    data = data.merge(spy_feat, on="date", how="left")
    # Convert daily membership map to actual trading dates. Weekend snapshots map fine because
    # membership history is date effective; signal filtering occurs only on trading dates.
    trading_members = memberships_by_date(mh, sorted(data["date"].unique()))
    summary, trades, equity, candidates = simulate(data, trading_members, year)
    summary.update(
        universe_symbols=len(symbols),
        loaded_symbols=int((audit_df.status == "OK").sum()),
        missing_symbols=int((audit_df.status != "OK").sum()),
        spy_source=spy_source,
    )
    pd.DataFrame([summary]).to_csv(args.results_dir / f"{year}_summary.csv", index=False)
    trades.to_csv(args.results_dir / f"{year}_trades.csv", index=False)
    equity.to_csv(args.results_dir / f"{year}_equity.csv", index=False)
    candidates[
        ["date", "ticker", "score", "ret5", "volratio", "dollar_vol", "atr14", "spy_close", "spy_ma50"]
    ].to_csv(args.results_dir / f"{year}_qualified_signals.csv", index=False)

    print(json.dumps(summary, indent=2, default=str))
    if len(missing):
        print("\nMissing historical symbols (not silently ignored):")
        print(", ".join(missing.ticker.tolist()))


if __name__ == "__main__":
    main()
