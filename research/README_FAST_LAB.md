# Fast Research Lab

Goal: stop iterating one GitHub Action at a time and test the Catalyst Ignition hypothesis in large historical batches before changing live rules.

## Frozen hypothesis
Early abnormal price acceleration + abnormal recent volume, while total move maturity remains low, can identify affordable stocks before a larger intraday/short-horizon move.

## Research sequence
1. Build a historical event table from Alpaca bars for affordable symbols ($2-$100 at observation time).
2. At each observation timestamp calculate only information available at that timestamp: 5m/15m velocity, prior 15m velocity, acceleration, recent volume impulse, broader volume acceleration, dollar volume, gap/change, maturity and risk proxies.
3. Grade forward outcomes at +15m, +30m, +60m, close, next-session high/close, MFE and MAE.
4. Evaluate threshold grids in one run rather than one workflow per formula change.
5. Split development and untouched validation periods. Do not tune on validation results.
6. Model the actual $100 whole-share constraint and permit cash/no-trade decisions and 1-3 simultaneous ideas.

## Primary research metrics
- Precision of BUY_CANDIDATE/WATCH
- Recall of subsequent +5%, +10%, +20% movers
- Median and tail MFE/MAE after detection
- Detection maturity: how far the stock had already moved when first detected
- Return per day of capital
- $100 whole-share equity curve and max drawdown
- False-positive rate
- Missed-monster audit

## Live decision product
No autonomous execution is required. The eventual live output is a ranked decision card: ticker, current price, suggested whole-share size, entry zone, invalidation/stop level, profit-management levels, phase, opportunity/maturity/risk scores, and NO TRADE when nothing qualifies.

Research first; live recommendations only after frozen out-of-sample and forward-paper evidence is adequate.
