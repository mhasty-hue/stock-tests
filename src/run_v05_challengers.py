from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Set, Tuple

import numpy as np
import pandas as pd

from run_out_of_sample import (
    STARTING_CASH, SLIPPAGE, MAX_SLOTS, TARGET_PCT, ATR_STOP_MULT,
    load_membership_history, memberships_by_date, load_ticker, prep_spy,
    prep_stock, Position, exit_on_bar,
)

VARIANTS = {
    "v04_baseline": "Original score only",
    "v05a_moderate_volume": "Prefer 1.5x-2.1x relative volume, then original score",
    "v05b_early_move": "Prefer prior 5-day return <= 8.3%, then original score",
    "v05c_both": "Prefer both moderate volume and early move; then either; then original score",
}


def add_rank_columns(candidates: pd.DataFrame, variant: str) -> pd.DataFrame:
    c = candidates.copy()
    vol_ok = c["volratio"].between(1.5, 2.1, inclusive="both")
    early_ok = c["ret5"] <= 0.083
    if variant == "v04_baseline":
        c["rank_group"] = 0
    elif variant == "v05a_moderate_volume":
        c["rank_group"] = vol_ok.astype(int)
    elif variant == "v05b_early_move":
        c["rank_group"] = early_ok.astype(int)
    elif variant == "v05c_both":
        c["rank_group"] = (vol_ok.astype(int) + early_ok.astype(int))
    else:
        raise ValueError(variant)
    return c.sort_values(
        ["date", "rank_group", "score", "ticker"],
        ascending=[True, False, False, True],
    )


def simulate_variant(
    prepared: pd.DataFrame,
    membership: Dict[pd.Timestamp, Set[str]],
    year: int,
    variant: str,
) -> Tuple[dict, pd.DataFrame, pd.DataFrame]:
    start_date = pd.Timestamp(f"{year}-01-01")
    end_date = pd.Timestamp(f"{year}-12-31")
    trade_days = sorted(d for d in prepared["date"].unique() if start_date <= d <= end_date)
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
    candidates = add_rank_columns(candidates[candidates["member"]], variant)
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
            trades.append(dict(
                ticker=p.ticker, signal_date=p.signal_date, entry_date=p.entry_date,
                exit_date=date, score=p.score, entry=p.entry, exit=xp,
                return_pct=(xp / p.entry - 1) * 100, pnl=proceeds - p.cost,
                reason=reason, bars=p.bars,
            ))
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
                xp, reason = exit_on_bar(p, rr)
                if xp is None:
                    positions.append(p)
                else:
                    proceeds = sh * xp
                    cash += proceeds
                    trades.append(dict(
                        ticker=x.ticker, signal_date=pd.Timestamp(x.date).normalize(),
                        entry_date=date, exit_date=date, score=float(x.score),
                        entry=en, exit=xp, return_pct=(xp / en - 1) * 100,
                        pnl=proceeds - amt, reason=reason, bars=p.bars,
                    ))

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
        trades.append(dict(
            ticker=p.ticker, signal_date=p.signal_date, entry_date=p.entry_date,
            exit_date=last, score=p.score, entry=p.entry, exit=xp,
            return_pct=(xp / p.entry - 1) * 100, pnl=proceeds - p.cost,
            reason="YEAR_END", bars=p.bars,
        ))

    tr = pd.DataFrame(trades)
    eq = pd.DataFrame(equity_rows)
    if len(eq):
        eq.loc[eq.index[-1], "equity"] = cash
    dd = (eq.equity / eq.equity.cummax() - 1).min() * 100 if len(eq) else np.nan
    summary = dict(
        year=year,
        variant=variant,
        description=VARIANTS[variant],
        ending_cash=cash,
        return_pct=(cash / STARTING_CASH - 1) * 100,
        trades=len(tr),
        win_rate_pct=(tr.return_pct > 0).mean() * 100 if len(tr) else np.nan,
        avg_trade_pct=tr.return_pct.mean() if len(tr) else np.nan,
        max_drawdown_pct=dd,
        target_exits=int((tr.reason == "TARGET").sum()) if len(tr) else 0,
        stop_exits=int((tr.reason == "STOP").sum()) if len(tr) else 0,
        time_exits=int((tr.reason == "TIME").sum()) if len(tr) else 0,
        major_loss_pct=(tr.return_pct < -5).mean() * 100 if len(tr) else np.nan,
    )
    return summary, tr, eq


def main() -> None:
    ap = argparse.ArgumentParser(description="v0.5 ranking challenger research")
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path(".cache/market-data"))
    ap.add_argument("--results-dir", type=Path, default=Path("results_v05"))
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

    spy, _ = load_ticker("SPY", warmup_start, fetch_end, args.cache_dir / "prices")
    if spy is None:
        raise RuntimeError("Could not retrieve SPY")
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
    if len(pieces) < max(20, int(len(symbols) * 0.80)):
        raise RuntimeError(f"Insufficient historical coverage: {len(pieces)}/{len(symbols)}")

    data = pd.concat(pieces, ignore_index=True).merge(spy_feat, on="date", how="left")
    trading_members = memberships_by_date(mh, sorted(data["date"].unique()))

    summaries = []
    for variant in VARIANTS:
        s, tr, eq = simulate_variant(data, trading_members, year, variant)
        s["loaded_symbols"] = int((audit_df.status == "OK").sum())
        s["missing_symbols"] = int((audit_df.status != "OK").sum())
        summaries.append(s)
        tr.to_csv(args.results_dir / f"{year}_{variant}_trades.csv", index=False)
        eq.to_csv(args.results_dir / f"{year}_{variant}_equity.csv", index=False)
        print(json.dumps(s, indent=2, default=str))

    pd.DataFrame(summaries).to_csv(args.results_dir / f"{year}_v05_comparison.csv", index=False)


if __name__ == "__main__":
    main()
