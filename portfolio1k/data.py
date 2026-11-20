"""Market data for Portfolio 1k (Alpaca free plan: IEX stock bars, indicative option quotes).
Everything that touches the network lives here, so rules.py stays pure and testable."""
import json
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest, OptionChainRequest, OptionSnapshotRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.enums import ContractType
from alpaca.trading.requests import GetOptionContractsRequest

import broker as B
import config as C
from portfolio1k import settings as S

ET = ZoneInfo("America/New_York")


def _stocks():
    return StockHistoricalDataClient(C.ALPACA_API_KEY, C.ALPACA_SECRET_KEY)


def _options():
    return OptionHistoricalDataClient(C.ALPACA_API_KEY, C.ALPACA_SECRET_KEY)


def intraday_bars(symbols: list, now_et: datetime) -> dict:
    """Today's CLOSED 5-minute bars per symbol (IEX feed, real time on the free plan)."""
    start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                           start=start.astimezone(timezone.utc), feed=DataFeed.IEX)
    df = _stocks().get_stock_bars(req).df
    out = {}
    if df.empty:
        return out
    cutoff = now_et - timedelta(minutes=5)  # a bar that started less than 5 min ago is still forming
    for sym in symbols:
        if sym not in df.index.get_level_values(0):
            continue
        d = df.xs(sym)[["open", "high", "low", "close", "volume"]].copy()
        d.index = pd.to_datetime(d.index).tz_convert(ET)
        out[sym] = d[d.index <= cutoff]
    return out


def daily_closes(symbols: list) -> dict:
    out = {}
    for s in symbols:
        try:
            out[s] = B.stock_daily_closes(s, days=40)
        except Exception as e:
            B.log.warning("[1k] daily closes for %s failed: %s", s, e)
    return out


def last_trade_age_ok(df: pd.DataFrame, now_et: datetime) -> bool:
    """Stale-data guard: the newest closed bar must be recent."""
    return not df.empty and (now_et - df.index[-1]) <= timedelta(minutes=12)


def candidates(trading_client, underlying: str, direction: str, spot: float, today: date) -> list:
    """Calls or puts 2-7 days out near the money, with quote, delta, open interest and (if available) volume."""
    ctype = ContractType.CALL if direction == "call" else ContractType.PUT
    lo, hi = spot * 0.95, spot * 1.05
    chain = _options().get_option_chain(OptionChainRequest(
        underlying_symbol=underlying, type=ctype, feed=option_feed(),
        expiration_date_gte=today + timedelta(days=S.MIN_DTE), expiration_date_lte=today + timedelta(days=S.MAX_DTE),
        strike_price_gte=lo, strike_price_lte=hi))
    oi = {}
    resp = trading_client.get_option_contracts(GetOptionContractsRequest(
        underlying_symbols=[underlying], type=ctype,
        expiration_date_gte=today + timedelta(days=S.MIN_DTE), expiration_date_lte=today + timedelta(days=S.MAX_DTE),
        strike_price_gte=f"{lo:.2f}", strike_price_lte=f"{hi:.2f}", limit=1000))
    for c in resp.option_contracts or []:
        oi[c.symbol] = (float(c.open_interest or 0), c.expiration_date, float(c.strike_price))
    rows = []
    for sym, snap in chain.items():
        if sym not in oi or not snap.latest_quote:
            continue
        q = snap.latest_quote
        rows.append({"symbol": sym, "expiry": oi[sym][1], "strike": oi[sym][2], "oi": oi[sym][0],
                     "delta": snap.greeks.delta if snap.greeks else None,
                     "bid": float(q.bid_price or 0), "ask": float(q.ask_price or 0),
                     "quote_time": q.timestamp, "volume": None})
    add_volume(rows, today)
    return rows


def add_volume(rows: list, today: date):
    """Today's contract volume from option daily bars (best effort; None if the feed doesn't have it)."""
    near = [r for r in rows if r["delta"] is not None and 0.3 <= abs(r["delta"]) <= 0.65][:100]
    if not near:
        return
    try:
        bars = _options().get_option_bars(OptionBarsRequest(symbol_or_symbols=[r["symbol"] for r in near],
                                                            timeframe=TimeFrame.Day, start=today))
        vol = {s: float(sum(b.volume for b in bl)) for s, bl in bars.data.items()}
        for r in near:
            r["volume"] = vol.get(r["symbol"], 0.0)
    except Exception as e:
        B.log.info("[1k] option volume unavailable (%s); open-interest filter still applies", e)


def option_feed() -> OptionsFeed:
    """Named on every request, so a change in Alpaca's default can never silently switch feeds."""
    return OptionsFeed.OPRA if S.OPTION_FEED == "opra" else OptionsFeed.INDICATIVE


def option_quote(symbol: str) -> tuple[float, float, datetime | None]:
    snap = _options().get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=symbol, feed=option_feed()))[symbol]
    q = snap.latest_quote
    return float(q.bid_price or 0), float(q.ask_price or 0), q.timestamp


def earnings_dates(symbols: list, today: date, days: int = 10) -> dict:
    """Next earnings date per watchlist stock from Nasdaq's public calendar. If the calendar can't
    be read, single stocks are marked unknown (None in the dict) and the runner skips them that day."""
    want = {s for s in symbols if s not in S.ETFS}
    found, ok = {}, True
    for i in range(days + 1):
        d = today + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        try:
            req = urllib.request.Request(f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}",
                                         headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                rows = (json.load(r).get("data") or {}).get("rows") or []
        except Exception as e:
            B.log.warning("[1k] earnings calendar failed for %s: %s", d, e)
            ok = False
            break
        for row in rows:
            s = (row.get("symbol") or "").upper()
            if s in want and s not in found:
                found[s] = d.isoformat()
    return {s: found.get(s) if ok else "unknown" for s in want}
