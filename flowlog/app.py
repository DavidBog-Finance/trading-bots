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
from datetime import date, datetime
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
                {"Title": title, "Priority": "low", "Tags": "whale"})


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
    days = max(30, (now.date() - oldest).days + 20)
    s = B.stock_daily_closes(symbol, days=days)
    if now.hour * 60 + now.minute < 16 * 60 + 20:   # today's bar is still forming: ignore it
        s = s[s.index < pd.Timestamp(now.date())]
    return s


def fill_outcomes(now: datetime | None = None, closes_fn=None) -> list[str]:
    """Fill 1-day / 1-week results that are now known. Returns lines for newly finished ones."""
    now = now or now_et()
    closes_fn = closes_fn or _closes
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
            S.update(r["id"], base=base, **{f"m_{h}": stock, f"spy_{h}": spy_move, f"v_{h}": v, f"d_{h}": str(when)})
            spy_txt = f", SPY {spy_move:+.1%}" if spy_move is not None else ""
            done.append(f"#{r['id']} {r['symbol']} {r['direction']} -> {'1 day' if h == '1d' else '1 week'}: "
                        f"{stock:+.1%}{spy_txt} = {v}")
    return done


def export(path: str) -> int:
    rs = S.rows()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        cols = ["id", "alert_day", "symbol", "expiry", "strike", "kind", "side", "premium", "tags", "direction",
                "guess", "base", "m_1d", "spy_1d", "v_1d", "m_1w", "spy_1w", "v_1w", "text"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rs:
            w.writerow(dict(r, tags=" ".join(r["tags"])))
    return len(rs)


def run_once(now: datetime | None = None):
    ingest_phone()
    done = fill_outcomes(now)
    if done:
        say("\n".join(done[:20]) + (f"\n...and {len(done) - 20} more" if len(done) > 20 else ""), "Flow results")
    now = now or now_et()
    week = now.strftime("%G-W%V")
    if now.weekday() == 4 and now.hour >= 17 and S.get("weekly_sent") != week and S.rows():
        S.put("weekly_sent", week)
        say(O.scorecard(S.rows()), "Flow weekly scorecard")
    B.log.info("flow log: %d alerts stored, %d results filled", len(S.rows()), len(done))


def watch(every_sec: int = 300):
    """Always-on computer: check the phone topic every few minutes, results after the close."""
    while True:
        try:
            run_once()
        except Exception as e:
            B.log.warning("flow log pass failed: %s", e)
        time.sleep(every_sec)
