"""
Run this on your Mac before adding more symbols (it needs internet access to Yahoo Finance).

  python validate.py

It applies the bots' rules, unchanged, to extra ETFs and coins. Because the rules were
fixed on SPY and BTC first, every result for another symbol is an out-of-sample test.
It prints a suggested STOCK_RSI2_SYMBOLS / CRYPTO_SYMBOLS line at the end.
"""
import numpy as np
import pandas as pd
import yfinance as yf

import strategies as S
from backtest import simulate

STOCKS = ["SPY", "QQQ", "IWM", "DIA"]
COINS = {"BTC-USD": "BTC/USD", "ETH-USD": "ETH/USD", "SOL-USD": "SOL/USD", "LTC-USD": "LTC/USD",
         "LINK-USD": "LINK/USD", "AVAX-USD": "AVAX/USD", "DOGE-USD": "DOGE/USD"}


def closes(ticker, start):
    df = yf.download(ticker, start=start, auto_adjust=True, progress=False)
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df["Close"].dropna()


def stats(r, ppy):
    r = r.dropna()
    eq = (1 + r).cumprod()
    yrs = len(r) / ppy
    return {"CAGR": eq.iloc[-1] ** (1 / yrs) - 1, "Sharpe": r.mean() / r.std() * np.sqrt(ppy) if r.std() else 0,
            "MaxDD": (eq / eq.cummax() - 1).min()}


def show(name, s):
    print(f"  {name:26s} CAGR {s['CAGR']:7.2%}  Sharpe {s['Sharpe']:5.2f}  MaxDD {s['MaxDD']:7.2%}")


def main():
    keep_stocks, keep_coins = [], []
    print("=== RSI(2) pullback sleeve on index ETFs, 2010-today, 0.05% cost per trade, idle cash earns 3%")
    cash = (1.03) ** (1 / 252) - 1
    for t in STOCKS:
        c = closes(t, "2008-01-01")
        r, _, _ = simulate(S.rsi2_positions(c), c.pct_change().fillna(0), 0.0005, cash)
        s = stats(r["2010":], 252)
        show(t, s)
        if s["Sharpe"] > 0.4 and s["CAGR"] > 0.03:
            keep_stocks.append(t)
    print("\n=== Crypto trend bot vs holding, from each coin's first full year on Yahoo, 0.30% cost")
    for t, alpaca_sym in COINS.items():
        try:
            c = closes(t, "2015-01-01")
        except Exception as e:
            print(f"  {t}: download failed ({e})")
            continue
        if len(c) < 600:
            print(f"  {t}: under 600 days of history, skipped")
            continue
        r, _, _ = simulate(S.crypto_positions(c), c.pct_change().fillna(0), 0.003)
        h, _, _ = simulate(pd.Series(1.0, index=c.index), c.pct_change().fillna(0), 0.003)
        start = c.index[200]
        bot, hold = stats(r[start:], 365), stats(h[start:], 365)
        print(f"  {t} from {start.date()}")
        show("bot", bot)
        show("hold", hold)
        if bot["Sharpe"] > 0.5 and bot["MaxDD"] > hold["MaxDD"]:
            keep_coins.append(alpaca_sym)
    print("\nSuggested settings (only symbols that passed):")
    print(f"STOCK_RSI2_SYMBOLS={','.join(keep_stocks) or 'SPY'}")
    print(f"CRYPTO_SYMBOLS={','.join(keep_coins) or 'BTC/USD'}")
    print("Before using a coin, confirm Alpaca lists it as tradable (Alpaca dashboard > Crypto).")


if __name__ == "__main__":
    main()
