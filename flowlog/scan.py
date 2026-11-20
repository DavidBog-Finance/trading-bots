"""Free, automatic "flow" from your own Alpaca data. After each close it looks at your watchlist's
options and logs the contracts with unusually big new volume (volume above open interest, large
premium) into the flow log, tagged "scan". Alpaca's free option data has no buy/sell flag, so the
side is estimated from price ticks (a trade above the last price counts as a buy) and tagged "est-side";
when the ticks are mixed the side is left blank and the read is "unclear".

Rougher than Unusual Whales, but it costs nothing and the flow log scores it the same way, so you can
see whether this kind of signal is worth paying for."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from flowlog import parse as P

MIN_PREMIUM = 100_000      # dollars traded in the contract today
MIN_VOLUME = 500           # contracts
MIN_VOL_OI = 1.0           # volume above open interest = mostly new positions
MAX_DTE = 45
PER_SYMBOL = 2
PER_DAY = 8


@dataclass
class Contract:
    symbol: str          # OCC symbol, e.g. AAPL261218C00230000
    underlying: str
    expiry: date
    strike: float
    kind: str            # call / put
    open_interest: float
    volume: float = 0.0
    vwap: float = 0.0

    @property
    def premium(self) -> float:
        return self.volume * self.vwap * 100

    @property
    def vol_oi(self) -> float:
        return self.volume / self.open_interest if self.open_interest else float("inf")


def unusual(contracts: list, today: date) -> list:
    """Pure: the day's most unusual contracts, biggest first."""
    keep = [c for c in contracts
            if c.premium >= MIN_PREMIUM and c.volume >= MIN_VOLUME and c.vol_oi > MIN_VOL_OI
            and 1 <= (c.expiry - today).days <= MAX_DTE]
    keep.sort(key=lambda c: c.premium * min(c.vol_oi, 10), reverse=True)
    out, per = [], {}
    for c in keep:
        if per.get(c.underlying, 0) >= PER_SYMBOL:
            continue
        per[c.underlying] = per.get(c.underlying, 0) + 1
        out.append(c)
        if len(out) >= PER_DAY:
            break
    return out


def side_from_ticks(trades: list) -> str:
    """Pure: trades = [(price, size), ...] in time order. Tick rule: a trade above the last different
    price is a buy, below is a sell. 'ask' if buys are at least 65% of classified volume, 'bid' if at
    most 35%, otherwise '' (unclear)."""
    buy = sell = 0.0
    last = None
    side = 0
    for price, size in trades:
        if last is not None and price != last:
            side = 1 if price > last else -1
        if side > 0:
            buy += size
        elif side < 0:
            sell += size
        if last is None or price != last:
            last = price
    total = buy + sell
    if total == 0:
        return ""
    share = buy / total
    return "ask" if share >= 0.65 else "bid" if share <= 0.35 else ""


def to_alert(c: Contract, side: str, today: date) -> P.Alert:
    # "delayed": Alpaca's free option feed delays trades and alters quotes, so this is never real-time flow
    tags = ["scan", "delayed", "vol>oi"] + (["est-side"] if side else [])
    a = P.Alert(symbol=c.underlying, expiry=c.expiry, strike=c.strike, kind=c.kind, side=side,
                premium=round(c.premium), tags=tags, alert_day=today)
    a.text = f"[scan, delayed data] {a.short()} vol {int(c.volume):,} / OI {int(c.open_interest):,}"
    return a


# ---------- network (Alpaca) ----------

def _contracts(trading, data, underlying: str, spot: float, today: date) -> list:
    from alpaca.data.requests import OptionBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.trading.requests import GetOptionContractsRequest

    found, token = [], None
    while True:
        resp = trading.get_option_contracts(GetOptionContractsRequest(
            underlying_symbols=[underlying], expiration_date_gte=today + timedelta(days=1),
            expiration_date_lte=today + timedelta(days=MAX_DTE),
            strike_price_gte=f"{spot * 0.9:.2f}", strike_price_lte=f"{spot * 1.1:.2f}", limit=1000,
            page_token=token))
        for k in resp.option_contracts or []:
            found.append(Contract(k.symbol, underlying, k.expiration_date, float(k.strike_price),
                                  "call" if str(getattr(k.type, "value", k.type)).lower() == "call" else "put",
                                  float(k.open_interest or 0)))
        token = getattr(resp, "next_page_token", None)
        if not token or len(found) > 4000:
            break
    by = {c.symbol: c for c in found}
    start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    syms = list(by)
    for i in range(0, len(syms), 100):
        bars = data.get_option_bars(OptionBarsRequest(symbol_or_symbols=syms[i:i + 100], timeframe=TimeFrame.Day,
                                                      start=start))
        for s, bl in bars.data.items():
            for b in bl:
                if b.timestamp.date() == today and s in by:
                    by[s].volume, by[s].vwap = float(b.volume), float(b.vwap or b.close or 0)
    return list(by.values())


def _ticks(data, symbol: str, today: date) -> list:
    from alpaca.data.requests import OptionTradesRequest
    start = datetime(today.year, today.month, today.day, 13, 0, tzinfo=timezone.utc)
    res = data.get_option_trades(OptionTradesRequest(symbol_or_symbols=symbol, start=start, limit=20000))
    return [(float(t.price), float(t.size)) for t in res.data.get(symbol, [])]


def run(today: date, watchlist: list, log=print) -> list:
    """Scan the watchlist for today's session and return alerts (not yet stored)."""
    import broker as B
    import config as C
    from alpaca.data.historical.option import OptionHistoricalDataClient

    trading = B.trading_client()
    data = OptionHistoricalDataClient(C.ALPACA_API_KEY, C.ALPACA_SECRET_KEY)
    pool = []
    for sym in watchlist:
        try:
            closes = B.stock_daily_closes(sym, days=10)
            pool += _contracts(trading, data, sym, float(closes.iloc[-1]), today)
        except Exception as e:
            log(f"scan {sym} failed: {e}")
    out = []
    for c in unusual(pool, today):
        try:
            side = side_from_ticks(_ticks(data, c.symbol, today))
        except Exception as e:
            log(f"ticks {c.symbol} failed: {e}")
            side = ""
        out.append(to_alert(c, side, today))
    return out
