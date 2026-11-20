"""
Backtester for both bots. Uses the same functions as the live bots (strategies.py).

Examples
  python backtest.py stocks --csv research/spy.csv            # your own CSV
  python backtest.py stocks --download                        # SPY from Yahoo (needs yfinance)
  python backtest.py crypto --download                        # BTC-USD from Yahoo
  python backtest.py crypto --download --split 2021-01-01     # in-sample / out-of-sample report

CSV format: a Date column and a Close column (Adj Close is used for returns if present).
Assumptions: decide at the close, fill at that close (lag=1); costs charged per unit of turnover.
"""
import argparse
import numpy as np
import pandas as pd
import strategies as S


def load(args, ticker):
    if args.csv:
        df = pd.read_csv(args.csv, parse_dates=[0], index_col=0)
    else:
        import yfinance as yf
        df = yf.download(ticker, start=args.start, auto_adjust=False, progress=False)
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.sort_index()
    close = df["Close"].astype(float)
    tr = df["Adj Close"].astype(float) if "Adj Close" in df else close
    return close, tr


def simulate(pos, asset_ret, cost, cash_rate_daily=0.0):
    w = pos.shift(1).fillna(0.0)                       # weight decided at t's close earns t+1's return
    turnover = w.diff().abs().fillna(w.abs())
    ret = w * asset_ret - turnover * cost + (1 - w) * cash_rate_daily
    return ret, turnover, w


def report(name, r, turnover, w, ppy):
    r = r.dropna()
    eq = (1 + r).cumprod()
    yrs = len(r) / ppy
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    vol = r.std() * np.sqrt(ppy)
    sharpe = r.mean() / r.std() * np.sqrt(ppy) if r.std() > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    trades = (turnover.loc[r.index] > 0).sum() / yrs
    print(f"{name:28s} CAGR {cagr:7.2%}  Vol {vol:6.2%}  Sharpe {sharpe:5.2f}  MaxDD {dd:7.2%}  "
          f"Trades/yr {trades:5.1f}  Avg exposure {w.loc[r.index].mean():5.1%}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("bot", choices=["stocks", "crypto"])
    p.add_argument("--csv")
    p.add_argument("--download", action="store_true")
    p.add_argument("--start", default="1993-01-01")
    p.add_argument("--split", help="date that starts the out-of-sample period, e.g. 2015-01-01")
    p.add_argument("--cost", type=float, help="one-way cost per trade as a fraction")
    p.add_argument("--cash-rate", type=float, default=0.0, help="annual yield on idle cash, e.g. 0.04")
    p.add_argument("--mode", default="blend", choices=["blend", "trend"])
    p.add_argument("--target-vol", type=float, default=0.40)
    a = p.parse_args()

    if a.bot == "stocks":
        close, tr = load(a, "SPY")
        pos, ppy, cost = S.stock_positions(close, a.mode), 252, a.cost if a.cost is not None else 0.0005
    else:
        close, tr = load(a, "BTC-USD")
        pos, ppy, cost = S.crypto_positions(close, a.target_vol), 365, a.cost if a.cost is not None else 0.003
    asset_ret = tr.pct_change().fillna(0.0)
    cash = (1 + a.cash_rate) ** (1 / ppy) - 1
    bot_r, bot_t, bot_w = simulate(pos, asset_ret, cost, cash)
    hold = pd.Series(1.0, index=close.index)
    bh_r, bh_t, bh_w = simulate(hold, asset_ret, cost, cash)

    periods = [("Full", None, None)]
    if a.split:
        periods = [("In-sample", None, a.split), ("Out-of-sample", a.split, None)]
    for label, s, e in periods:
        sl = slice(s, e)
        print(f"--- {label}: {bot_r[sl].index[0].date()} to {bot_r[sl].index[-1].date()}  (cost {cost:.2%}/side)")
        report("Buy & hold", bh_r[sl], bh_t, bh_w, ppy)
        report(f"{a.bot} bot", bot_r[sl], bot_t, bot_w, ppy)


if __name__ == "__main__":
    main()
