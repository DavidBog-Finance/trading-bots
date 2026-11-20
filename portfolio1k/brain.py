"""Claude's part in Portfolio 1k. Claude never places orders and never changes limits:
  1. Morning game plan: reads overnight news for the watchlist, notes the day's risks and a bias per name.
  2. Trade review: when a setup fires, Claude sees the setup, the chart numbers, SPY, news and the
     picked contract, and answers take/skip plus a grade (A+ / A / B). Code decides size from the grade
     and the full-port rules. If Claude is unavailable, setups 1 and 2 still trade at standard size.
  3. News scoring (news.py) feeds setup 3."""
import json

import broker as B
import claude_client as CC
import config as C
import state as global_state

PLAN_SYSTEM = """You are the pre-market analyst for a small options day-trading account that buys 2-7 day
calls or puts on this watchlist only. You are given overnight and recent headlines with scores.
Write a short game plan: what matters today, which names have a clear bias and why, and any risk
(scheduled macro events, earnings, big gaps) that should make the trader more careful.
Be concrete and brief. Do not invent facts not in the input."""

PLAN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary", "bias", "avoid"],
               "properties": {
                   "summary": {"type": "string"},
                   "bias": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                       "required": ["symbol", "direction", "why"],
                                                       "properties": {"symbol": {"type": "string"},
                                                                      "direction": {"type": "string", "enum": ["call", "put", "none"]},
                                                                      "why": {"type": "string"}}}},
                   "avoid": {"type": "array", "items": {"type": "string"},
                             "description": "watchlist symbols to skip today, with no other text"}}}

REVIEW_SYSTEM = """You review one proposed options day trade for a $1,000-class account. Hard rules (stops,
take-profits, size caps, time limits) are enforced by code; your job is judgment: does this setup make
sense right now given the chart numbers, the market (SPY), the news and the morning game plan?

Grade it:
  A+  exceptional: the setup is clean, the news/market strongly agree, nothing argues against it
  A   a good, normal setup
  B   marginal: something argues against it (choppy tape, news against it, late in the move)
take=false for B or anything you would not put real money on. Be strict: skipping is cheap,
bad trades are not. reason: at most 30 words, written for the account owner."""

REVIEW_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["take", "grade", "reason"],
                 "properties": {"take": {"type": "boolean"},
                                "grade": {"type": "string", "enum": ["A+", "A", "B"]},
                                "reason": {"type": "string"}}}


def ask(purpose: str, system: str, payload: dict, schema: dict) -> dict:
    prompt = json.dumps(payload, default=str)
    if CC.use_pro():
        return CC.cli_json(purpose, system, prompt, schema, timeout=180)
    resp = CC.create(purpose, C.AGENT_MODEL, max_tokens=2000, system=system,
                     output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
                     messages=[{"role": "user", "content": prompt}])
    return json.loads(CC.text_of(resp))


def available() -> str | None:
    return CC.available()


def game_plan(watchlist: list, earnings: dict) -> dict | None:
    news = [r for r in global_state.recent_scores(hours=18) if r["symbol"] in watchlist][:60]
    try:
        return ask("p1k_plan", PLAN_SYSTEM, {"watchlist": watchlist, "earnings_next_10_days": earnings,
                                             "headlines": news}, PLAN_SCHEMA)
    except (CC.BudgetExceeded, RuntimeError) as e:
        B.log.warning("[1k] no game plan today: %s", e)
        return None


def review(payload: dict) -> dict | None:
    """None = Claude unavailable (caller falls back to the rules)."""
    if not CC.allow("agent"):
        return None
    try:
        return ask("p1k_review", REVIEW_SYSTEM, payload, REVIEW_SCHEMA)
    except (CC.BudgetExceeded, RuntimeError) as e:
        B.log.warning("[1k] trade review unavailable: %s", e)
        return None
