import importlib.util
import sys
from pathlib import Path

import pandas as pd

MODULE = Path(__file__).resolve().parents[1] / "src" / "run_out_of_sample.py"
spec = importlib.util.spec_from_file_location("runner", MODULE)
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


def test_stop_wins_same_bar_ambiguity():
    p = runner.Position(
        ticker="TEST", signal_date=pd.Timestamp("2022-01-03"), entry_date=pd.Timestamp("2022-01-04"),
        score=90, entry=100, shares=1, cost=100, stop=95, target=106,
    )
    bar = pd.Series(dict(open=100, high=110, low=90, close=105))
    price, reason = runner.exit_on_bar(p, bar)
    assert reason == "STOP"
    assert price == min(100, 95) * (1 - runner.SLIPPAGE)
    assert p.bars == 1


def test_entry_session_counts_toward_four_session_limit():
    p = runner.Position(
        ticker="TEST", signal_date=pd.Timestamp("2022-01-03"), entry_date=pd.Timestamp("2022-01-04"),
        score=90, entry=100, shares=1, cost=100, stop=90, target=120,
    )
    bar = pd.Series(dict(open=100, high=105, low=95, close=101))
    for _ in range(3):
        price, reason = runner.exit_on_bar(p, bar)
        assert price is None
        assert reason is None
    price, reason = runner.exit_on_bar(p, bar)
    assert reason == "TIME"
    assert p.bars == 4
    assert price == 101 * (1 - runner.SLIPPAGE)


def test_breakout_uses_prior_20_session_high():
    dates = pd.date_range("2021-12-01", periods=25, freq="B")
    close = [100.0] * 24 + [102.0]
    high = [101.0] * 24 + [103.0]
    low = [99.0] * 25
    volume = [1_000_000.0] * 24 + [2_000_000.0]
    df = pd.DataFrame(dict(date=dates, open=close, high=high, low=low, close=close, volume=volume))
    q = runner.prep_stock(df)
    last = q.iloc[-1]
    assert last.hi20 == 101.0
    assert bool(last.breakout)
