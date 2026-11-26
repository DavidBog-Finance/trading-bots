"""Portfolio 1k memory: its own SQLite file (logs/portfolio1k.db), separate from the other bots.
Holds the risk-limit state, the open trade, the trade log and Claude's notes. In GitHub Actions
the file is carried between runs by the Actions cache; on a home computer it just sits on disk."""
import json
import os
import sqlite3
from datetime import datetime, timezone

from portfolio1k import settings as S

_SCHEMA = """
create table if not exists kv(key text primary key, value text);
create table if not exists trades(id integer primary key autoincrement, opened text, closed text, symbol text,
    underlying text, setup text, direction text, tier text, qty integer, entry real, exit real, pnl real,
    cost real, equity_before real, kind text, slip real, reason text, claude text);
create table if not exists events(ts text, kind text, text text);
"""
# Cost log columns, added to older databases on first open.
_COST_COLS = {"feed": "text", "entry_bid": "real", "entry_ask": "real", "entry_slip": "real", "entry_slip_pct": "real",
              "entry_spread_pct": "real", "exit_bid": "real", "exit_ask": "real", "exit_slip": "real",
              "exit_slip_pct": "real", "fees": "real", "shadow": "text",
              "context": "text", "mfe": "real", "mae": "real"}


def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(S.DB_PATH), exist_ok=True)
    con = sqlite3.connect(S.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    have = {r[1] for r in con.execute("pragma table_info(trades)")}
    for col, typ in _COST_COLS.items():
        if col not in have:
            con.execute(f"alter table trades add column {col} {typ}")
    return con


def get(key: str, default=None):
    with db() as con:
        row = con.execute("select value from kv where key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def put(key: str, value):
    with db() as con:
        con.execute("insert or replace into kv values(?,?)", (key, json.dumps(value)))


def state() -> dict:
    return get("risk", {}) or {}


def save_state(st: dict):
    put("risk", st)


def event(kind: str, text: str):
    with db() as con:
        con.execute("insert into events values(?,?,?)", (datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, text[:2000]))


def events_since(ts: str) -> list:
    """Events at or after a UTC ISO time, oldest first."""
    with db() as con:
        return [dict(r) for r in con.execute("select * from events where ts >= ? order by ts", (ts,))]


def events(limit: int = 20) -> list:
    with db() as con:
        return [dict(r) for r in con.execute("select * from events order by ts desc limit ?", (limit,))]


def open_trade(**row) -> int:
    cols = ",".join(row)
    with db() as con:
        cur = con.execute(f"insert into trades({cols}) values({','.join('?' * len(row))})", list(row.values()))
        return cur.lastrowid


def close_trade(tid: int, **row):
    sets = ",".join(f"{k}=?" for k in row)
    with db() as con:
        con.execute(f"update trades set {sets} where id=?", [*row.values(), tid])


def closed_trades() -> list:
    with db() as con:
        return [dict(r) for r in con.execute("select * from trades where closed is not null order by id")]
