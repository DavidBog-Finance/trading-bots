"""
SCORECARD: is the AI earning its keep? Run daily by the home server, pushed weekly.

  News:   for every headline scored at least 5 trading days ago, what did the stock do
          next (1, 3, 5 days), relative to SPY? Reported separately for Claude's scores
          and the local model's, so you can see what each is worth.
  Agents: every agent buy, from entry to exit (or today), relative to SPY (stocks)
          or BTC (crypto). Positive "edge" means the agent beat simply holding those.

  python scorecard.py        # print it now
"""
from datetime import timedelta

import numpy as np
import pandas as pd

import broker as B
import market as M
import state

HORIZONS = (1, 3, 5)


def forward_excess(closes: pd.DataFrame, symbol: str, bench: str, day: pd.Timestamp, h: int):
    """Return of `symbol` minus `bench` from the close of `day` (or the next session) over h sessions."""
    if symbol not in closes or bench not in closes:
        return None
    s = closes[symbol].dropna()
    b = closes[bench].dropna()
    after = s.index[s.index >= day.normalize()]
    if len(after) <= h:
        return None
    t0, t1 = after[0], after[h]
    if t0 not in b.index or t1 not in b.index:
        return None
    return float(s[t1] / s[t0] - 1) - float(b[t1] / b[t0] - 1)


def news_table(rows: list, closes: pd.DataFrame) -> pd.DataFrame:
    """rows: scores (symbol, ts, score, materiality, source). Adds excess returns per horizon."""
    out = []
    for r in rows:
        day = pd.Timestamp(r["ts"][:10])
        rec = {"source": r.get("source", "claude"), "score": r["score"], "materiality": r["materiality"]}
        for h in HORIZONS:
            rec[f"ex{h}"] = forward_excess(closes, r["symbol"], "SPY", day, h)
        if rec["ex1"] is not None:
            out.append(rec)
    return pd.DataFrame(out)


def summarize_news(df: pd.DataFrame) -> dict:
    res = {}
    if df.empty:
        return res
    for src, g in df.groupby("source"):
        material = g[g.materiality >= 0.5]
        d = {"headlines": len(g)}
        for h in HORIZONS:
            col = g[f"ex{h}"].dropna()
            if len(col) >= 10:
                d[f"corr_{h}d"] = round(float(np.corrcoef(g.loc[col.index, "score"], col)[0, 1]), 3)
        if len(material) >= 10:
            m = material.dropna(subset=["ex3"])
            d["hit_rate_material_3d"] = round(float((np.sign(m.score) == np.sign(m.ex3)).mean()), 3)
            pos, neg = m[m.score >= 0.5].ex3, m[m.score <= -0.5].ex3
            d["avg_3d_after_good_news"] = round(float(pos.mean()), 4) if len(pos) else None
            d["avg_3d_after_bad_news"] = round(float(neg.mean()), 4) if len(neg) else None
        res[src] = d
    return res


def agent_trades(closes_stock: pd.DataFrame, closes_crypto: pd.DataFrame) -> pd.DataFrame:
    """Each agent buy -> (agent, symbol, days, return, benchmark return, edge)."""
    with state.db() as con:
        orders = [dict(r) for r in con.execute("select * from agent_orders order by ts")]
    out = []
    for o in orders:
        if o["side"] != "buy":
            continue
        crypto = o["agent"] == "crypto"
        closes = closes_crypto if crypto else closes_stock
        sym = o["symbol"][:-3] + "/USD" if crypto and "/" not in o["symbol"] else o["symbol"]
        bench = "BTC/USD" if crypto else "SPY"
        if sym not in closes or bench not in closes:
            continue
        t0 = pd.Timestamp(o["day"])
        sell = next((x for x in orders if x["symbol"] == o["symbol"] and x["side"] == "sell" and x["ts"] > o["ts"]), None)
        s, b = closes[sym].dropna(), closes[bench].dropna()
        a0 = s.index[s.index >= t0]
        if len(a0) < 2:
            continue
        t1 = s.index[s.index <= pd.Timestamp(sell["day"])][-1] if sell else s.index[-1]
        if t1 not in b.index or a0[0] not in b.index or t1 <= a0[0]:
            continue
        ret = float(s[t1] / s[a0[0]] - 1)
        bret = float(b[t1] / b[a0[0]] - 1)
        out.append({"agent": o["agent"], "symbol": sym, "open": sell is None, "days": (t1 - a0[0]).days,
                    "return": ret, "benchmark": bret, "edge": ret - bret})
    return pd.DataFrame(out)


def build(days: int = 90) -> dict:
    rows = [r for r in state.recent_scores(hours=days * 24) if not r["symbol"].endswith("USD")]
    syms = sorted({r["symbol"] for r in rows} | {"SPY"})
    with state.db() as con:
        held = {r[0] for r in con.execute("select distinct symbol from agent_orders")}
    stock_syms = sorted(set(syms) | {s for s in held if not s.endswith("USD")} | {"SPY"})
    coin_syms = sorted({s[:-3] + "/USD" for s in held if s.endswith("USD")} | {"BTC/USD"})
    closes_s = M.stock_closes(stock_syms, days + 15) if stock_syms else pd.DataFrame()
    closes_c = M.crypto_closes(coin_syms, days + 15) if coin_syms else pd.DataFrame()
    news = summarize_news(news_table(rows, closes_s))
    trades = agent_trades(closes_s, closes_c)
    agents = {}
    for a, g in (trades.groupby("agent") if not trades.empty else []):
        agents[a] = {"trades": len(g), "open": int(g.open.sum()), "avg_edge": round(float(g.edge.mean()), 4),
                     "win_rate_vs_benchmark": round(float((g.edge > 0).mean()), 3)}
    card = {"as_of": state.today(), "news": news, "agents": agents,
            "escalate_threshold": state.get("escalate_threshold")}
    state.put("scorecard", card)
    return card


def text(card: dict) -> str:
    lines = []
    for src, d in card.get("news", {}).items():
        c3 = d.get("corr_3d")
        hit = d.get("hit_rate_material_3d")
        lines.append(f"News ({src}): {d['headlines']} headlines" + (f", score vs 3-day move corr {c3:+.2f}" if c3 is not None else "")
                     + (f", right direction {hit:.0%} on material news" if hit is not None else ""))
    for a, d in card.get("agents", {}).items():
        lines.append(f"{a.title()} agent: {d['trades']} buys, avg edge vs benchmark {d['avg_edge']:+.1%}, "
                     f"beat it {d['win_rate_vs_benchmark']:.0%} of the time")
    if not lines:
        lines.append("Not enough history yet. Give it a few weeks.")
    lines.append("Corr above +0.05 and a hit rate above 55% mean the news scores carry real signal.")
    return "\n".join(lines)


if __name__ == "__main__":
    print(text(build()))
