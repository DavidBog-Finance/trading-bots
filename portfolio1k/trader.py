"""Order handling for Portfolio 1k: patient limit buys, a stop at Alpaca, patient limit exits
(market orders only for stops, kills and expiry closes).
The protective stop is a GTC order when Alpaca accepts it (GTC options orders since March 2026), so a trade held
overnight stays protected before the first run of the next day; if Alpaca refuses GTC it falls back to a DAY
stop, which manage() places again every morning."""
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
    err = None
    for tif in (TimeInForce.GTC, TimeInForce.DAY):
        try:
            o = client.submit_order(StopOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, stop_price=sp,
                                                     time_in_force=tif, position_intent=PositionIntent.SELL_TO_CLOSE,
                                                     client_order_id=f"p1k-stop-{uuid.uuid4().hex[:10]}"))
            return str(o.id)
        except Exception as e:
            err = e
    B.log.warning("[1k] could not place the stop for %s: %s", symbol, err)
    # A stop may already be resting (e.g. placed before a lookup failed): keep its id rather than lose it
    return open_stop_for(client, symbol)


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


def stop_state(client, oid: str | None) -> tuple:
    """(resting at Alpaca? True/False, or None if Alpaca could not be asked; contracts on the order or None).
    None means 'unknown': the caller must NOT place a second stop on top of one that may still be resting."""
    if not oid or C.DRY_RUN or oid == "dry-run-stop":
        return bool(oid), None
    try:
        o = client.get_order_by_id(oid)
    except Exception as e:
        B.log.info("[1k] stop lookup failed: %s", e)
        return None, None
    q = getattr(o, "qty", None)
    st = _status(o)
    if st in ("pending_cancel", "pending_replace"):
        return None, None
    return st in ("new", "accepted", "pending_new", "held", "partially_filled"), int(float(q)) if q else None


def cancel_open_sells(client, symbol: str):
    """Cancel every open sell order for this contract (the stop and any leftover exit order), so a new
    sell is not refused for 'insufficient qty' because an old order still holds the contracts."""
    if C.DRY_RUN:
        return
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    try:
        rows = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], limit=50))
    except Exception as e:
        B.log.info("[1k] open orders lookup failed: %s", e)
        return
    for o in rows:
        if str(getattr(o.side, "value", o.side)).lower() == "sell":
            cancel(client, str(o.id))


def open_stop_for(client, symbol: str) -> str | None:
    """The id of a sell-stop already resting at Alpaca for this contract (used after memory was lost)."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    try:
        for o in client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], limit=50)):
            if str(getattr(o.type, "value", o.type)).lower() in ("stop", "stop_limit") and \
                    str(getattr(o.side, "value", o.side)).lower() == "sell":
                return str(o.id)
    except Exception as e:
        B.log.info("[1k] open orders lookup failed: %s", e)
    return None


def recent_orders(client, since) -> list:
    """Closed orders since a UTC datetime, as plain dicts (for rebuilding counters and exit prices)."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    try:
        rows = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=since, limit=200))
    except Exception as e:
        B.log.info("[1k] order history lookup failed: %s", e)
        return []
    out = []
    for o in rows:
        fa = getattr(o, "filled_at", None)
        out.append({"symbol": o.symbol, "client_order_id": o.client_order_id or "",
                    "side": str(getattr(o.side, "value", o.side)).lower(), "status": _status(o),
                    "filled_at": fa.isoformat() if hasattr(fa, "isoformat") else (fa or ""),
                    "qty": float(o.filled_qty or 0), "price": float(o.filled_avg_price or 0)})
    return out


def stop_active(client, oid: str | None) -> bool:
    if not oid or C.DRY_RUN or oid == "dry-run-stop":
        return bool(oid)
    try:
        return _status(client.get_order_by_id(oid)) in ("new", "accepted", "pending_new", "held", "partially_filled")
    except Exception:
        return False


def _market_sell(client, symbol: str, qty: int):
    o = client.submit_order(MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                                               position_intent=PositionIntent.SELL_TO_CLOSE,
                                               client_order_id=f"p1k-exit-{uuid.uuid4().hex[:10]}"))
    o = _wait(client, o.id, 30)
    if _status(o) != "filled":
        try:
            client.cancel_order_by_id(o.id)   # don't leave an untracked sell resting
            o = _wait(client, o.id, 5)
        except Exception:
            pass
        B.alert(f"[1k] Exit order for {symbol} was {_status(o)} after 30 s; it is cancelled and the trade is still "
                f"managed (stop re-placed). Check Alpaca.")
    return int(float(o.filled_qty or 0)), float(o.filled_avg_price or 0)


def sell(client, symbol: str, qty: int, bid: float, ask: float = 0.0, urgent: bool = True) -> tuple[int, float]:
    """Sell to close; returns (contracts actually sold, their average price). (0, 0) = nothing filled:
    the caller must treat the position as still open.
    urgent (stops, kills, expiry): market order at once. Otherwise a patient limit ladder from the mid
    down to the bid, about 5 s per step, then market for anything still unsold."""
    if C.DRY_RUN:
        B.log.info("[1k] DRY RUN sell %d %s @ ~%.2f", qty, symbol, bid)
        return qty, bid
    ladder = [] if urgent else R.sell_ladder((R.parse_occ(symbol) or {}).get("root", symbol), bid, ask)
    done, value = 0, 0.0
    if ladder:
        o = client.submit_order(LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, limit_price=ladder[0],
                                                  time_in_force=TimeInForce.DAY, position_intent=PositionIntent.SELL_TO_CLOSE,
                                                  client_order_id=f"p1k-exit-{uuid.uuid4().hex[:10]}"))
        for nxt in [*ladder[1:], None]:
            o = _wait(client, o.id, 5)
            if _status(o) in ("filled", "canceled", "expired", "rejected") or nxt is None:
                break
            try:
                o = client.replace_order_by_id(o.id, ReplaceOrderRequest(limit_price=nxt))
            except Exception as e:
                B.log.info("[1k] could not lower the sell limit: %s", e)
                break
        if _status(o) not in ("filled", "canceled", "expired", "rejected"):
            try:
                client.cancel_order_by_id(o.id)
            except Exception:
                pass
            o = _wait(client, o.id, 5)
        done = int(float(o.filled_qty or 0))
        value = done * float(o.filled_avg_price or 0)
    if done < qty:
        q, px = _market_sell(client, symbol, qty - done)
        done, value = done + q, value + q * px
    return done, (value / done if done else 0.0)
