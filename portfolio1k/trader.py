"""Order handling for Portfolio 1k: patient limit buys, a stop at Alpaca, market exits.
Alpaca options orders are DAY orders only, so the protective stop is placed again every morning
for any position held overnight (runner.ensure_stop)."""
import time
import uuid

from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, ReplaceOrderRequest, StopOrderRequest

import broker as B
import config as C
from portfolio1k import rules as R


def _status(o) -> str:
    return str(getattr(o.status, "value", o.status)).lower()


def _wait(client, oid: str, seconds: float):
    end = time.time() + seconds
    o = client.get_order_by_id(oid)
    while _status(o) not in ("filled", "canceled", "expired", "rejected") and time.time() < end:
        time.sleep(1)
        o = client.get_order_by_id(oid)
    return o


def buy(client, symbol: str, root: str, qty: int, bid: float, ask: float) -> tuple[int, float]:
    """Limit buy at the mid, raised one tick every 10 s up to 2% above mid; after 60 s cancel.
    Returns (contracts filled, average price). (0, 0) = not filled."""
    mid = (bid + ask) / 2
    cap = R.round_tick(root, mid * 1.02, up=False)
    price = R.round_tick(root, mid, up=True)
    if C.DRY_RUN:
        B.log.info("[1k] DRY RUN buy %d %s @ %.2f", qty, symbol, price)
        return qty, price
    o = client.submit_order(LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, limit_price=price,
                                              time_in_force=TimeInForce.DAY, position_intent=PositionIntent.BUY_TO_OPEN,
                                              client_order_id=f"p1k-{uuid.uuid4().hex[:12]}"))
    start = time.time()
    while time.time() - start < 60:
        o = _wait(client, o.id, 10)
        if _status(o) == "filled":
            break
        if _status(o) in ("canceled", "expired", "rejected"):
            break
        nxt = round(price + R.tick(root, price), 2)
        if nxt <= cap and time.time() - start < 55:
            try:
                o = client.replace_order_by_id(o.id, ReplaceOrderRequest(limit_price=nxt))
                price = nxt
            except Exception as e:
                B.log.info("[1k] could not raise the limit: %s", e)
    if _status(o) not in ("filled", "canceled", "expired", "rejected"):
        try:
            client.cancel_order_by_id(o.id)
        except Exception:
            pass
        o = _wait(client, o.id, 5)
    filled = int(float(o.filled_qty or 0))
    return filled, float(o.filled_avg_price or 0) if filled else 0.0


def place_stop(client, symbol: str, root: str, qty: int, stop_price: float) -> str | None:
    """A sell-stop resting at Alpaca, so the trade is protected even if this program or GitHub goes down."""
    sp = max(0.01, R.round_tick(root, stop_price, up=False))
    if C.DRY_RUN:
        return "dry-run-stop"
    try:
        o = client.submit_order(StopOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, stop_price=sp,
                                                 time_in_force=TimeInForce.DAY, position_intent=PositionIntent.SELL_TO_CLOSE,
                                                 client_order_id=f"p1k-stop-{uuid.uuid4().hex[:10]}"))
        return str(o.id)
    except Exception as e:
        B.alert(f"[1k] Could not place the stop for {symbol} ({e}). The program still checks it every {int(15)} s while running.")
        return None


def cancel(client, oid: str | None):
    if oid and not C.DRY_RUN and oid != "dry-run-stop":
        try:
            client.cancel_order_by_id(oid)
            _wait(client, oid, 5)
        except Exception as e:
            B.log.info("[1k] cancel %s: %s", oid, e)


def stop_filled(client, oid: str | None) -> tuple[int, float]:
    if not oid or C.DRY_RUN or oid == "dry-run-stop":
        return 0, 0.0
    o = client.get_order_by_id(oid)
    if _status(o) == "filled":
        return int(float(o.filled_qty or 0)), float(o.filled_avg_price or 0)
    return 0, 0.0


def stop_active(client, oid: str | None) -> bool:
    if not oid or C.DRY_RUN or oid == "dry-run-stop":
        return bool(oid)
    try:
        return _status(client.get_order_by_id(oid)) in ("new", "accepted", "pending_new", "held", "partially_filled")
    except Exception:
        return False


def sell(client, symbol: str, qty: int, bid: float) -> float:
    """Market sell to close; returns the fill price (bid in a dry run)."""
    if C.DRY_RUN:
        B.log.info("[1k] DRY RUN sell %d %s @ ~%.2f", qty, symbol, bid)
        return bid
    o = client.submit_order(MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                                               position_intent=PositionIntent.SELL_TO_CLOSE,
                                               client_order_id=f"p1k-exit-{uuid.uuid4().hex[:10]}"))
    o = _wait(client, o.id, 30)
    if _status(o) != "filled":
        B.alert(f"[1k] Exit order for {symbol} is {_status(o)} after 30 s. Check Alpaca.")
    return float(o.filled_avg_price or bid or 0)
