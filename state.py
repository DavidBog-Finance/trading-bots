"""
Small SQLite store (logs/state.db) so the Mac runner remembers things between
restarts: scored news, Claude spend, what the agents bought and why, cooldowns.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "logs", "state.db")

_SCHEMA = """
create table if not exists kv(key text primary key, value text);
create table if not exists news(id integer primary key, ts text, symbols text, headline text, summary text);
create table if not exists scores(news_id integer, symbol text, ts text, score real, materiality real,
                                  tag text, reason text, primary key(news_id, symbol));
create table if not exists spend(ts text, day text, purpose text, model text, usd real);
create table if not exists entries(symbol text primary key, agent text, day text, thesis text);
create table if not exists agent_orders(ts text, day text, agent text, symbol text, side text, usd real, reason text);
create table if not exists cooldown(symbol text primary key, until text, why text);
create table if not exists journal(ts text, agent text, trigger text, summary text, actions text);
create table if not exists local_scores(news_id integer, symbol text, ts text, score real, materiality real,
                                        tag text, primary key(news_id, symbol));
"""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today() -> str:
    """Trading 'day' in New York time (what the PDT rule and daily limits use)."""
    import zoneinfo
    return now_utc().astimezone(zoneinfo.ZoneInfo("America/New_York")).date().isoformat()


def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    try:  # older databases: remember who scored each headline
        con.execute("alter table scores add column source text default 'claude'")
    except sqlite3.OperationalError:
        pass
    return con


# ---------- key/value ----------

def get(key: str, default=None):
    with db() as con:
        row = con.execute("select value from kv where key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def put(key: str, value):
    with db() as con:
        con.execute("insert or replace into kv values(?,?)", (key, json.dumps(value)))


# ---------- news ----------

def seen_news(news_id: int) -> bool:
    with db() as con:
        return con.execute("select 1 from news where id=?", (news_id,)).fetchone() is not None


def add_news(news_id: int, ts: str, symbols, headline: str, summary: str):
    with db() as con:
        con.execute("insert or ignore into news values(?,?,?,?,?)",
                    (news_id, ts, ",".join(symbols), headline, summary))


def add_score(news_id: int, symbol: str, ts: str, score: float, materiality: float, tag: str, reason: str,
              source: str = "claude"):
    with db() as con:
        con.execute("insert or replace into scores(news_id, symbol, ts, score, materiality, tag, reason, source) "
                    "values(?,?,?,?,?,?,?,?)", (news_id, symbol, ts, score, materiality, tag, reason, source))


def add_local_score(news_id: int, symbol: str, ts: str, score: float, materiality: float, tag: str):
    with db() as con:
        con.execute("insert or replace into local_scores values(?,?,?,?,?,?)",
                    (news_id, symbol, ts, score, materiality, tag))


def local_vs_claude(days: int = 7):
    """Headlines scored by both, for calibrating the local model."""
    since = (now_utc() - timedelta(days=days)).isoformat()
    with db() as con:
        return [dict(r) for r in con.execute(
            "select l.score as l_score, l.materiality as l_mat, s.score as c_score, s.materiality as c_mat "
            "from local_scores l join scores s on s.news_id=l.news_id and s.symbol=l.symbol "
            "where s.source='claude' and l.ts >= ?", (since,))]


def claude_calls_since(seconds: float) -> int:
    since = (now_utc() - timedelta(seconds=seconds)).isoformat()
    with db() as con:
        return con.execute("select count(*) from spend where ts >= ?", (since,)).fetchone()[0]


def recent_scores(symbol: str | None = None, hours: float = 72, min_materiality: float = 0.0):
    since = (now_utc() - timedelta(hours=hours)).isoformat()
    q = ("select s.*, n.headline from scores s join news n on n.id = s.news_id "
         "where s.ts >= ? and s.materiality >= ?")
    args = [since, min_materiality]
    if symbol:
        q += " and s.symbol = ?"
        args.append(symbol)
    with db() as con:
        return [dict(r) for r in con.execute(q + " order by s.ts desc", args)]


def news_tilt(symbol: str, hours: float = 72, half_life_h: float = 24) -> float:
    """Materiality-weighted, time-decayed average news score in [-1, 1] (0 = no news)."""
    rows = recent_scores(symbol, hours)
    num = den = 0.0
    for r in rows:
        age_h = (now_utc() - datetime.fromisoformat(r["ts"])).total_seconds() / 3600
        trust = 1.0 if r.get("source", "claude") == "claude" else 0.6  # local-only scores count for less
        w = trust * r["materiality"] * 0.5 ** (age_h / half_life_h)
        num += w * r["score"]
        den += w
    return num / den if den > 0 else 0.0


# ---------- Claude spend ----------

def add_spend(purpose: str, model: str, usd: float):
    with db() as con:
        con.execute("insert into spend values(?,?,?,?,?)", (now_utc().isoformat(), today(), purpose, model, usd))


def spend_today(purpose: str | None = None) -> float:
    q, args = "select coalesce(sum(usd),0) from spend where day=?", [today()]
    if purpose:
        q, args = q + " and purpose=?", args + [purpose]
    with db() as con:
        return con.execute(q, args).fetchone()[0]


def spend_month() -> float:
    with db() as con:
        return con.execute("select coalesce(sum(usd),0) from spend where day like ?", (today()[:7] + "%",)).fetchone()[0]


# ---------- agent bookkeeping ----------

def log_agent_order(agent: str, symbol: str, side: str, usd: float, reason: str):
    with db() as con:
        con.execute("insert into agent_orders values(?,?,?,?,?,?,?)",
                    (now_utc().isoformat(), today(), agent, symbol, side, usd, reason))
        if side == "buy":
            con.execute("insert or ignore into entries values(?,?,?,?)", (symbol, agent, today(), reason))


def drop_entry(symbol: str):
    with db() as con:
        con.execute("delete from entries where symbol=?", (symbol,))


def entry(symbol: str):
    with db() as con:
        r = con.execute("select * from entries where symbol=?", (symbol,)).fetchone()
    return dict(r) if r else None


def orders_today(agent: str) -> int:
    with db() as con:
        return con.execute("select count(*) from agent_orders where day=? and agent=?", (today(), agent)).fetchone()[0]


def bought_today(symbol: str) -> bool:
    with db() as con:
        return con.execute("select 1 from agent_orders where day=? and symbol=? and side='buy'",
                           (today(), symbol)).fetchone() is not None


def set_cooldown(symbol: str, days: float, why: str):
    until = (now_utc() + timedelta(days=days)).isoformat()
    with db() as con:
        con.execute("insert or replace into cooldown values(?,?,?)", (symbol, until, why))


def cooldown(symbol: str):
    with db() as con:
        r = con.execute("select * from cooldown where symbol=? and until > ?", (symbol, now_utc().isoformat())).fetchone()
    return dict(r) if r else None


def add_journal(agent: str, trigger: str, summary: str, actions: list):
    with db() as con:
        con.execute("insert into journal values(?,?,?,?,?)",
                    (now_utc().isoformat(timespec="seconds"), agent, trigger, summary, json.dumps(actions)))


def journal(agent: str, limit: int = 8):
    with db() as con:
        rows = con.execute("select * from journal where agent=? order by ts desc limit ?", (agent, limit)).fetchall()
    return [dict(r) for r in rows]


def runs_today(agent: str) -> int:
    with db() as con:
        return con.execute("select count(*) from journal where agent=? and ts >= ?",
                           (agent, today())).fetchone()[0]


def calls_today() -> int:
    with db() as con:
        return con.execute("select count(*) from spend where day=?", (today(),)).fetchone()[0]
