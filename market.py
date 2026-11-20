"""
MARKET SCANNER: what the agents can see. Whole-market, not a fixed list.

Stocks: every active, fractionable US stock/ETF on a major exchange, filtered to
price >= $5 and average daily dollar volume >= $20M (about 1,000-1,500 names),
then ranked by volatility-scaled momentum + news. Today's top movers and most
active names are added so the agent also sees what is moving right now.

Crypto: every Alpaca USD pair except stablecoins, ranked by the crypto bot's
trend vote + news.

  python market.py stocks    # print today's stock scan (needs Alpaca keys)
  python market.py crypto
"""
import re
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import (CryptoBarsRequest, CryptoSnapshotRequest, MarketMoversRequest,
                                  MostActivesRequest, StockBarsRequest, StockSnapshotRequest)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

import broker as B
import config as C
import state
import strategies as S

EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "NYSEARCA"}
# Leveraged/inverse ETFs decay and gap; warrants, rights and units are illiquid.
BLOCKED_NAME = re.compile(r"\b(2x|3x|-1x|-2x|-3x|ultra|ultrashort|ultrapro|leveraged|inverse|bear|bull|"
                          r"daily target|warrant|rights?|units?|volatility|vix)\b", re.I)
STABLECOINS = {"USDT", "USDC", "DAI", "PYUSD", "USDG", "TUSD", "FDUSD", "BUSD", "GUSD", "USDP", "RLUSD"}


def core_symbols() -> set:
    """Symbols the tested rules bots own. The agents leave these alone."""
    s = {C.STOCK_TREND_SYMBOL, *C.STOCK_RSI2_SYMBOLS, *C.CRYPTO_SYMBOLS}
    if C.CASH_SYMBOL:
        s.add(C.CASH_SYMBOL)
    return {x.replace("/", "") for x in s} | s


def _stock_data():
    return StockHistoricalDataClient(C.ALPACA_API_KEY, C.ALPACA_SECRET_KEY)


def _chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def daily_bars(symbols, days: int) -> pd.DataFrame:
    """Long table of split-adjusted daily bars (symbol, date, close, volume), SIP feed, 16+ min old."""
    data = _stock_data()
    end = datetime.now(timezone.utc) - timedelta(minutes=16)
    frames = []
    for chunk in _chunks(list(symbols), 200):
        req = StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=end - timedelta(days=days),
                               end=end, adjustment=Adjustment.SPLIT, feed=DataFeed.SIP)
        df = data.get_stock_bars(req).df
        if not df.empty:
            frames.append(df.reset_index()[["symbol", "timestamp", "close", "volume"]])
    if not frames:
        return pd.DataFrame(columns=["symbol", "timestamp", "close", "volume"])
    out = pd.concat(frames)
    out["date"] = pd.to_datetime(out["timestamp"]).dt.tz_convert("America/New_York").dt.normalize().dt.tz_localize(None)
    return out


def stock_universe(client) -> list[str]:
    """Liquid, tradable, fractionable stocks and ETFs. Cached for the day."""
    cached = state.get("universe")
    if cached and cached.get("day") == state.today():
        return cached["symbols"]
    assets = client.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE))
    cands = [a.symbol for a in assets
             if a.tradable and a.fractionable and str(a.exchange).split(".")[-1] in EXCHANGES
             and re.fullmatch(r"[A-Z]{1,5}", a.symbol) and not BLOCKED_NAME.search(a.name or "")]
    bars = daily_bars(cands, days=20)
    bars["dollar_vol"] = bars["close"] * bars["volume"]
    g = bars.groupby("symbol").agg(price=("close", "last"), dvol=("dollar_vol", "mean"))
    liquid = g[(g.price >= C.AGENT_MIN_PRICE) & (g.dvol >= C.AGENT_MIN_DOLLAR_VOL)].index.tolist()
    liquid = sorted(set(liquid) - core_symbols())
    state.put("universe", {"day": state.today(), "symbols": liquid})
    B.log.info("universe: %d candidates -> %d liquid", len(cands), len(liquid))
    return liquid


def stock_closes(symbols, days: int = 400) -> pd.DataFrame:
    if C.DATASTORE_ENABLED:
        import datastore
        return datastore.closes(symbols, days, daily_bars)  # only downloads what's missing
    bars = daily_bars(symbols, days)
    return bars.pivot_table(index="date", columns="symbol", values="close").sort_index()


def movers() -> dict:
    """Today's top gainers/losers and most active stocks (Alpaca screener)."""
    sc = ScreenerClient(C.ALPACA_API_KEY, C.ALPACA_SECRET_KEY)
    out = {}
    try:
        mv = sc.get_market_movers(MarketMoversRequest(top=20))
        out["gainers"] = [{"symbol": m.symbol, "pct": round(m.percent_change, 2), "price": m.price} for m in mv.gainers]
        out["losers"] = [{"symbol": m.symbol, "pct": round(m.percent_change, 2), "price": m.price} for m in mv.losers]
        ma = sc.get_most_actives(MostActivesRequest(top=20))
        out["most_active"] = [a.symbol for a in ma.most_actives]
    except Exception as e:  # screener is a nice-to-have
        B.log.warning("screener failed: %s", e)
    return out


def _momentum_table(client) -> tuple[pd.DataFrame, dict]:
    """Momentum stats for the whole universe; price history only changes daily, so cache it."""
    cached = state.get("momentum_table")
    if cached and cached.get("day") == state.today():
        return pd.DataFrame.from_records(cached["rows"], index="symbol"), cached["spy"]
    uni = stock_universe(client)
    closes = stock_closes(uni + ["SPY"])
    spy = S.symbol_stats(closes["SPY"].dropna())
    table = S.rank_momentum(closes.drop(columns=["SPY"], errors="ignore"))
    state.put("momentum_table", {"day": state.today(), "spy": spy,
                                 "rows": table.reset_index(names="symbol").to_dict("records")})
    return table, spy


def scan_stocks(client, top: int = 25) -> dict:
    """Ranked candidates + movers + market regime, ready to show the agent."""
    table, spy = _momentum_table(client)
    if not table.empty:
        z = (table["risk_adj_mom"] - table["risk_adj_mom"].mean()) / (table["risk_adj_mom"].std() or 1.0)
        table["news"] = [round(state.news_tilt(s), 2) for s in table.index]
        table["score"] = z + table["news"]
        blocked = {r["symbol"] for r in state.recent_scores(hours=24 * 7) if r["tag"] in ("mna_target", "bankruptcy")}
        table = table[~table.index.isin(blocked)].sort_values("score", ascending=False)
    cols = ["price", "ret_1d", "ret_1m", "mom_12_1", "vol_ann", "rsi2", "off_52w_high", "news", "score"]
    return {
        "market": {"SPY": spy, "regime": "uptrend" if spy.get("above_sma200") else "downtrend"},
        "universe_size": len(table),
        "top_ranked": table.head(top)[cols].round(3).reset_index(names="symbol").to_dict("records") if not table.empty else [],
        "movers_today": movers(),
    }


# ---------- crypto ----------

def crypto_universe(client) -> list[str]:
    assets = client.get_all_assets(GetAssetsRequest(asset_class=AssetClass.CRYPTO, status=AssetStatus.ACTIVE))
    syms = [a.symbol for a in assets if a.tradable and a.symbol.endswith("/USD")
            and a.symbol.split("/")[0] not in STABLECOINS]
    return sorted(set(syms) - core_symbols())


def crypto_closes(symbols, days: int = 300) -> pd.DataFrame:
    data = CryptoHistoricalDataClient()
    now = datetime.now(timezone.utc)
    df = data.get_crypto_bars(CryptoBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame.Day,
                                                start=now - timedelta(days=days), end=now)).df
    if df.empty:
        return pd.DataFrame()
    df = df.reset_index()
    df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_convert("UTC").dt.normalize().dt.tz_localize(None)
    return df.pivot_table(index="date", columns="symbol", values="close").sort_index()


def scan_crypto(client, top: int = 15) -> dict:
    uni = crypto_universe(client)
    closes = crypto_closes(uni + ["BTC/USD"])
    rows = []
    for sym in closes.columns:
        c = closes[sym].dropna()
        if len(c) < 150 or sym == "BTC/USD":
            continue
        st = S.symbol_stats(c)
        st.update(symbol=sym, trend_vote=S.crypto_trend_vote(c), news=round(state.news_tilt(sym.replace("/", "")), 2))
        st["score"] = round(st["trend_vote"] + 0.5 * st["news"], 3)
        rows.append(st)
    rows.sort(key=lambda r: -r["score"])
    btc = S.symbol_stats(closes["BTC/USD"].dropna()) if "BTC/USD" in closes else {}
    return {"market": {"BTC": btc}, "universe_size": len(uni), "top_ranked": rows[:top]}


# ---------- live prices ----------

def live_prices(symbols) -> dict:
    """{symbol: (price, change_today_fraction)} using free real-time IEX / crypto snapshots."""
    out = {}
    stocks = [s for s in symbols if "/" not in s]
    coins = [s for s in symbols if "/" in s]
    if stocks:
        snaps = _stock_data().get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=stocks, feed=DataFeed.IEX))
        for s, sn in snaps.items():
            if sn and sn.latest_trade and sn.previous_daily_bar:
                out[s] = (float(sn.latest_trade.price), float(sn.latest_trade.price) / float(sn.previous_daily_bar.close) - 1)
    if coins:
        snaps = CryptoHistoricalDataClient().get_crypto_snapshot(CryptoSnapshotRequest(symbol_or_symbols=coins))
        for s, sn in snaps.items():
            if sn and sn.latest_trade and sn.previous_daily_bar:
                out[s] = (float(sn.latest_trade.price), float(sn.latest_trade.price) / float(sn.previous_daily_bar.close) - 1)
    return out


if __name__ == "__main__":
    import json
    cl = B.trading_client()
    res = scan_crypto(cl) if sys.argv[1:] == ["crypto"] else scan_stocks(cl)
    print(json.dumps(res, indent=1, default=str))
