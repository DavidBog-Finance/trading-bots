"""
STOCK BOT: SPY trend sleeve + RSI(2) pullback sleeve, run once a day ~2-55 minutes before the close.

  python stock_bot.py              # normal run (exits quietly if it's not near the close)
  FORCE_RUN=1 python stock_bot.py  # run now regardless of time (for testing on paper)

Size = STOCK_PCT of account equity, so deposits are used automatically.
Trend sleeve: half the stock budget in STOCK_TREND_SYMBOL.
RSI(2) sleeve: the other half split evenly across STOCK_RSI2_SYMBOLS.
"""
import os
import sys
from collections import defaultdict

import pandas as pd

import broker as B
import config as C
import strategies as S

BOT = "stock"


def with_today(closes: pd.Series, price: float) -> pd.Series:
    today = pd.Timestamp.now(tz="America/New_York").normalize().tz_localize(None)
    closes = closes[closes.index < today].copy()
    closes.loc[today] = price  # treat the near-close price as today's close
    return closes


def load(symbol):
    closes = B.stock_daily_closes(symbol)
    if B.stale(closes.index[-1], max_age_days=5):
        B.alert(f"[{BOT}] {symbol} price history looks stale (last bar {closes.index[-1].date()}). Not trading.")
        sys.exit(1)
    price = B.stock_latest_price(symbol)
    if abs(price / closes.iloc[-1] - 1) > 0.15:
        B.alert(f"[{BOT}] {symbol} latest price {price} is >15% from last close {closes.iloc[-1]}. Not trading.")
        sys.exit(1)
    return with_today(closes, price), price


def main():
    client = B.trading_client()
    clock = client.get_clock()
    mins_to_close = (clock.next_close - clock.timestamp).total_seconds() / 60
    if not os.getenv("FORCE_RUN") and not (clock.is_open and 2 <= mins_to_close <= 55):
        B.log.info("Not in the pre-close window (open=%s, %.0f min to close). Exiting.", clock.is_open, mins_to_close)
        return

    symbols = sorted({C.STOCK_TREND_SYMBOL, *C.STOCK_RSI2_SYMBOLS} | ({C.CASH_SYMBOL} if C.CASH_SYMBOL else set()))
    eq = B.safety_gate(client, BOT, symbols)
    budget = B.sleeve_budget(eq, C.STOCK_PCT, C.STOCK_MAX_USD)

    targets, prices = defaultdict(float), {}
    trend_share = 1.0 if C.STOCK_MODE == "trend" else 0.5
    closes, prices[C.STOCK_TREND_SYMBOL] = load(C.STOCK_TREND_SYMBOL)
    trend_on = float(S.trend_positions(closes).iloc[-1])
    targets[C.STOCK_TREND_SYMBOL] += trend_share * budget * trend_on
    notes = [f"{C.STOCK_TREND_SYMBOL} trend {'ON' if trend_on else 'OFF'}"]

    if C.STOCK_MODE != "trend":
        each = (1 - trend_share) * budget / len(C.STOCK_RSI2_SYMBOLS)
        for sym in C.STOCK_RSI2_SYMBOLS:
            if sym == C.STOCK_TREND_SYMBOL:
                c = closes
            else:
                c, prices[sym] = load(sym)
            held = float(S.rsi2_positions(c).iloc[-1])
            targets[sym] += each * held
            notes.append(f"{sym} RSI2 {S.rsi_wilder(c, 2).iloc[-1]:.0f} {'IN' if held else 'out'}")

    B.log.info("[%s] equity $%.2f, stock budget $%.2f | %s", BOT, eq, budget, " | ".join(notes))
    if C.CASH_SYMBOL:
        targets[C.CASH_SYMBOL] = max(0.0, budget - sum(targets.values()))
        prices[C.CASH_SYMBOL] = B.stock_latest_price(C.CASH_SYMBOL)

    # Sells first so the cash is there for buys.
    order = sorted(targets, key=lambda s: targets[s] - B.position_value(client, s)[0])
    for sym in order:
        B.rebalance(client, BOT, sym, targets[sym], prices[sym], crypto=False, eq=eq, tolerance=0.05)


if __name__ == "__main__":
    main()
