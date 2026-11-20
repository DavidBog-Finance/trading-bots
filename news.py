"""
NEWS WATCHER: pulls every new Alpaca/Benzinga headline (stocks and crypto) and
scores each one for the symbols it names. Local AI and Claude work together:

  1. The local model (Ollama, free) screens every headline first.
  2. Claude scores the ones that matter: news on anything the agents hold,
     critical tags (hack, delisting, guidance cut...), anything the local model
     rates as material, plus a small random sample to check the local model.
  3. Claude is paced (claude_client.allow): as the hour's allowance fills,
     lower-priority headlines keep their local score instead of waiting.
  4. Each night, calibrate() compares the two on the double-checked headlines and
     moves the escalation threshold, so Claude is used where the local model is weakest.

Without local AI (LOCAL_LLM_ENABLED=false) Claude scores everything, as before.
Each score is:

  score        -1 (very bad for the price) .. +1 (very good)
  materiality   0 (noise) .. 1 (moves the price for days)
  tag          earnings, mna_target, hack_or_exploit, ...

Scores are saved in logs/state.db. The agents read them, and a material
headline on something the agents hold (or a big market story) wakes the agent
right away instead of waiting for its next check-in.

  python news.py      # one poll, prints what it scored
"""
import json
from datetime import datetime, timedelta

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

import broker as B
import claude_client as CC
import config as C
import local_llm as L
import state

BATCH = 40
TAGS = ["earnings", "guidance", "mna_target", "mna_acquirer", "analyst", "product", "regulatory", "legal",
        "offering_dilution", "bankruptcy", "management", "macro", "hack_or_exploit", "delisting",
        "partnership", "other"]

SYSTEM = """You score financial news headlines for an automated trading system.

For each article, and for each ticker it is tagged with, judge the likely effect on that
ticker's price over the next 1-5 trading days, relative to the overall market.

score: -1.0 (strongly negative) to 1.0 (strongly positive). 0 means no clear direction.
materiality: 0.0 (routine noise, e.g. a list of stocks to watch, a recycled story, a minor
price target tweak) to 1.0 (clearly moves the price, e.g. earnings surprise with guidance
change, FDA decision, acquisition announcement, exchange hack, delisting, fraud charges).
tag: the best-fitting category.
reason: at most 15 words.

Rules:
- Score only what the headline and summary say. Do not guess at facts not given.
- If a company is being acquired at a fixed price, tag mna_target: its price is pinned and
  momentum signals stop working, so the trading system avoids it.
- Crypto tickers look like BTCUSD or ETHUSD.
- Market-wide stories (Fed, CPI, tariffs) that are tagged to an index ETF: score the ETF.
- Skip tickers that are only mentioned in passing (return nothing for them)."""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "symbol": {"type": "string"},
                    "score": {"type": "number"},
                    "materiality": {"type": "number"},
                    "tag": {"type": "string", "enum": TAGS},
                    "reason": {"type": "string"},
                },
                "required": ["id", "symbol", "score", "materiality", "tag", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _clamp(x, lo, hi):
    return max(lo, min(hi, float(x)))


def fetch_new_articles():
    """New articles since the last poll (at most the last 6 hours on first run)."""
    last = state.get("news_last_ts")
    start = state.now_utc() - timedelta(hours=6)
    if last:
        start = max(state.now_utc() - timedelta(days=2), datetime.fromisoformat(last))
    nc = NewsClient(C.ALPACA_API_KEY, C.ALPACA_SECRET_KEY)
    res = nc.get_news(NewsRequest(start=start, sort="asc", limit=200, include_content=False))
    out = []
    for a in res.data.get("news", []):
        if state.seen_news(int(a.id)):
            continue
        ts = a.created_at.isoformat()
        state.add_news(int(a.id), ts, a.symbols or [], a.headline or "", (a.summary or "")[:400])
        if a.symbols:
            out.append({"id": int(a.id), "time": ts, "symbols": a.symbols[:8],
                        "headline": a.headline, "summary": (a.summary or "")[:400]})
        state.put("news_last_ts", ts)
    return out


CRITICAL_TAGS = {"hack_or_exploit", "delisting", "bankruptcy", "mna_target", "offering_dilution", "guidance", "legal"}
LOCAL_BATCH = 12  # small batches suit an 8B model


def _valid(items, by_id):
    for it in items:
        art = by_id.get(it.get("id"))
        if art and it.get("symbol") in art["symbols"]:  # ignore anything a model invented
            yield art, it


def local_score(articles) -> dict:
    """First pass on the local model. Returns {(news_id, symbol): row}."""
    by_id = {a["id"]: a for a in articles}
    items = L.chat_json(SYSTEM, json.dumps(articles), SCHEMA)["items"]
    out = {}
    for art, it in _valid(items, by_id):
        row = dict(news_id=art["id"], symbol=it["symbol"], ts=art["time"], score=_clamp(it["score"], -1, 1),
                   materiality=_clamp(it["materiality"], 0, 1), tag=it.get("tag", "other"))
        state.add_local_score(**row)
        state.add_score(**row, reason=str(it.get("reason", ""))[:200], source="local")
        out[(art["id"], it["symbol"])] = {**row, "reason": it.get("reason", ""), "source": "local",
                                          "headline": art["headline"]}
    return out


def escalation(article, local_rows: dict, held: set, threshold: float) -> str | None:
    """Why (if at all) a headline should also go to Claude, as a pacing priority."""
    syms = article["symbols"]
    if any(s in held for s in syms):
        return "held"
    rows = [local_rows[(article["id"], s)] for s in syms if (article["id"], s) in local_rows]
    if not rows:
        return "material"  # local model skipped or failed it: let Claude decide
    if any(r["tag"] in CRITICAL_TAGS and r["materiality"] >= 0.4 for r in rows):
        return "critical"
    if max(r["materiality"] for r in rows) >= threshold:
        return "material"
    if article["id"] % 100 < C.AUDIT_SHARE * 100:  # deterministic random sample
        return "audit"
    return None


def calibrate() -> float:
    """Nightly: move the escalation threshold based on how the local model did on Claude's audits."""
    thr = state.get("escalate_threshold", C.ESCALATE_MATERIALITY)
    pairs = state.local_vs_claude(7)
    audited = [p for p in pairs if p["l_mat"] < thr]
    escalated = [p for p in pairs if p["l_mat"] >= thr]
    if len(audited) >= 20:
        missed = sum(p["c_mat"] >= 0.6 for p in audited) / len(audited)
        wasted = sum(p["c_mat"] < 0.4 for p in escalated) / len(escalated) if escalated else 0
        if missed > 0.15:
            thr = max(0.3, thr - 0.05)   # local model misses too much: send Claude more
        elif missed < 0.05 and wasted > 0.5:
            thr = min(0.75, thr + 0.05)  # Claude mostly confirms noise: send it less
        state.put("escalate_threshold", round(thr, 2))
    return thr


def score(articles, source_note: str = ""):
    """Ask Claude to score a batch. Returns list of dicts and stores them."""
    by_id = {a["id"]: a for a in articles}
    if CC.use_pro():
        items = CC.cli_json("news", SYSTEM, json.dumps(articles), SCHEMA)["items"]
    else:
        resp = CC.create(
            "news", C.NEWS_MODEL, max_tokens=8000,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": json.dumps(articles)}],
        )
        items = json.loads(CC.text_of(resp))["items"]
    saved = []
    for art, it in _valid(items, by_id):
        row = dict(news_id=it["id"], symbol=it["symbol"], ts=art["time"],
                   score=_clamp(it["score"], -1, 1), materiality=_clamp(it["materiality"], 0, 1),
                   tag=it["tag"], reason=it["reason"][:200])
        state.add_score(**row, source="claude")
        saved.append({**row, "source": "claude", "headline": art["headline"]})
    return saved


def poll(held: set | None = None):
    """One poll: local screen, then Claude for what matters, paced. Returns the newly scored rows."""
    held = {h.replace("/", "") for h in (held or set())}
    arts = fetch_new_articles()
    local_rows = {}
    if C.LOCAL_LLM_ENABLED:
        for i in range(0, len(arts), LOCAL_BATCH):
            try:
                local_rows.update(local_score(arts[i:i + LOCAL_BATCH]))
            except L.LocalUnavailable as e:
                B.log.warning("news: %s; Claude scores these instead", e)
                break
    final = dict(local_rows)
    if not C.LOCAL_LLM_ENABLED:
        queues = {"material": arts}
    else:
        thr = state.get("escalate_threshold", C.ESCALATE_MATERIALITY)
        queues = {}
        for a in arts:
            why = escalation(a, local_rows, held, thr)
            if why:
                queues.setdefault(why, []).append(a)
    sent = 0
    for priority in ("held", "critical", "material", "audit"):
        batch = queues.get(priority, [])
        for i in range(0, len(batch), BATCH):
            if not CC.allow(priority):
                B.log.info("news: Claude paced; %d %s headlines keep their local score", len(batch) - i, priority)
                break
            try:
                for r in score(batch[i:i + BATCH]):
                    final[(r["news_id"], r["symbol"])] = r
                sent += len(batch[i:i + BATCH])
            except CC.BudgetExceeded as e:
                B.log.warning("news: %s", e)
                break
    rows = list(final.values())
    B.log.info("news: %d articles, %d local scores, %d sent to Claude", len(arts), len(local_rows), sent)
    return rows


if __name__ == "__main__":
    for r in poll():
        print(f"{r['symbol']:>8} {r['score']:+.2f} m{r['materiality']:.1f} {r['tag']:<16} {r['headline'][:80]}")
