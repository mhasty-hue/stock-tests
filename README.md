# Market Signal Lab v0.4

This package freezes the strategy discovered during the 2025 development study and runs it on untouched historical years.

## Frozen rules

No parameter should be changed after seeing 2021-2024 results unless the version number is advanced and the affected years are reclassified as development data.

- Universe: point-in-time S&P 500 membership on each signal date.
- Signal: close above the prior 20-session high; relative volume >= 1.20; positive 5-session return; signal-day dollar volume >= $20M.
- Market gate: SPY close above its 50-session moving average on the signal date.
- Ranking: `70 + volume bonus (max 15) + 5-day return bonus (max 15)`.
- Entry: next trading session open plus 5 bps slippage.
- Portfolio: maximum 3 concurrent positions, approximately equal capital per slot; cash is allowed.
- Target: +6% from simulated entry.
- Stop: entry minus 2 x ATR(14).
- Maximum holding period: 4 trading sessions, counting the entry session as session 1.
- Daily-bar ambiguity: if stop and target are both inside the same daily bar, assume the stop happened first.
- Exit slippage: 5 bps.

## Data integrity

The runner downloads a historical S&P 500 membership file and checks membership separately for every signal date. Price data is fetched and cached ticker by ticker. Missing historical symbols are written to `results/<year>_data_audit.csv`; they are not silently discarded.

The first price source is Yahoo's public chart endpoint because it may retain histories for delisted/renamed tickers. A public Hugging Face adjusted-OHLCV archive is used as a fallback. This is a research pipeline, not an institutional market-data feed.

## Run locally

```bash
python -m pip install -r requirements.txt
python src/run_out_of_sample.py --year 2022
```

For a quick plumbing test only:

```bash
python src/run_out_of_sample.py --year 2022 --max-tickers 10
```

Do not interpret a limited-ticker run as a strategy backtest.

## GitHub Actions

The included workflow can run 2021, 2022, 2023, or 2024 manually and uploads the summary, trade ledger, equity curve, qualified signals, and data-coverage audit as a workflow artifact.

## Development benchmark

The heavily studied 2025 development result was approximately `$100 -> $213.63`. It is not an out-of-sample result and must not be treated as validated performance.
