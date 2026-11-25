"""
One place that calls Claude, so limits and error handling are the same for the
news watcher and both agents. Two backends (CLAUDE_BACKEND):

  pro  Runs `claude -p` (Claude Code, non-interactive) signed in with your Claude
       subscription. No API key. All of Claude Code's own tools are switched off,
       so Claude can only answer; it can't run commands or read files.
  api  The Claude API with ANTHROPIC_API_KEY. Opus calls opt into server-side
       fallbacks: if Opus's safety filter wrongly declines a request, Anthropic
       re-runs it on its recommended fallback model.
"""
import json
import os
import subprocess
import time

import anthropic

import config as C
import state

# USD per million tokens: (input, output, cache write, cache read). Estimates for the budget.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}
NO_FALLBACK = {"claude-haiku-5-5"}  # no server-side fallback for Haiku


class BudgetExceeded(Exception):
    """Out of API budget, or the Pro plan's usage limit is reached. Agents skip until later."""
    pass


SANDBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "claude_sandbox")


def use_pro() -> bool:
    return C.CLAUDE_BACKEND == "pro"


def available() -> str | None:
    """None if Claude can be called right now, otherwise why not."""
    if use_pro():
        until = state.get("pro_limited_until") or 0
        if time.time() < until:
            return f"Claude plan usage limit reached; retrying after {time.strftime('%H:%M', time.localtime(until))}"
        if not os.path.exists(C.CLAUDE_BIN):
            return f"Claude Code not found at {C.CLAUDE_BIN} (set CLAUDE_BIN)"
        return None
    if not C.ANTHROPIC_API_KEY:
        return "ANTHROPIC_API_KEY is not set"
    if state.spend_today() >= C.CLAUDE_DAILY_BUDGET_USD:
        return "Claude budget for today used"
    return None


# Share of the hourly Claude allowance each kind of work may use before it backs off to local AI.
PRIORITY_SHARE = {"agent": 1.0, "held": 1.0, "critical": 0.9, "material": 0.7, "audit": 0.4}


def allow(priority: str) -> bool:
    """Pacing: should this piece of work use Claude right now? Keeps Claude for what matters most,
    spread through the day, instead of spending it all early and running dry."""
    if available():
        return False
    used = state.claude_calls_since(3600)
    return used < C.CLAUDE_CALLS_PER_HOUR * PRIORITY_SHARE.get(priority, 0.5)


def clean_token(tok: str) -> str:
    """Drop spaces and line breaks picked up when the token was copied from a wrapped terminal line."""
    return "".join(tok.split())


def cli_json(purpose: str, system: str, prompt: str, schema: dict, timeout: int = 300) -> dict:
    """One `claude -p` call with no tools, returning JSON that matches `schema`."""
    why = available()
    if why:
        raise BudgetExceeded(why)
    os.makedirs(SANDBOX, exist_ok=True)  # empty working folder: nothing for Claude Code to see
    cmd = [C.CLAUDE_BIN, "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
           "--system-prompt", system, "--json-schema", json.dumps(schema)]
    if C.CLAUDE_CODE_MODEL:
        cmd += ["--model", C.CLAUDE_CODE_MODEL]
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # make sure the plan is used
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        # `claude setup-token` wraps the token over two lines, so a copy-paste often keeps the line break.
        env["CLAUDE_CODE_OAUTH_TOKEN"] = clean_token(env["CLAUDE_CODE_OAUTH_TOKEN"])
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=SANDBOX, env=env, timeout=timeout)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Claude Code failed ({r.returncode}): {(r.stderr or r.stdout)[:300]}")
    text = str(d.get("result", ""))
    if d.get("is_error") or d.get("subtype") != "success":
        if "limit" in text.lower() or d.get("api_error_status") == 429:
            state.put("pro_limited_until", time.time() + 3600)
            raise BudgetExceeded(f"Claude plan usage limit reached ({text[:120]})")
        if "log in" in text.lower() or "login" in text.lower() or "auth" in text.lower():
            raise RuntimeError("Claude Code is not signed in: run `claude` once and log in "
                               "(cloud: refresh the CLAUDE_CODE_OAUTH_TOKEN secret with `claude setup-token`). "
                               f"Claude Code said: {text[:200]}")
        raise RuntimeError(f"Claude Code error: {text[:300]}")
    # Not billed on a subscription; logged so you can see how much the bots use.
    state.add_spend(purpose, "pro-plan", 0.0)
    out = d.get("structured_output")
    if out is None:
        out = json.loads(text[text.index("{"):text.rindex("}") + 1])
    return out


_client = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not C.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (see .env.example)")
        _client = anthropic.Anthropic(api_key=C.ANTHROPIC_API_KEY, max_retries=3)
    return _client


def cost(model: str, usage) -> float:
    p_in, p_out, p_cw, p_cr = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (p_in * (usage.input_tokens or 0)
            + p_out * (usage.output_tokens or 0)
            + p_cw * (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            + p_cr * (getattr(usage, "cache_read_input_tokens", 0) or 0)) / 1e6


def check_budget(purpose: str):
    spent = state.spend_today()
    if spent >= C.CLAUDE_DAILY_BUDGET_USD:
        raise BudgetExceeded(f"Claude budget for today used (${spent:.2f} of ${C.CLAUDE_DAILY_BUDGET_USD:.2f})")
    if purpose == "news" and state.spend_today("news") >= C.NEWS_DAILY_BUDGET_USD:
        raise BudgetExceeded(f"news-scoring budget for today used (${C.NEWS_DAILY_BUDGET_USD:.2f})")


def create(purpose: str, model: str, **kwargs):
    """messages.create with budget check, fallbacks, cost logging and refusal check."""
    check_budget(purpose)
    extra = {}
    if model not in NO_FALLBACK:
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    resp = client().beta.messages.create(model=model, **extra, **kwargs)
    state.add_spend(purpose, model, cost(model, resp.usage))
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the {purpose} request")
    return resp


def text_of(resp) -> str:
    return "".join(b.text for b in resp.content if b.type == "text")
