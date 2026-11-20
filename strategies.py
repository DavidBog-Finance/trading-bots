"""
Signal logic shared by the live bots AND the backtester.

Keeping one copy of the rules means the backtest tests exactly what the bot
trades. Every function here is pure: give it prices, get back target weights.
A weight of 1.0 means "put 100% of this bot's allocation in the asset",
0.0 means "all cash".
"""
import numpy as np
import pandas as pd


# ---------- indicators ----------

def rsi_wilder(close: pd.Series, n: int) -> pd.Series:
    """Classic Wilder RSI (what TradingView and most charting tools show)."""
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


# ---------- STOCK BOT: trend sleeve + RSI(2) dip sleeve on SPY ----------

def trend_positions(close: pd.Series, length: int = 200, band: float = 0.01) -> pd.Series:
    """Long when price is above its moving average (Faber 2007 / Siegel style).

    The band adds hysteresis: enter above SMA*(1+band), exit below SMA*(1-band),
    otherwise keep yesterday's state. That cuts whipsaw trades roughly in half.
    """
    sma = close.rolling(length).mean()
    pos = pd.Series(np.nan, index=close.index)
    pos[close > sma * (1 + band)] = 1.0
    pos[close < sma * (1 - band)] = 0.0
    return pos.ffill().fillna(0.0)


def rsi2_positions(close: pd.Series, entry: float = 10, exit_ma: int = 5,
                   trend_len: int = 200, rsi_len: int = 2) -> pd.Series:
    """Larry Connors' RSI(2) pullback rule (Short Term Trading Strategies That Work, 2008).

    Buy at the close when price > 200-day SMA and RSI(2) < entry.
    Sell at the close when price > 5-day SMA.
    """
    r = rsi_wilder(close, rsi_len).values
    up = (close > close.rolling(trend_len).mean()).values
    ma = close.rolling(exit_ma).mean().values
    c = close.values
    pos = np.zeros(len(close))
    holding = False
    for i in range(len(close)):
        if not holding and up[i] and r[i] < entry:
            holding = True
        elif holding and c[i] > ma[i]:
            holding = False
        pos[i] = 1.0 if holding else 0.0
    return pd.Series(pos, index=close.index)


def stock_positions(close: pd.Series, mode: str = "blend", **kw) -> pd.Series:
    """Target weight series for the stock bot.

    mode="trend": 100% trend sleeve (more growth, deeper drawdowns)
    mode="blend": 50% trend sleeve + 50% RSI(2) sleeve (smoother, the default)
    """
    t = trend_positions(close, kw.get("trend_len", 200), kw.get("band", 0.01))
    if mode == "trend":
        return t
    m = rsi2_positions(close, kw.get("rsi_entry", 10), kw.get("exit_ma", 5), kw.get("trend_len", 200))
    return 0.5 * t + 0.5 * m


# ---------- CRYPTO BOT: trend ensemble + volatility targeting on BTC ----------

SMA_LENGTHS = (50, 100, 150)
MOMENTUM_LOOKBACKS = (28, 56, 120)


def crypto_raw_weights(close: pd.Series, target_vol: float = 0.40, vol_window: int = 30,
                       sma_lengths=SMA_LENGTHS, lookbacks=MOMENTUM_LOOKBACKS) -> pd.Series:
    """Unbanded target weight for each day.

    1. Trend score = average of 6 yes/no votes: price above 50/100/150-day SMA,
       and 28/56/120-day return > 0. Averaging many lookbacks instead of picking
       the single best one is the main defence against overfitting.
    2. Scale by target_vol / realised 30-day volatility, capped at 1 (no leverage).
       BTC usually runs 50-80% annualised vol, so the bot typically holds 40-80%
       of its allocation when the trend is fully up.
    """
    votes = [(close > close.rolling(n).mean()).astype(float) for n in sma_lengths]
    votes += [(close.pct_change(n) > 0).astype(float) for n in lookbacks]
    score = pd.concat(votes, axis=1).mean(axis=1)
    vol = close.pct_change().rolling(vol_window).std() * np.sqrt(365)
    return (score * (target_vol / vol).clip(upper=1.0)).fillna(0.0)


def apply_rebalance_band(raw: pd.Series, band: float = 0.10) -> pd.Series:
    """Only trade when the target moves >= band away from what we hold (or goes to 0).

    This mirrors the live bot, which compares the target to its actual position.
    """
    out = np.zeros(len(raw))
    cur = 0.0
    for i, x in enumerate(raw.values):
        if needs_rebalance(cur, x, band):
            cur = x
        out[i] = cur
    return pd.Series(out, index=raw.index)


def needs_rebalance(current: float, target: float, band: float = 0.10) -> bool:
    return abs(target - current) >= band or (target == 0 and current != 0)


def crypto_positions(close: pd.Series, target_vol: float = 0.40, band: float = 0.10) -> pd.Series:
    return apply_rebalance_band(crypto_raw_weights(close, target_vol), band)


# ---------- OPTIONS BOT: SPY put credit spreads (bull put spreads) ----------
# Sell a ~25-delta put about 45 days out, buy a put ~3% of the stock price lower,
# only while SPY's trend sleeve is on. Take profit at 50% of the credit, otherwise
# close at 21 days to expiry. One spread position at a time.

OPT_TARGET_DELTA = 0.25
OPT_TARGET_DTE = 45
OPT_MIN_DTE, OPT_MAX_DTE = 30, 60
OPT_WIDTH_PCT = 0.03
OPT_TAKE_PROFIT = 0.50
OPT_EXIT_DTE = 21


def choose_expiry(expiries, today, target=OPT_TARGET_DTE, lo=OPT_MIN_DTE, hi=OPT_MAX_DTE):
    """Expiry date closest to `target` days out, within [lo, hi]; None if there isn't one."""
    ok = [e for e in expiries if lo <= (e - today).days <= hi]
    return min(ok, key=lambda e: abs((e - today).days - target)) if ok else None


def pick_put_spread(puts, spot, target_delta=OPT_TARGET_DELTA, width_pct=OPT_WIDTH_PCT):
    """puts: list of dicts with strike, delta (negative for puts), bid, ask.

    Returns (short, long) dicts or None. The long strike is the listed strike closest
    to short_strike - width_pct*spot (and strictly below the short strike).
    """
    usable = [p for p in puts if p.get("delta") is not None and p["bid"] > 0 and p["ask"] > 0]
    if not usable:
        return None
    short = min(usable, key=lambda p: abs(abs(p["delta"]) - target_delta))
    want = short["strike"] - width_pct * spot
    lower = [p for p in puts if p["strike"] < short["strike"] and p["ask"] > 0]
    if not lower:
        return None
    long = min(lower, key=lambda p: abs(p["strike"] - want))
    return short, long


def spread_mid_credit(short, long):
    return (short["bid"] + short["ask"]) / 2 - (long["bid"] + long["ask"]) / 2


def spread_contracts(risk_budget_usd, width, credit):
    """How many spreads fit inside the risk budget. Max loss per spread = (width - credit) * 100."""
    max_loss = (width - credit) * 100
    if max_loss <= 0:
        return 0
    return int(risk_budget_usd // max_loss)


def spread_exit_reason(entry_credit, current_value, dte, take_profit=OPT_TAKE_PROFIT, exit_dte=OPT_EXIT_DTE):
    """Why to close an open spread now, or None to keep holding."""
    if entry_credit > 0 and current_value <= entry_credit * (1 - take_profit):
        return f"take profit ({take_profit:.0%} of credit captured)"
    if dte <= exit_dte:
        return f"{dte} days to expiry (exit at {exit_dte})"
    return None


# ---------- MARKET SCANNER (feeds the Claude agents) ----------
# Ranks any number of symbols with rules that have long out-of-sample records:
# 12-1 month momentum scaled by volatility (Jegadeesh-Titman 1993, Barroso &
# Santa-Clara 2015), only names above their 200-day average (Faber 2007).
# The agents see this ranking plus the news; it does not trade by itself.

def symbol_stats(close: pd.Series) -> dict:
    """Plain-number facts about one price series (daily closes, oldest first)."""
    c = close.dropna()
    r = c.pct_change()
    last = float(c.iloc[-1])

    def ret(n):
        return float(c.iloc[-1] / c.iloc[-1 - n] - 1) if len(c) > n else None

    out = {
        "price": round(last, 4),
        "ret_1d": ret(1), "ret_5d": ret(5), "ret_1m": ret(21), "ret_3m": ret(63),
        "mom_12_1": float(c.iloc[-22] / c.iloc[-253] - 1) if len(c) > 253 else None,
        "above_sma50": bool(last > c.tail(50).mean()) if len(c) >= 50 else None,
        "above_sma200": bool(last > c.tail(200).mean()) if len(c) >= 200 else None,
        "vol_ann": float(r.tail(63).std() * np.sqrt(252)) if len(c) > 63 else None,
        "rsi2": float(rsi_wilder(c, 2).iloc[-1]) if len(c) > 10 else None,
        "off_52w_high": float(last / c.tail(252).max() - 1),
    }
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}


def rank_momentum(closes: pd.DataFrame, news_tilt: dict | None = None, news_weight: float = 1.0,
                  min_days: int = 253) -> pd.DataFrame:
    """closes: one column per symbol. Returns rows sorted best first with a `score` column.

    score = z-score of (12-1 momentum / volatility) + news_weight * news tilt (-1..1),
    only for symbols trading above their 200-day average.
    """
    rows = {}
    for sym in closes.columns:
        c = closes[sym].dropna()
        if len(c) < min_days:
            continue
        st = symbol_stats(c)
        if not st["above_sma200"] or not st["vol_ann"]:
            continue
        rows[sym] = {**st, "risk_adj_mom": st["mom_12_1"] / st["vol_ann"]}
    df = pd.DataFrame.from_dict(rows, orient="index")
    if df.empty:
        return df
    z = (df["risk_adj_mom"] - df["risk_adj_mom"].mean()) / (df["risk_adj_mom"].std() or 1.0)
    df["news"] = [float((news_tilt or {}).get(s, 0.0)) for s in df.index]
    df["score"] = z + news_weight * df["news"]
    return df.sort_values("score", ascending=False)


def crypto_trend_vote(close: pd.Series) -> float:
    """Share of the crypto bot's 6 trend signals that are on today (0..1)."""
    votes = [close.iloc[-1] > close.tail(n).mean() for n in SMA_LENGTHS]
    votes += [close.iloc[-1] > close.iloc[-1 - n] for n in MOMENTUM_LOOKBACKS]
    return float(np.mean(votes))
