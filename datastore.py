"""
LOCAL MARKET-DATA STORE (data/market.duckdb on the home server's SSD).

  bars           daily close + volume for every symbol the bots have looked at.
                 After the first fill, each scan only downloads the days it's missing.
  option_quotes  a daily snapshot of SPY option chains (bid, ask, IV, delta), so in a
                 year the options bot can be backtested on real prices instead of a model.

  python datastore.py options     # record today's SPY option chain now
  python datastore.py info        # what's stored
"""
import os
import sys
from datetime import date, timedelta

import duckdb
import pandas as pd

import config as C

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "market.duckdb")

_SCHEMA = """
create table if not exists bars(symbol varchar, date date, close double, volume double, primary key(symbol, date));
create table if not exists option_quotes(day date, symbol varchar, underlying varchar, expiry date, type varchar,
    strike double, bid double, ask double, iv double, delta double, underlying_price double,
    primary key(day, symbol));
"""


def con():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = duckdb.connect(DB_PATH)
    c.execute(_SCHEMA)
    return c


def upsert_bars(df: pd.DataFrame):
    """df columns: symbol, date, close, volume."""
    if df.empty:
        return
    df = df[["symbol", "date", "close", "volume"]].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    with con() as c:
        c.register("incoming", df)
        c.execute("insert or replace into bars select symbol, date, close, volume from incoming")


def stored_closes(symbols, start: date) -> pd.DataFrame:
    with con() as c:
        df = c.execute("select symbol, date, close from bars where date >= ? and symbol in (select unnest(?))",
                       [start, list(symbols)]).df()
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot_table(index="date", columns="symbol", values="close").sort_index()


def last_dates(symbols) -> dict:
    with con() as c:
        rows = c.execute("select symbol, max(date) from bars where symbol in (select unnest(?)) group by symbol",
                         [list(symbols)]).fetchall()
    return {s: d for s, d in rows}


def closes(symbols, days: int, fetch) -> pd.DataFrame:
    """Daily closes for `symbols` over `days` calendar days, downloading only what's missing.

    fetch(symbols, days) -> long DataFrame (symbol, date, close, volume); market.daily_bars in practice.
    """
    symbols = sorted(set(symbols))
    start = date.today() - timedelta(days=days)
    have = last_dates(symbols)
    fresh_after = date.today() - timedelta(days=4)  # weekends and holidays
    stale = [s for s in symbols if s not in have or have[s] < fresh_after]
    missing = [s for s in stale if s not in have]
    behind = [s for s in stale if s in have]
    if missing:
        upsert_bars(fetch(missing, days))
    if behind:
        oldest = min(have[s] for s in behind)
        upsert_bars(fetch(behind, (date.today() - oldest).days + 3))
    # Today's bar is still forming during market hours: refresh the last few days for everyone.
    recent = fetch(symbols, 5) if not stale else pd.DataFrame()
    upsert_bars(recent)
    return stored_closes(symbols, start)


def record_options(underlying: str = "SPY") -> int:
    """Snapshot the option chain (puts and calls, 7-90 days, strikes within 20% of spot)."""
    import broker as B
    from alpaca.data.requests import OptionChainRequest
    import options_bot as O

    spot = B.stock_latest_price(underlying)
    today = date.today()
    chain = O.data_client().get_option_chain(OptionChainRequest(
        underlying_symbol=underlying, expiration_date_gte=today + timedelta(days=7),
        expiration_date_lte=today + timedelta(days=90), strike_price_gte=spot * 0.8, strike_price_lte=spot * 1.2))
    rows = []
    for sym, snap in chain.items():
        info = O.parse_occ(sym)
        q = getattr(snap, "latest_quote", None)
        if not info or not q:
            continue
        g = getattr(snap, "greeks", None)
        rows.append({"day": today, "symbol": sym, "underlying": underlying, "expiry": info["expiry"],
                     "type": info["type"], "strike": info["strike"], "bid": q.bid_price, "ask": q.ask_price,
                     "iv": getattr(snap, "implied_volatility", None), "delta": getattr(g, "delta", None) if g else None,
                     "underlying_price": spot})
    if rows:
        with con() as c:
            c.register("incoming", pd.DataFrame(rows))
            c.execute("insert or replace into option_quotes select * from incoming")
    B.log.info("options recorder: %d %s contracts saved", len(rows), underlying)
    return len(rows)


def info() -> dict:
    with con() as c:
        b = c.execute("select count(distinct symbol), count(*), min(date), max(date) from bars").fetchone()
        o = c.execute("select count(distinct day), count(*) from option_quotes").fetchone()
    return {"bar_symbols": b[0], "bars": b[1], "from": str(b[2]), "to": str(b[3]),
            "option_days": o[0], "option_quotes": o[1], "file_mb": round(os.path.getsize(DB_PATH) / 1e6, 1)}


if __name__ == "__main__":
    if sys.argv[1:] == ["options"]:
        record_options(C.OPTIONS_UNDERLYING)
    print(info())
