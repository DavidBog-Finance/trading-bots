"""Every Portfolio 1k rule as a pure function: no network, no clock, fully unit tested.

Bars are pandas DataFrames of today's 5-minute bars (open, high, low, close, volume) indexed by
bar START time in New York time. Only CLOSED bars are passed in (the runner drops the forming one).
"""
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import pandas as pd

from portfolio1k import settings as S


def hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def between(now: datetime, start: str, end: str) -> bool:
    return hm(start) <= now.time() < hm(end)


# ---------- indicators ----------

def vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].replace(0, 1e-9)
    return (tp * vol).cumsum() / vol.cumsum()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def opening_range(df: pd.DataFrame) -> tuple[float, float] | None:
    """High and low of the first 15 minutes (the 09:30, 09:35 and 09:40 bars)."""
    first = df[[hm("09:30") <= t.time() < hm("09:45") for t in df.index]]
    if len(first) < 3:
        return None
    return float(first["high"].max()), float(first["low"].min())


# ---------- setups ----------

@dataclass
class Signal:
    symbol: str
    setup: str              # orb | pullback | news
    direction: str          # call | put
    level: float            # the price that, if lost on a 5-min close, kills the idea (setup stop)
    note: str = ""
    extra: dict = field(default_factory=dict)


def orb_signal(sym: str, df: pd.DataFrame, now: datetime) -> Signal | None:
    """Setup 1: a 5-min bar closes beyond the 15-min opening range, on above-average volume,
    on the same side of VWAP. Only the FIRST bar that breaks counts (no chasing later bars)."""
    if not between(now, S.ORB_START, S.ORB_END) or len(df) < 4:
        return None
    rng = opening_range(df)
    if not rng:
        return None
    hi, lo = rng
    last, prev = df.iloc[-1], df.iloc[-2]
    vw = float(vwap(df).iloc[-1])
    avg_vol = float(df["volume"].iloc[:-1].mean())
    if last["volume"] <= avg_vol:
        return None
    if last["close"] > hi and prev["close"] <= hi and last["close"] > vw:
        return Signal(sym, "orb", "call", hi, f"closed {last['close']:.2f} above the opening-range high {hi:.2f}")
    if last["close"] < lo and prev["close"] >= lo and last["close"] < vw:
        return Signal(sym, "orb", "put", lo, f"closed {last['close']:.2f} below the opening-range low {lo:.2f}")
    return None


def pullback_signal(sym: str, df: pd.DataFrame, daily_closes: pd.Series, now: datetime) -> Signal | None:
    """Setup 2: daily trend up (last close above the 20-day average), price dips to VWAP or the
    9-bar EMA within the last 3 bars, then the newest bar makes a higher low and closes back above
    both. Mirror image for puts."""
    if not between(now, S.PULLBACK_START, S.ENTRY_END) or len(df) < 6 or len(daily_closes) < 20:
        return None
    sma20 = float(daily_closes.iloc[-20:].mean())
    trend_up = float(daily_closes.iloc[-1]) > sma20
    trend_dn = float(daily_closes.iloc[-1]) < sma20
    vw, e9 = vwap(df), ema(df["close"], 9)
    last, prev = df.iloc[-1], df.iloc[-2]
    recent = df.iloc[-4:-1]
    sup = max(float(vw.iloc[-1]), float(e9.iloc[-1]))
    res = min(float(vw.iloc[-1]), float(e9.iloc[-1]))
    if trend_up:
        touched = any(r["low"] <= max(vw.loc[t], e9.loc[t]) * 1.001 for t, r in recent.iterrows())
        if touched and last["low"] > prev["low"] and last["close"] > sup and last["close"] > last["open"]:
            return Signal(sym, "pullback", "call", float(vw.iloc[-1]), "uptrend pullback to VWAP/EMA9 held with a higher low")
    if trend_dn:
        touched = any(r["high"] >= min(vw.loc[t], e9.loc[t]) * 0.999 for t, r in recent.iterrows())
        if touched and last["high"] < prev["high"] and last["close"] < res and last["close"] < last["open"]:
            return Signal(sym, "pullback", "put", float(vw.iloc[-1]), "downtrend bounce to VWAP/EMA9 failed with a lower high")
    return None


def strong_news(rows: list, now_utc: datetime) -> dict | None:
    """The strongest fresh Claude-scored headline among `rows` (one symbol), or None."""
    best = None
    for r in rows:
        ts = datetime.fromisoformat(r["ts"])
        if now_utc - ts > timedelta(minutes=S.NEWS_MAX_AGE_MIN):
            continue
        if abs(r["score"]) >= S.NEWS_MIN_SCORE and r["materiality"] >= S.NEWS_MIN_MATERIALITY:
            if not best or abs(r["score"]) * r["materiality"] > abs(best["score"]) * best["materiality"]:
                best = r
    return best


def news_signal(sym: str, df: pd.DataFrame, news: dict | None, now: datetime) -> Signal | None:
    """Setup 3: strong fresh headline AND price confirms by closing beyond the prior 5-min bar."""
    if not news or not between(now, S.NEWS_START, S.ENTRY_END) or len(df) < 2:
        return None
    last, prev = df.iloc[-1], df.iloc[-2]
    if news["score"] > 0 and last["close"] > prev["high"]:
        return Signal(sym, "news", "call", float(prev["low"]), f"news: {news.get('headline', '')[:90]}", {"news": news})
    if news["score"] < 0 and last["close"] < prev["low"]:
        return Signal(sym, "news", "put", float(prev["high"]), f"news: {news.get('headline', '')[:90]}", {"news": news})
    return None


def spy_agrees(spy: pd.DataFrame, direction: str) -> bool:
    """No calls while SPY is below VWAP and falling; no puts while above VWAP and rising."""
    if len(spy) < 2:
        return False
    last, prev = spy["close"].iloc[-1], spy["close"].iloc[-2]
    vw = float(vwap(spy).iloc[-1])
    if direction == "call":
        return not (last < vw and last < prev)
    return not (last > vw and last > prev)


def merge_signals(sigs: list) -> list:
    """Same symbol and direction from two setups = a stronger (A+ candidate) signal."""
    by = {}
    for s in sigs:
        k = (s.symbol, s.direction)
        if k in by:
            by[k].extra.setdefault("also", []).append(s.setup)
            by[k].note += f"; also {s.setup}: {s.note}"
        else:
            by[k] = s
    return list(by.values())


# ---------- contract picker ----------

def tick(symbol_root: str, price: float) -> float:
    """Minimum option price increment: $0.01 on the penny-program ETFs and under $3, else $0.05."""
    return 0.01 if symbol_root in S.ETFS or price < 3 else 0.05


def round_tick(root: str, price: float, up: bool = False) -> float:
    t = tick(root, price)
    n = price / t
    return round((math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)) * t, 2)


def contract_problems(c: dict, underlying: str, today: date, earnings: date | None) -> list:
    """Why a contract fails 'What to trade' (empty list = passes). c has: expiry, delta, bid, ask, oi, volume."""
    bad = []
    dte = (c["expiry"] - today).days
    if not S.MIN_DTE <= dte <= S.MAX_DTE:
        bad.append(f"{dte} days to expiry")
    d = abs(c.get("delta") or 0)
    if not S.MIN_DELTA <= d <= S.MAX_DELTA:
        bad.append(f"delta {d:.2f}")
    bid, ask = c.get("bid") or 0, c.get("ask") or 0
    if bid <= 0 or ask <= 0 or ask < bid:
        bad.append("no two-sided quote")
    else:
        mid, spread = (bid + ask) / 2, ask - bid
        if underlying in S.ETFS and spread > S.MAX_SPREAD_ETF + 1e-9:
            bad.append(f"spread ${spread:.2f}")
        elif spread > S.MAX_SPREAD_PCT * mid + 1e-9:
            bad.append(f"spread {spread / mid:.0%} of mid")
    if (c.get("oi") or 0) < S.MIN_OI:
        bad.append(f"open interest {c.get('oi') or 0:.0f}")
    if c.get("volume") is not None and c["volume"] < S.MIN_VOLUME:
        bad.append(f"volume {c['volume']:.0f}")
    if earnings and today <= earnings <= c["expiry"]:
        bad.append(f"earnings {earnings} before expiry")
    return bad


def pick_contract(cands: list, underlying: str, today: date, earnings: date | None, budget: float):
    """Best passing contract that fits at least 1 whole contract in `budget`, or (None, reasons)."""
    ok, reasons = [], {}
    for c in cands:
        bad = contract_problems(c, underlying, today, earnings)
        if not bad and contracts_for(budget, c["ask"]) < 1:
            bad = [f"1 contract costs ${c['ask'] * 100:.0f}, budget ${budget:.0f}"]
        if bad:
            for b in bad:
                key = b.split(" ")[0]
                reasons[key] = reasons.get(key, 0) + 1
            continue
        ok.append(c)
    if not ok:
        return None, reasons

    def rank(c):
        dte = (c["expiry"] - today).days
        friday = c["expiry"].weekday() == 4 and dte >= 3
        return (not friday, dte, abs(abs(c["delta"]) - 0.475), (c["ask"] - c["bid"]) / c["ask"])
    return sorted(ok, key=rank)[0], reasons


# ---------- sizing ----------

def contracts_for(budget: float, ask: float) -> int:
    return int(budget // (ask * 100)) if ask > 0 else 0


def budget_for(tier: str, equity: float, cash: float) -> float:
    pct = S.FULL_PCT if tier == "full" else S.STANDARD_PCT
    return max(0.0, min(equity * pct, cash))


def stop_pct(tier: str) -> float:
    return S.FULL_STOP if tier == "full" else S.STANDARD_STOP


def choose_tier(sig: Signal, grade: str, spy_ok: bool, st: dict, week: str) -> tuple[str, str]:
    """Full port only when: enabled, Claude grades it A+, two setups agree, SPY agrees, the last trade
    was not a loss, no full port yet this week, and not in the post-losing-streak standard-only period."""
    why = []
    if not st.get("full_enabled", S.FULL_PORT_ENABLED):
        why.append("full port switched off")
    if grade != "A+":
        why.append(f"grade {grade}")
    if not sig.extra.get("also"):
        why.append("only one setup")
    if not spy_ok:
        why.append("SPY not agreeing")
    if st.get("last_result") == "loss":
        why.append("last trade lost")
    if st.get("full_week") == week and st.get("full_count", 0) >= S.FULL_PER_WEEK:
        why.append("already used this week's full port")
    if st.get("standard_only_left", 0) > 0:
        why.append("standard size after losing streak")
    return ("standard", "; ".join(why)) if why else ("full", "A+ and every full-port rule met")


# ---------- risk limits ----------

def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def entry_block(st: dict, now: datetime, equity: float, deposits: float, has_position: bool,
                daytrades: int, fomc_days: set) -> str | None:
    """Why no new trade may open right now (None = allowed). `now` is New York time."""
    today = now.date().isoformat()
    if st.get("killed"):
        return "killed (send 'resume 1k' to restart)"
    if deposits > 0 and equity <= deposits * S.KILL_PCT:
        return "KILL: equity at or below the account floor"
    if st.get("paused"):
        return "paused from phone"
    if has_position:
        return "one position at a time"
    if not between(now, S.NEWS_START, S.ENTRY_END):
        return "outside entry hours"
    if today in fomc_days and between(now, "13:30", "14:30"):
        return "Fed decision window"
    if st.get("pause_until") and today < st["pause_until"]:
        return f"paused until {st['pause_until']} ({st.get('pause_why', '')})"
    day_start = st.get("day_start_equity") if st.get("day") == today else None
    if day_start and equity <= day_start * (1 - S.DAILY_LOSS):
        return f"down {S.DAILY_LOSS:.0%}+ today"
    if st.get("week") == iso_week(now.date()) and st.get("week_start_equity") and \
            equity <= st["week_start_equity"] * (1 - S.WEEKLY_LOSS):
        return f"down {S.WEEKLY_LOSS:.0%}+ this week"
    if st.get("day") == today and st.get("trades_day", 0) >= S.MAX_TRADES_DAY:
        return f"{S.MAX_TRADES_DAY} trades today already"
    if st.get("week") == iso_week(now.date()) and st.get("trades_week", 0) >= S.MAX_TRADES_WEEK:
        return f"{S.MAX_TRADES_WEEK} trades this week already"
    if equity < 25_000 and daytrades >= 3:
        return "3 day trades used in 5 days (Alpaca may still apply the old PDT rule)"
    return None


def add_trading_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def after_close(st: dict, result: str, today: date) -> dict:
    """Update streak counters when a trade closes. result: win | loss | flat."""
    st = dict(st)
    st["last_result"] = result
    if st.get("standard_only_left", 0) > 0:
        st["standard_only_left"] -= 1
    st["loss_streak"] = st.get("loss_streak", 0) + 1 if result == "loss" else 0
    if st["loss_streak"] >= S.STREAK_LOSSES:
        st["pause_until"] = add_trading_days(today, S.STREAK_PAUSE_DAYS + 1).isoformat()
        st["pause_why"] = f"{S.STREAK_LOSSES} losses in a row"
        st["standard_only_left"] = S.STREAK_STANDARD_TRADES
        st["loss_streak"] = 0
    return st


# ---------- exits ----------

@dataclass
class Exit:
    qty: int
    reason: str
    kind: str               # stop | setup_stop | tp1 | tp2 | trail | time | expiry


def exit_decision(p: dict, mark: float, now: datetime, last_close: float | None, next_trading_day: date) -> Exit | None:
    """What to sell now, if anything. p: entry, qty, tier, direction, level, expiry (date), tp1_done,
    high, stop (current stop price). `last_close` = underlying's last closed 5-min bar."""
    entry, qty = p["entry"], p["qty"]
    gain = mark / entry - 1
    if mark <= p["stop"]:
        return Exit(qty, f"stop hit ({gain:+.0%})", "stop")
    if last_close is not None and p.get("level"):
        if (p["direction"] == "call" and last_close < p["level"]) or (p["direction"] == "put" and last_close > p["level"]):
            return Exit(qty, f"setup failed: underlying closed {last_close:.2f} back through {p['level']:.2f}", "setup_stop")
    if p["expiry"] <= next_trading_day and now.time() >= hm(S.EXPIRY_CLOSE):
        return Exit(qty, "expires next trading day", "expiry")
    if now.time() >= hm(S.TIME_STOP):
        dte = (p["expiry"] - now.date()).days
        if p["tier"] == "full" or not (gain >= S.HOLD_OVERNIGHT_GAIN and dte >= S.HOLD_OVERNIGHT_DTE):
            return Exit(qty, f"time stop ({gain:+.0%})", "time")
    if not p.get("tp1_done") and gain >= S.TP1:
        sell = qty // 2 if qty >= 2 else qty
        return Exit(sell, f"take profit 1 (+{gain:.0%})", "tp1")
    if p.get("tp1_done"):
        if gain >= S.TP2:
            return Exit(qty, f"take profit 2 (+{gain:.0%})", "tp2")
        if p.get("high") and mark <= p["high"] * (1 - S.TRAIL):
            return Exit(qty, f"trailing stop {S.TRAIL:.0%} under the high ({gain:+.0%})", "trail")
    return None


# ---------- scorecard ----------

def scorecard(trades: list, cost_per_contract: float = 0.0) -> dict:
    """trades: closed trades with pnl, qty, cost, kind, slip (stop slippage in % points, or None).
    cost_per_contract: dollars subtracted per contract per round trip, because paper fills are better
    than real ones. Every number in the card is AFTER that cost; paper_net is the raw paper result."""
    adj = [t["pnl"] - cost_per_contract * (t.get("qty") or 0) for t in trades]
    wins = [p for p in adj if p > 0]
    losses = [-p for p in adj if p < 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    base = trades[0]["equity_before"] if trades else 0
    for p in adj:
        eq += p
        peak = max(peak, eq)
        if base:
            dd = max(dd, (peak - eq) / (base + peak))
    slips = [t["slip"] for t in trades if t.get("slip") is not None]
    return {"trades": len(trades), "win_rate": len(wins) / len(trades) if trades else 0.0,
            "profit_factor": (sum(wins) / sum(losses)) if losses else (math.inf if wins else 0.0),
            "net": sum(adj), "paper_net": sum(t["pnl"] for t in trades), "max_drawdown": dd,
            "avg_stop_slip": sum(slips) / len(slips) if slips else None}


def go_live_ready(card: dict, rule_breaks: int) -> list:
    """The plan's go-live bars that are NOT yet met (empty = ready)."""
    out = []
    if card["trades"] < 40:
        out.append(f"{card['trades']}/40 trades")
    if card["profit_factor"] < 1.3:
        out.append(f"profit factor {card['profit_factor']:.2f} < 1.3")
    if card["max_drawdown"] >= 0.35:
        out.append(f"max drawdown {card['max_drawdown']:.0%}")
    if card["avg_stop_slip"] is not None and card["avg_stop_slip"] >= 10:
        out.append(f"stop slippage {card['avg_stop_slip']:.0f} pts")
    if rule_breaks:
        out.append(f"{rule_breaks} rule breaks")
    return out
