"""What the stock did after an alert, and the running scorecard. Pure functions, no network.

"Base" is the stock's close on the alert day (or the last close before it, for weekend alerts).
"1 day" is the next session's close, "1 week" the close 5 sessions later. Each move is also shown
next to SPY's move over the same days, because a call alert in a rising market proves little."""
from datetime import date

import pandas as pd

HORIZONS = {"1d": 1, "1w": 5}


def move(closes: pd.Series, day: date, sessions: int) -> tuple[float, float, date] | None:
    """(base close, later close, later date) or None if that later session has not closed yet."""
    if closes is None or closes.empty:
        return None
    idx = closes.index[closes.index <= pd.Timestamp(day)]
    if len(idx) == 0:
        return None
    pos = closes.index.get_loc(idx[-1])
    if pos + sessions >= len(closes):
        return None
    return float(closes.iloc[pos]), float(closes.iloc[pos + sessions]), closes.index[pos + sessions].date()


def occ(symbol: str, expiry: date, kind: str, strike: float) -> str:
    """Option contract code used by Alpaca, e.g. AAPL261218C00230000."""
    return f"{symbol}{expiry:%y%m%d}{'C' if kind == 'call' else 'P'}{round(strike * 1000):08d}"


def option_move(opt: pd.Series, stock: pd.Series, day: date, sessions: int, expiry: date,
                strike: float, kind: str) -> float | None:
    """How much the option itself gained or lost from the alert day's close to the same later
    session the stock is judged on. After expiry the option is worth what it pays at expiry
    (stock above the strike for a call, below for a put; otherwise zero)."""
    m = move(stock, day, sessions)
    if m is None or opt is None or opt.empty:
        return None
    later_day = pd.Timestamp(m[2])
    before = opt[opt.index <= pd.Timestamp(day)]
    if before.empty or not before.iloc[-1]:
        return None
    base = float(before.iloc[-1])
    if later_day.date() > expiry:
        s = stock[stock.index <= pd.Timestamp(expiry)]
        if s.empty:
            return None
        px = float(s.iloc[-1])
        value = max(0.0, px - strike) if kind == "call" else max(0.0, strike - px)
    else:
        upto = opt[opt.index <= later_day]
        value = float(upto.iloc[-1])
    return value / base - 1


def pct(base: float, later: float) -> float:
    return later / base - 1 if base else 0.0


def verdict(direction: str, stock: float, spy: float | None) -> str:
    """right / wrong for a bullish or bearish read; 'no read' when the side was unknown."""
    if direction not in ("bullish", "bearish"):
        return "no read"
    sign = 1 if direction == "bullish" else -1
    return "right" if sign * stock > 0 else "wrong"


def _rate(rows: list, key: str) -> str:
    done = [r for r in rows if r.get(key) in ("right", "wrong")]
    if not done:
        return "-"
    right = sum(r[key] == "right" for r in done)
    return f"{right}/{len(done)} ({right / len(done):.0%})"


def _avg_edge(rows: list, h: str) -> str:
    """Average move in the alert's direction minus SPY's move in that direction."""
    vals = []
    for r in rows:
        if r.get(f"v_{h}") not in ("right", "wrong") or r.get(f"m_{h}") is None:
            continue
        sign = 1 if r["direction"] == "bullish" else -1
        vals.append(sign * (r[f"m_{h}"] - (r.get(f"spy_{h}") or 0.0)))
    return f"{sum(vals) / len(vals):+.1%}" if vals else "-"


def _opt(rows: list, h: str) -> str:
    vals = [r[f"o_{h}"] for r in rows if r.get(f"o_{h}") is not None]
    if not vals:
        return "-"
    up = sum(v > 0 for v in vals)
    return f"avg {sum(vals) / len(vals):+.0%}, made money {up}/{len(vals)}"


def bucket(premium: float) -> str:
    if premium >= 1_000_000:
        return "$1M+"
    if premium >= 500_000:
        return "$500K-1M"
    if premium >= 100_000:
        return "$100-500K"
    return "under $100K"


def scorecard(rows: list) -> str:
    """Plain-text summary for the phone. rows: dicts with direction, premium, tags (list), guess,
    v_1d/v_1w (verdict), m_1d/m_1w (stock move), spy_1d/spy_1w."""
    if not rows:
        return "No alerts logged yet."
    out = [f"{len(rows)} alerts logged. Right = stock moved the way the flow pointed.",
           f"All:  1 day {_rate(rows, 'v_1d')}, 1 week {_rate(rows, 'v_1w')}, "
           f"edge vs SPY 1 week {_avg_edge(rows, '1w')}",
           f"The option itself (bought at the alert-day close): 1 day {_opt(rows, '1d')}; 1 week {_opt(rows, '1w')}"]
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(f"size {bucket(r.get('premium') or 0)}", []).append(r)
        for t in r.get("tags") or []:
            groups.setdefault(f"tag {t}", []).append(r)
        groups.setdefault(f"read {r['direction']}", []).append(r)
    for name in sorted(groups):
        g = groups[name]
        if any(x.get("v_1w") in ("right", "wrong") or x.get("v_1d") in ("right", "wrong") for x in g):
            out.append(f"{name}: 1d {_rate(g, 'v_1d')}, 1w {_rate(g, 'v_1w')}, edge 1w {_avg_edge(g, '1w')}, "
                       f"option 1w {_opt(g, '1w')}")
    guessed = [r for r in rows if r.get("guess")]
    if guessed:
        agree = sum((r["guess"] == "bull") == (r["direction"] == "bullish") for r in guessed
                    if r["direction"] in ("bullish", "bearish"))
        mine = [dict(r, direction="bullish" if r["guess"] == "bull" else "bearish") for r in guessed]
        for r in mine:
            for h in HORIZONS:
                if r.get(f"m_{h}") is not None:
                    r[f"v_{h}"] = verdict(r["direction"], r[f"m_{h}"], None)
        out.append(f"Your guesses: matched the flow read {agree}/{len(guessed)}; "
                   f"right 1d {_rate(mine, 'v_1d')}, 1w {_rate(mine, 'v_1w')}")
    n = sum(r.get("v_1w") in ("right", "wrong") for r in rows)
    if n < 30:
        out.append(f"Only {n} alerts have a 1-week result. Under ~30 this is mostly luck; keep logging.")
    return "\n".join(out)
