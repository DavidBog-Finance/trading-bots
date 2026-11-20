"""Flow log jobs: read alerts you send from your phone, fill in what the stock did, push results.
Network (ntfy, Alpaca daily bars) lives here; parse.py and outcomes.py stay pure.

Phone: open ntfy.sh/<FLOW_TOPIC> in Safari (the iPhone app only receives) and send one alert per line,
or one of these words:
    report         scorecard so far
    list           the last 10 alerts with their numbers
    delete 12      remove alert #12 (a typo)
    help           this list
Replies come to your normal NTFY_TOPIC."""
import csv
import json
import os
import time
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import broker as B
import config as C
from flowlog import outcomes as O
from flowlog import parse as P
from flowlog import store as S

ET = ZoneInfo("America/New_York")
FLOW_TOPIC = os.getenv("FLOW_TOPIC", "")
HELP = ("Send one alert per line, e.g.\nAAPL 12/20 230C ask $250K sweep\n"
        "Add 'on 11/18' for an older alert, 'guess bull' or 'guess bear' to test yourself.\n"
        "Words: report | list | delete N | help")


def now_et() -> datetime:
    return datetime.now(ET)


def say(text: str, title: str = "Flow log"):
    """Push to your phone. Unlike B.notify this does not copy the text into the log, because GitHub
    run logs on a public repo are public and alert data is for personal use only."""
    B.log.info("flow log: push '%s' (%d lines)", title, len(text.splitlines()))
    if C.NTFY_TOPIC:
        B._post(f"https://ntfy.sh/{C.NTFY_TOPIC}", text.encode(),
                {"Title": title, "Priority": "default", "Tags": "whale"})


def add_text(text: str, when: datetime | None = None) -> list[str]:
    """Log every line that parses. Returns one reply line per input line."""
    when = when or now_et()
    replies = []
    for line in [x.strip() for x in text.splitlines() if x.strip()]:
        try:
            a = P.parse(line, when.date())
        except ValueError as e:
            replies.append(f"Skipped '{line[:40]}': {e}")
            continue
        day = a.alert_day or when.date()
        n = S.add(a, when.isoformat(timespec="minutes"), day.isoformat())
        read = {"bullish": "bullish (expects up)", "bearish": "bearish (expects down)"}.get(
            a.direction, "unclear (no ask/bid side given)")
        extra = ""
        if a.expiry and (a.expiry - day).days <= 3:
            extra = " Note: 0-3 days to expiry, often a lotto bet or a hedge."
        if a.side == "bid":
            extra += " Bid side can also be someone closing a position."
        replies.append(f"#{n} {a.short()} on {day:%a %m/%d}: read {read}.{extra}")
    return replies


def command(text: str) -> str | None:
    """Phone words. None = not a command (treat as alerts)."""
    words = text.strip().lower().split()
    if not words:
        return None
    if words[0] == "help" and len(words) == 1:
        return HELP
    if words[0] in ("report", "scorecard") and len(words) == 1:
        return O.scorecard(S.rows())
    if words[0] == "list" and len(words) == 1:
        rs = S.rows()[-10:]
        if not rs:
            return "No alerts logged yet."
        return "\n".join(f"#{r['id']} {r['alert_day'][5:]} {r['text'][:50]} | 1d {_fmt(r, '1d')} 1w {_fmt(r, '1w')}"
                         for r in rs)
    if words[0] == "delete" and len(words) == 2 and words[1].lstrip("#").isdigit():
        n = int(words[1].lstrip("#"))
        return f"Deleted #{n}." if S.delete(n) else f"No alert #{n}."
    return None


def _fmt(r: dict, h: str) -> str:
    if r.get(f"m_{h}") is None:
        return "pending"
    return f"{r[f'm_{h}']:+.1%} {r[f'v_{h}']}"


def ingest_phone() -> int:
    """Read new messages from FLOW_TOPIC. ntfy.sh keeps messages about 12 hours, so this must run
    at least that often (GitHub: every 2 hours; own computer: every few minutes)."""
    if not FLOW_TOPIC:
        return 0
    since = S.get("ntfy_since") or "12h"
    try:
        with urllib.request.urlopen(f"https://ntfy.sh/{FLOW_TOPIC}/json?poll=1&since={since}", timeout=15) as r:
            lines = r.read().decode().splitlines()
    except Exception as e:
        B.log.warning("flow topic read failed: %s", e)
        return 0
    count = 0
    for line in lines:
        msg = json.loads(line)
        if msg.get("event") != "message":
            continue
        S.put("ntfy_since", msg["id"])
        body = msg.get("message", "")
        reply = command(body)
        if reply is None:
            when = datetime.fromtimestamp(msg.get("time", time.time()), ET)
            reply = "\n".join(add_text(body, when))
            count += 1
        say(reply)
    return count


def _closes(symbol: str, oldest: date, now: datetime) -> pd.Series:
    days = max(220, (now.date() - oldest).days + 200)   # ~120 sessions before the alert, for beta
    s = B.stock_daily_closes(symbol, days=days)
    if now.hour * 60 + now.minute < 16 * 60 + 20:   # today's bar is still forming: ignore it
        s = s[s.index < pd.Timestamp(now.date())]
    return s


def _option_closes(code: str, oldest: date, now: datetime) -> pd.Series:
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionBarsRequest
    from alpaca.data.timeframe import TimeFrame
    start = datetime(oldest.year, oldest.month, oldest.day) - pd.Timedelta(days=7)
    bars = OptionHistoricalDataClient(C.ALPACA_API_KEY, C.ALPACA_SECRET_KEY).get_option_bars(
        OptionBarsRequest(symbol_or_symbols=code, timeframe=TimeFrame.Day, start=start))
    rows = bars.data.get(code, [])
    s = pd.Series([float(b.close) for b in rows],
                  index=pd.to_datetime([b.timestamp for b in rows], utc=True).tz_convert("America/New_York")
                  .normalize().tz_localize(None), dtype=float)
    if now.hour * 60 + now.minute < 16 * 60 + 20:
        s = s[s.index < pd.Timestamp(now.date())]
    return s


def fill_outcomes(now: datetime | None = None, closes_fn=None, option_fn=None) -> list[str]:
    """Fill 1-day / 1-week results that are now known. Returns lines for newly finished ones."""
    now = now or now_et()
    closes_fn = closes_fn or _closes
    option_fn = option_fn or _option_closes
    todo = S.rows("v_1d is null or v_1w is null")
    if not todo:
        return []
    oldest = min(date.fromisoformat(r["alert_day"]) for r in todo)
    cache: dict[str, pd.Series | None] = {}

    def series(sym):
        if sym not in cache:
            try:
                cache[sym] = closes_fn(sym, oldest, now)
            except Exception as e:
                B.log.warning("closes for %s failed: %s", sym, e)
                cache[sym] = None
        return cache[sym]

    opts: dict[str, pd.Series | None] = {}

    def opt_series(code):
        if code not in opts:
            try:
                opts[code] = option_fn(code, oldest, now)
            except Exception as e:
                B.log.warning("option prices failed: %s", type(e).__name__)
                opts[code] = None
        return opts[code]

    spy = series("SPY")
    done = []
    for r in todo:
        s = series(r["symbol"])
        if s is None:
            continue
        day = date.fromisoformat(r["alert_day"])
        for h, n in O.HORIZONS.items():
            if r.get(f"v_{h}"):
                continue
            m = O.move(s, day, n)
            if m is None:
                continue
            base, later, when = m
            stock = O.pct(base, later)
            sm = O.move(spy, day, n) if spy is not None else None
            spy_move = O.pct(sm[0], sm[1]) if sm else None
            v = O.verdict(r["direction"], stock, spy_move)
            om = None
            if r.get("expiry") and r.get("strike") and r.get("kind"):
                exp = date.fromisoformat(r["expiry"])
                code = O.occ(r["symbol"], exp, r["kind"], r["strike"])
                om = O.option_move(opt_series(code), s, day, n, exp, r["strike"], r["kind"])
            if r.get("beta") is None and spy is not None:
                r["beta"] = O.beta(s, spy, day)
            S.update(r["id"], base=base, beta=r.get("beta"),
                     **{f"m_{h}": stock, f"spy_{h}": spy_move, f"v_{h}": v, f"d_{h}": str(when), f"o_{h}": om})
            spy_txt = f", SPY {spy_move:+.1%}" if spy_move is not None else ""
            opt_txt = f"; the option {om:+.0%}" if om is not None else ""
            done.append(f"#{r['id']} {r['symbol']} {r['direction']} -> {'1 day' if h == '1d' else '1 week'}: "
                        f"{stock:+.1%}{spy_txt} = {v}{opt_txt}")
    return done


def last_session(now: datetime) -> date:
    """The most recent weekday whose session has closed (holidays simply find no volume)."""
    d = now.date()
    if now.weekday() >= 5 or now.hour * 60 + now.minute < 16 * 60 + 20:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def scan(now: datetime | None = None, scan_fn=None, catch_up: bool = False) -> list[str]:
    """Daily free scan (after the close): log the watchlist's most unusual option volume, tagged "scan".
    catch_up=True scans the last closed session even if it is not today (the GitHub button)."""
    from flowlog import scan as SC
    now = now or now_et()
    day = last_session(now)
    if not catch_up and day != now.date():
        return []
    if S.get("scan_day") == day.isoformat():
        return []
    if scan_fn is None:
        from portfolio1k import settings as PS
        alerts = SC.run(day, PS.WATCHLIST, log=B.log.warning)
    else:
        alerts = scan_fn(day)
    lines = []
    for a in alerts:
        n = S.add(a, now.isoformat(timespec="minutes"), day.isoformat())
        read = a.direction if a.direction != "unclear" else "unclear (mixed buying and selling)"
        lines.append(f"#{n} {a.text}: {read}")
    S.put("scan_day", day.isoformat())
    if lines:
        say("Today's biggest unusual option volume on your watchlist (estimated side):\n" + "\n".join(lines),
            "Flow scan")
    return lines


def export(path: str) -> int:
    rs = S.rows()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        cols = ["id", "alert_day", "symbol", "expiry", "strike", "kind", "side", "premium", "tags", "direction",
                "guess", "base", "beta", "m_1d", "spy_1d", "v_1d", "o_1d", "m_1w", "spy_1w", "v_1w", "o_1w", "text"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rs:
            w.writerow(dict(r, tags=" ".join(r["tags"])))
    return len(rs)


def run_once(now: datetime | None = None, scan_too: bool = False):
    ingest_phone()
    if scan_too:
        try:
            scan(now)
        except Exception as e:
            B.log.warning("flow scan failed: %s", e)
    done = fill_outcomes(now)
    daily = [d for d in done if "-> 1 week" not in d]
    if daily:
        say("\n".join(daily[:20]) + (f"\n...and {len(daily) - 20} more" if len(daily) > 20 else ""), "Flow results")
    # 1-week results are saved up and sent together on Friday evening with the scorecard
    S.put("week_queue", (S.get("week_queue") or []) + [d for d in done if "-> 1 week" in d])
    now = now or now_et()
    week = now.strftime("%G-W%V")
    if now.weekday() == 4 and now.hour >= 17 and S.get("weekly_sent") != week and S.rows():
        S.put("weekly_sent", week)
        queued = S.get("week_queue") or []
        S.put("week_queue", [])
        head = ("1-week results this week:\n" + "\n".join(queued[:25]) + "\n\n") if queued else ""
        say(head + O.scorecard(S.rows()), "Flow weekly results")
    B.log.info("flow log: %d alerts stored, %d results filled", len(S.rows()), len(done))


def watch(every_sec: int = 300):
    """Always-on computer: check the phone topic every few minutes, results after the close."""
    while True:
        try:
            run_once(scan_too=True)
        except Exception as e:
            B.log.warning("flow log pass failed: %s", e)
        time.sleep(every_sec)
