"""Thin wrapper around alpaca-py with the safety checks all bots share."""
import atexit
import csv
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, OrderType, PositionIntent, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, OptionLegRequest

import config as C

log = logging.getLogger("bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
LOG_FILE = os.path.join(os.path.dirname(__file__), "logs", "trades.csv")


# ---------- notifications ----------

def _post(url, body: bytes, headers: dict):
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # a notification failure must never crash a bot
        log.error("notify failed: %s", e)


def notify(msg: str, title: str = "Trading bot", priority: str = "default"):
    """Push to the ntfy iPhone app and/or a Discord/Slack webhook."""
    log.info("NOTIFY %s: %s", title, msg)
    tag = "" if C.PAPER else "LIVE "
    if C.NTFY_TOPIC:
        _post(f"https://ntfy.sh/{C.NTFY_TOPIC}", msg.encode(),
              {"Title": f"{tag}{title}", "Priority": priority, "Tags": "chart_with_upwards_trend"})
    if C.ALERT_WEBHOOK_URL:
        body = json.dumps({"content": f"**{tag}{title}**\n{msg}", "text": f"{tag}{title}: {msg}"}).encode()
        _post(C.ALERT_WEBHOOK_URL, body, {"Content-Type": "application/json"})


def alert(msg: str):
    log.warning(msg)
    notify(msg, "Bot problem", "high")


# Trades are collected and pushed as ONE message when the run finishes,
# so your phone buzzes once per run, after the orders have filled.
_trade_lines: list[str] = []


def flush_trades(title: str = "Trades done"):
    if _trade_lines and C.NOTIFY_TRADES and not C.DRY_RUN:
        notify("\n".join(_trade_lines), title)
    _trade_lines.clear()


atexit.register(flush_trades)


# ---------- clients ----------

def trading_client() -> TradingClient:
    if not C.ALPACA_API_KEY or not C.ALPACA_SECRET_KEY:
        sys.exit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY (see .env.example)")
    if not C.PAPER and not C.LIVE_CONFIRMED:
        sys.exit("ALPACA_PAPER=false but I_UNDERSTAND_LIVE_RISK is not 'yes'. Refusing to trade live.")
    log.info("Mode: %s%s", "PAPER" if C.PAPER else "LIVE", " (dry run)" if C.DRY_RUN else "")
    return TradingClient(C.ALPACA_API_KEY, C.ALPACA_SECRET_KEY, paper=C.PAPER)


# ---------- market data ----------

def stock_daily_closes(symbol: str, days: int = 500) -> pd.Series:
    """Split-adjusted daily closes for completed sessions (free SIP history ends 15+ min ago)."""
    client = StockHistoricalDataClient(C.ALPACA_API_KEY, C.ALPACA_SECRET_KEY)
    end = datetime.now(timezone.utc) - timedelta(minutes=16)
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                           start=end - timedelta(days=int(days * 1.5)), end=end,
                           adjustment=Adjustment.SPLIT, feed=DataFeed.SIP)
    df = client.get_stock_bars(req).df.xs(symbol)
    s = df["close"]
    s.index = pd.to_datetime(s.index).tz_convert("America/New_York").normalize().tz_localize(None)
    return s


def stock_latest_price(symbol: str) -> float:
    client = StockHistoricalDataClient(C.ALPACA_API_KEY, C.ALPACA_SECRET_KEY)
    t = client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))
    return float(t[symbol].price)


def crypto_daily_closes(symbol: str, days: int = 400) -> pd.Series:
    """Builds UTC daily closes from hourly bars and drops today's unfinished day."""
    client = CryptoHistoricalDataClient()  # crypto data needs no keys
    now = datetime.now(timezone.utc)
    req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Hour, start=now - timedelta(days=days), end=now)
    df = client.get_crypto_bars(req).df.xs(symbol)
    df.index = pd.to_datetime(df.index).tz_convert("UTC")
    daily = df["close"].resample("1D").last().dropna()
    today = pd.Timestamp(now.date(), tz="UTC")
    daily = daily[daily.index < today]
    daily.index = daily.index.tz_localize(None)
    return daily


def stale(last_bar_date: pd.Timestamp, max_age_days: int) -> bool:
    age = (pd.Timestamp.now("UTC").tz_localize(None).normalize() - last_bar_date.normalize()).days
    return age > max_age_days


# ---------- account ----------

def equity(client: TradingClient) -> float:
    acct = client.get_account()
    if acct.trading_blocked or acct.account_blocked:
        alert("Account is blocked from trading. Stopping.")
        sys.exit(1)
    return float(acct.equity)


def net_deposits(client: TradingClient) -> float:
    """Deposits minus withdrawals over the account's life (0 if none found)."""
    if C.NET_DEPOSITS_OVERRIDE:
        return C.NET_DEPOSITS_OVERRIDE
    total, token = 0.0, None
    for _ in range(50):  # 100 per page; 5,000 transfers is plenty
        params = {"activity_types": "CSD,CSW,JNLC", "page_size": 100, "direction": "desc"}
        if token:
            params["page_token"] = token
        try:
            page = client.get("/account/activities", params)
        except APIError as e:
            log.error("could not read deposits: %s", e)
            return 0.0
        if not page:
            break
        total += sum(float(a.get("net_amount", 0) or 0) for a in page)
        if len(page) < 100:
            break
        token = page[-1]["id"]
    return total


def sleeve_budget(eq: float, pct: float, cap: float) -> float:
    budget = eq * pct
    return min(budget, cap) if cap > 0 else budget


def safety_gate(client: TradingClient, bot: str, symbols) -> float:
    """Kill switch + deposit-aware loss limit. Returns equity if trading may continue."""
    eq = equity(client)
    if C.KILL_SWITCH:
        for s in symbols:
            flatten(client, s, bot, "KILL_SWITCH is on")
        sys.exit(0)
    dep = net_deposits(client)
    if dep > 0 and eq < dep * (1 - C.MAX_LOSS_PCT):
        for s in symbols:
            flatten(client, s, bot, f"equity ${eq:,.2f} is more than {C.MAX_LOSS_PCT:.0%} below deposits ${dep:,.2f}")
        alert(f"[{bot}] Loss limit hit. Bot stopped. Raise MAX_LOSS_PCT or deposit to resume.")
        sys.exit(1)
    if dep <= 0:
        log.warning("No deposit history found; loss limit inactive (set NET_DEPOSITS_OVERRIDE on paper).")
    return eq


# ---------- orders: stocks & crypto ----------

def position_value(client: TradingClient, symbol: str) -> tuple[float, float]:
    """(market value USD, quantity) of a position, zeros if none."""
    try:
        p = client.get_open_position(symbol.replace("/", ""))
        return float(p.market_value), float(p.qty)
    except APIError:
        return 0.0, 0.0


_FINAL = {"filled", "canceled", "expired", "rejected", "done_for_day"}


def _status(order) -> str:
    return str(getattr(order, "status", "")).split(".")[-1].lower()


def wait_for_fill(client: TradingClient, order, timeout: float | None = None):
    """Poll an order until it fills (or is rejected/cancelled) or `timeout` seconds pass."""
    timeout = C.FILL_WAIT_SEC if timeout is None else timeout
    get = getattr(client, "get_order_by_id", None)
    deadline = time.time() + timeout
    while get and _status(order) not in _FINAL and time.time() < deadline:
        time.sleep(2)
        order = get(order.id)
    return order


def fill_text(order) -> str:
    """'filled at $123.45' / 'NOT filled yet (accepted)' for the phone message."""
    st = _status(order)
    price = getattr(order, "filled_avg_price", None)
    if st == "filled" and price:
        return f"filled at ${float(price):,.2f}"
    return f"NOT filled yet ({st})" if st not in _FINAL else st


def record(bot: str, symbol: str, side: str, usd: float, note: str, order=None):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["utc_time", "bot", "symbol", "side", "usd", "paper", "note"])
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), bot, symbol, side, f"{usd:.2f}", C.PAPER, note])
    status = fill_text(order) if order is not None else ("dry run" if C.DRY_RUN else "sent")
    _trade_lines.append(f"[{bot}] {side.upper()} {symbol} ${usd:,.2f}, {status} ({note})")


def flatten(client: TradingClient, symbol: str, bot: str, reason: str, warn: bool = True):
    """Sell the whole position. warn=False for routine sells (the trade push covers them)."""
    value, qty = position_value(client, symbol)
    if qty == 0:
        return
    if warn:
        alert(f"[{bot}] Closing {symbol} (${value:,.2f}): {reason}")
    else:
        log.info("[%s] Closing %s ($%.2f): %s", bot, symbol, value, reason)
    order = None
    if not C.DRY_RUN:
        order = wait_for_fill(client, client.close_position(symbol.replace("/", "")))
    record(bot, symbol, "sell_all", value, reason, order)


def rebalance(client: TradingClient, bot: str, symbol: str, target_usd: float, price: float,
              crypto: bool, eq: float | None = None, tolerance: float = 0.0, note: str = "rebalance"):
    """Move the position in `symbol` to `target_usd` dollars with one market order."""
    value, qty = position_value(client, symbol)
    diff = target_usd - value
    log.info("[%s] %s: holding $%.2f, target $%.2f, diff $%.2f", bot, symbol, value, target_usd, diff)
    if target_usd <= 0 and qty > 0:
        return flatten(client, symbol, bot, "signal says 0%")
    threshold = max(C.MIN_TRADE_USD, tolerance * abs(target_usd))
    if abs(diff) < threshold:
        log.info("[%s] difference below $%.2f, nothing to do", bot, threshold)
        return
    if eq is not None and abs(diff) > C.MAX_ORDER_PCT * eq:
        alert(f"[{bot}] {symbol} order ${abs(diff):,.2f} is over {C.MAX_ORDER_PCT:.0%} of equity. Skipped; check config.")
        return
    side = OrderSide.BUY if diff > 0 else OrderSide.SELL
    tif = TimeInForce.GTC if crypto else TimeInForce.DAY
    if side == OrderSide.BUY:
        req = MarketOrderRequest(symbol=symbol, notional=round(diff, 2), side=side, time_in_force=tif)
    else:
        sell_qty = min(qty, abs(diff) / price)
        req = MarketOrderRequest(symbol=symbol, qty=round(sell_qty, 9 if crypto else 6), side=side, time_in_force=tif)
    log.info("[%s] %s %s $%.2f", bot, "DRY RUN:" if C.DRY_RUN else "Submitting", side.value, abs(diff))
    order = None
    if not C.DRY_RUN:
        order = client.submit_order(req)
        log.info("[%s] order id %s status %s", bot, order.id, order.status)
        order = wait_for_fill(client, order)
    record(bot, symbol, side.value, abs(diff), note, order)
    return order


# ---------- orders: options ----------

def option_positions(client: TradingClient, underlying: str):
    """Open option positions on `underlying` as dicts (qty is negative for shorts)."""
    out = []
    for p in client.get_all_positions():
        if str(getattr(p, "asset_class", "")).endswith("us_option") and p.symbol.startswith(underlying):
            out.append({"symbol": p.symbol, "qty": float(p.qty), "avg_entry_price": float(p.avg_entry_price),
                        "market_value": float(p.market_value or 0)})
    return out


def submit_spread(client: TradingClient, legs, qty: int, limit_price: float, note: str):
    """legs: list of (symbol, OrderSide, PositionIntent). limit_price: signed, already rounded."""
    req = LimitOrderRequest(
        qty=qty, order_class=OrderClass.MLEG, type=OrderType.LIMIT, limit_price=limit_price,
        time_in_force=TimeInForce.DAY,
        legs=[OptionLegRequest(symbol=s, ratio_qty=1, side=side, position_intent=intent) for s, side, intent in legs],
    )
    log.info("[options] %s %s x%d limit %.2f", "DRY RUN:" if C.DRY_RUN else "Submitting", note, qty, limit_price)
    order = None
    if not C.DRY_RUN:
        order = client.submit_order(req)
        log.info("[options] order id %s status %s", order.id, order.status)
        order = wait_for_fill(client, order)
    record("options", legs[0][0], note, abs(limit_price) * 100 * qty, f"limit {limit_price:.2f} x{qty}", order)


__all__ = ["OrderSide", "PositionIntent"]
