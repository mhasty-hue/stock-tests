# Frozen v0.4 rules

Frozen before inspecting 2021-2024 portfolio outcomes.

1. Point-in-time S&P 500 membership on the signal date.
2. Signal-day close > prior 20-session high.
3. Signal-day volume / 20-session average volume >= 1.20.
4. Five-session return > 0.
5. Signal-day dollar volume >= $20 million.
6. SPY close > SPY 50-session moving average on the signal date.
7. Ranking score = 70 + capped relative-volume bonus (15 max) + capped five-day-return bonus (15 max).
8. Enter at the next session open plus 5 bps slippage.
9. Maximum three concurrent positions, approximately one-third of current equity per slot; unused capital remains cash.
10. Profit target = +6% from simulated entry.
11. Stop = entry - 2 x ATR(14).
12. Maximum hold = four sessions including entry day.
13. If stop and target are both within a daily bar, assume stop first.
14. Exit prices incur 5 bps slippage.
15. No parameter changes after seeing an out-of-sample year without advancing the strategy version and reclassifying that year as development data.

2025 is development data. The enhanced development result was approximately $100 -> $213.63 and is not validated performance.
