# Research scripts (how the strategies were chosen)

These are the exact scripts behind the guide's numbers. They are rough on purpose: they show the
whole search, including the ideas that failed.

Data (free, no account needed):
- S&P 500 daily, 1885-2025, with total return and T-bill rate:
  `git clone --depth 1 https://github.com/SteelCerberus/us-market-data data/spx`
- Bitstamp BTC/USD 1-minute bars, 2012 to today:
  `git clone --depth 1 https://github.com/ff137/bitstamp-btcusd-minute-data data/btc`
  then resample to daily bars into `data/btc_1d.csv` (columns ts,open,high,low,close,volume).

Run with `DATA_DIR=data/ python research/stocks2.py` and so on.

| Script | What it tests |
|---|---|
| stocks1.py | Buy & hold vs 200-day SMA vs Connors RSI(2), in-sample 1928-1999, out-of-sample 2000-2025 |
| stocks2.py | Parameter grids (SMA length, band, RSI thresholds), vol targeting, turn-of-month |
| stocks3.py | Combinations, crisis years (2008, 2020, 2022) |
| stocks4.py | Walk-forward re-tuning vs fixed parameters, cost/delay stress test, yearly table |
| crypto1.py | BTC SMA / time-series momentum / Donchian grids, IS 2013-2020 vs OOS 2021-2026 |
| crypto2.py | Ensemble of lookbacks + volatility targeting + rebalance band, fee stress |
| crypto3.py | Yearly results, plus an hourly "day trading" RSI test showing fees destroy it |
| options1.py | Model backtest of SPY put credit spreads (Black-Scholes priced at VIX x skew), IS 1991-2007 vs OOS 2008-2025. Needs `git clone https://github.com/datasets/finance-vix data/vix` |
| options2.py | Robustness of the spread model to skew, cost and width assumptions |
