"""
OPTIONS BOT: SPY bull put credit spreads, run once a day ~2-55 minutes before the close
(same window as the stock bot).

Rules (see strategies.py): while SPY's 200-day trend is ON, sell a ~25-delta put ~45 days
out and buy a put ~3% of SPY's price lower. Close at 50% of the credit or at 21 days to expiry.
Never more than one spread position. Most you can lose on it = OPTIONS_RISK_PER_TRADE of the
options sleeve (OPTIONS_PCT of equity). If even one spread doesn't fit, it waits and says so.

  python options_bot.py
  FORCE_RUN=1 DRY_RUN=1 python options_bot.py
"""
import math
import os
import re
import sys
from datetime import date

from alpaca.common.exceptions import APIError
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, OptionSnapshotRequest
from alpaca.trading.enums import ContractType, OrderSide, PositionIntent, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

import broker as B
import config as C
import strategies as S
import stock_bot

BOT = "options"
OCC = re.compile(r"^(?P<root>[A-Z]+)(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?P<cp>[CP])(?P<k>\d{8})$")


def parse_occ(symbol):
    m = OCC.match(symbol)
    if not m:
        return None
    return {"expiry": date(2000 + int(m["y"]), int(m["m"]), int(m["d"])), "type": m["cp"], "strike": int(m["k"]) / 1000}


def bs_put_delta(spot, strike, years, iv, r=0.04):
    if not iv or years <= 0:
        return None
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * years) / (iv * math.sqrt(years))
    return 0.5 * (1 + math.erf(d1 / math.sqrt(2))) - 1


def mid(snap):
    q = snap.latest_quote
    return ((q.bid_price or 0) + (q.ask_price or 0)) / 2 if q else 0.0


def data_client():
    return OptionHistoricalDataClient(C.ALPACA_API_KEY, C.ALPACA_SECRET_KEY)


def manage_open_spread(client, legs, today):
    shorts = [p for p in legs if p["qty"] < 0]
    longs = [p for p in legs if p["qty"] > 0]
    if len(shorts) != 1 or len(longs) != 1:
        B.alert(f"[{BOT}] Unexpected option positions {[(p['symbol'], p['qty']) for p in legs]}. Not touching them; check Alpaca.")
        sys.exit(1)
    short, long = shorts[0], longs[0]
    qty = int(abs(short["qty"]))
    snaps = data_client().get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=[short["symbol"], long["symbol"]]))
    value = mid(snaps[short["symbol"]]) - mid(snaps[long["symbol"]])
    entry_credit = short["avg_entry_price"] - long["avg_entry_price"]
    dte = (parse_occ(short["symbol"])["expiry"] - today).days
    reason = S.spread_exit_reason(entry_credit, value, dte)
    B.log.info("[%s] open spread %s/%s x%d: credit %.2f, now %.2f, %d DTE -> %s",
               BOT, short["symbol"], long["symbol"], qty, entry_credit, value, dte, reason or "hold")
    if not reason:
        return
    width = parse_occ(short["symbol"])["strike"] - parse_occ(long["symbol"])["strike"]
    limit = round(min(width, max(0.01, value + 0.02)), 2)  # pay a debit to close
    legs_close = [(short["symbol"], OrderSide.BUY, PositionIntent.BUY_TO_CLOSE),
                  (long["symbol"], OrderSide.SELL, PositionIntent.SELL_TO_CLOSE)]
    try:
        B.submit_spread(client, legs_close, qty, limit, f"close spread: {reason}")
    except APIError as e:
        B.alert(f"[{BOT}] Spread close order rejected ({e}). Closing legs one by one, short first.")
        if not C.DRY_RUN:
            client.close_position(short["symbol"])
            client.close_position(long["symbol"])


def open_new_spread(client, eq, today):
    closes, spot = stock_bot.load(C.OPTIONS_UNDERLYING)
    if not S.trend_positions(closes).iloc[-1]:
        B.log.info("[%s] %s trend is OFF. No new spreads.", BOT, C.OPTIONS_UNDERLYING)
        return
    sleeve = B.sleeve_budget(eq, C.OPTIONS_PCT, C.OPTIONS_MAX_USD)
    risk_budget = sleeve * C.OPTIONS_RISK_PER_TRADE

    chain = data_client().get_option_chain(OptionChainRequest(
        underlying_symbol=C.OPTIONS_UNDERLYING, type=ContractType.PUT,
        expiration_date_gte=date.fromordinal(today.toordinal() + S.OPT_MIN_DTE),
        expiration_date_lte=date.fromordinal(today.toordinal() + S.OPT_MAX_DTE),
        strike_price_gte=spot * 0.75, strike_price_lte=spot * 1.0))
    rows = []
    for sym, snap in chain.items():
        info = parse_occ(sym)
        if not info or not snap.latest_quote:
            continue
        delta = snap.greeks.delta if snap.greeks and snap.greeks.delta is not None else \
            bs_put_delta(spot, info["strike"], (info["expiry"] - today).days / 365, snap.implied_volatility)
        rows.append({"symbol": sym, **info, "delta": delta,
                     "bid": snap.latest_quote.bid_price or 0, "ask": snap.latest_quote.ask_price or 0})
    expiry = S.choose_expiry(sorted({r["expiry"] for r in rows}), today)
    if not expiry:
        B.alert(f"[{BOT}] No {C.OPTIONS_UNDERLYING} expiry between {S.OPT_MIN_DTE} and {S.OPT_MAX_DTE} days. Skipping.")
        return
    pick = S.pick_put_spread([r for r in rows if r["expiry"] == expiry], spot)
    if not pick:
        B.alert(f"[{BOT}] Couldn't find quotes to build a spread for {expiry}. Skipping.")
        return
    short, long = pick
    credit = round(S.spread_mid_credit(short, long), 2)
    width = short["strike"] - long["strike"]
    qty = S.spread_contracts(risk_budget, width, credit)
    max_loss = (width - credit) * 100
    B.log.info("[%s] candidate %s %.0f/%.0f put spread, delta %.2f, credit %.2f, max loss $%.0f each; risk budget $%.0f -> %d",
               BOT, expiry, short["strike"], long["strike"], short["delta"], credit, max_loss, risk_budget, qty)
    if credit < 0.10:
        B.log.info("[%s] credit too small to be worth the fees. Skipping.", BOT)
        return
    if qty < 1:
        need = max_loss / C.OPTIONS_RISK_PER_TRADE / C.OPTIONS_PCT
        B.log.info("[%s] Account too small for one spread (needs ~$%.0f equity at current settings).", BOT, need)
        return
    limit = -credit if C.OPTIONS_CREDIT_IS_NEGATIVE else credit
    legs = [(short["symbol"], OrderSide.SELL, PositionIntent.SELL_TO_OPEN),
            (long["symbol"], OrderSide.BUY, PositionIntent.BUY_TO_OPEN)]
    B.submit_spread(client, legs, qty, limit, f"open {short['strike']:.0f}/{long['strike']:.0f}P {expiry}")


def main():
    if not C.OPTIONS_ENABLED:
        B.log.info("Options bot disabled (OPTIONS_ENABLED=false).")
        return
    client = B.trading_client()
    clock = client.get_clock()
    mins_to_close = (clock.next_close - clock.timestamp).total_seconds() / 60
    if not os.getenv("FORCE_RUN") and not (clock.is_open and 2 <= mins_to_close <= 55):
        B.log.info("Not in the pre-close window. Exiting.")
        return
    legs = B.option_positions(client, C.OPTIONS_UNDERLYING)
    if C.KILL_SWITCH and legs:
        B.alert(f"[{BOT}] KILL_SWITCH on: closing option positions.")
        if not C.DRY_RUN:
            for p in sorted(legs, key=lambda p: p["qty"]):  # shorts (negative qty) first
                client.close_position(p["symbol"])
        return
    eq = B.safety_gate(client, BOT, [p["symbol"] for p in sorted(legs, key=lambda p: p["qty"])])
    today = clock.timestamp.astimezone().date()
    if legs:
        return manage_open_spread(client, legs, today)
    open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    if any(len(o.symbol or "") > 10 or getattr(o, "legs", None) for o in open_orders):
        B.log.info("[%s] An options order is already working. Waiting.", BOT)
        return
    open_new_spread(client, eq, today)


if __name__ == "__main__":
    main()
