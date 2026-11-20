"""Flow log memory: logs/flowlog.db (SQLite). On GitHub it rides in the Actions cache between runs;
on your own computer it is just a file."""
import json
import os
import sqlite3

DB_PATH = os.getenv("FLOW_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "flowlog.db"))

_SCHEMA = """
create table if not exists alerts(id integer primary key autoincrement, logged text, alert_day text,
    symbol text, expiry text, strike real, kind text, side text, premium real, tags text, direction text,
    guess text, text text, base real,
    m_1d real, spy_1d real, v_1d text, d_1d text,
    m_1w real, spy_1w real, v_1w text, d_1w text);
create table if not exists kv(key text primary key, value text);
"""


def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    for col in ("o_1d real", "o_1w real"):      # option results, added after the first version
        try:
            con.execute(f"alter table alerts add column {col}")
        except sqlite3.OperationalError:
            pass
    return con


def get(key: str, default=None):
    with db() as con:
        row = con.execute("select value from kv where key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def put(key: str, value):
    with db() as con:
        con.execute("insert or replace into kv values(?,?)", (key, json.dumps(value)))


def add(a, logged: str, alert_day: str) -> int:
    with db() as con:
        cur = con.execute(
            "insert into alerts(logged, alert_day, symbol, expiry, strike, kind, side, premium, tags, direction,"
            " guess, text) values(?,?,?,?,?,?,?,?,?,?,?,?)",
            (logged, alert_day, a.symbol, a.expiry.isoformat() if a.expiry else None, a.strike, a.kind, a.side,
             a.premium, json.dumps(a.tags), a.direction, a.guess, a.text))
        return int(cur.lastrowid)


def rows(where: str = "1=1", args=()) -> list[dict]:
    with db() as con:
        out = [dict(r) for r in con.execute(f"select * from alerts where {where} order by id", args)]
    for r in out:
        r["tags"] = json.loads(r["tags"] or "[]")
    return out


def update(alert_id: int, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with db() as con:
        con.execute(f"update alerts set {cols} where id=?", (*fields.values(), alert_id))


def delete(alert_id: int) -> bool:
    with db() as con:
        return con.execute("delete from alerts where id=?", (alert_id,)).rowcount > 0
