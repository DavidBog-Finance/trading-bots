"""
CLAUDE TRADING AGENTS: a stock agent and a crypto agent.

Each run, Claude looks at its portfolio, the whole-market scan, live prices and
scored news, decides what (if anything) to buy or sell, and explains why. It can
pick any liquid stock/ETF or any Alpaca coin. Runs are triggered by the Mac
runner (daemon.py) on a schedule AND whenever something happens: a material
headline, a holding moving sharply, or the market dropping.

Claude never touches the API keys or the broker directly. Every order goes
through RiskGate below, which enforces limits in code that the agent cannot talk
its way around: sleeve size, max per position, max positions, max orders per
day, no shorting/leverage/options, no illiquid or penny stocks, PDT protection,
cooldowns after stop-outs, the account loss limit and the kill switch.

  python agent.py stock "manual check"      # one run now
  python agent.py crypto "manual check"
"""
import json
import sys
from datetime import datetime

import broker as B
import claude_client as CC
import config as C
import market as M
import state
import strategies as S

MAX_TURNS = 14

AGENTS = {
    "stock": {"pct": lambda: C.AGENT_STOCK_PCT, "cap": lambda: C.STOCK_MAX_USD, "stop": lambda: C.AGENT_STOCK_STOP_PCT,
              "asset": "us_equity", "crypto": False},
    "crypto": {"pct": lambda: C.AGENT_CRYPTO_PCT, "cap": lambda: C.CRYPTO_MAX_USD, "stop": lambda: C.AGENT_CRYPTO_STOP_PCT,
               "asset": "crypto", "crypto": True},
}


def agent_positions(client, agent: str) -> list:
    """Positions that belong to this agent: right asset class, not owned by the rules bots."""
    core = M.core_symbols()
    want = AGENTS[agent]["asset"]
    return [p for p in client.get_all_positions()
            if str(getattr(p, "asset_class", "")).split(".")[-1].lower() == want and p.symbol not in core]


def to_order_symbol(agent: str, symbol: str) -> str:
    """Crypto positions come back as BTCUSD; orders and data use BTC/USD."""
    symbol = symbol.upper().strip()
    if agent == "crypto" and "/" not in symbol and symbol.endswith("USD"):
        return symbol[:-3] + "/USD"
    return symbol


# ---------- the hard limits ----------

class RiskGate:
    def __init__(self, client, agent: str):
        self.client, self.agent = client, agent
        self.cfg = AGENTS[agent]
        acct = client.get_account()
        self.acct = acct
        self.equity = float(acct.equity)
        self.cash = max(0.0, float(acct.cash))
        self.sleeve = B.sleeve_budget(self.equity, self.cfg["pct"](), self.cfg["cap"]())
        self.positions = {p.symbol: p for p in agent_positions(client, agent)}
        self.invested = sum(float(p.market_value or 0) for p in self.positions.values())
        self.unrealized = sum(float(p.unrealized_pl or 0) for p in self.positions.values())
        self._universe = None

    def held(self, symbol: str):
        return self.positions.get(symbol.replace("/", "")) or self.positions.get(symbol)

    def available(self) -> float:
        return max(0.0, min(self.sleeve - self.invested, self.cash))

    def universe(self) -> set:
        if self._universe is None:
            u = M.crypto_universe(self.client) if self.cfg["crypto"] else M.stock_universe(self.client)
            self._universe = set(u)
        return self._universe

    def buys_paused(self) -> str | None:
        if state.get(f"paused_{self.agent}") or state.get("paused_all"):
            return "agent is paused from your phone"
        if self.sleeve > 0 and -self.unrealized > C.AGENT_SLEEVE_MAX_DD * self.sleeve:
            return f"open losses exceed {C.AGENT_SLEEVE_MAX_DD:.0%} of the sleeve; sells only"
        return None

    def common(self, symbol: str) -> str | None:
        if C.KILL_SWITCH:
            return "kill switch is on"
        if state.orders_today(self.agent) >= C.AGENT_MAX_ORDERS_PER_DAY:
            return f"daily order limit ({C.AGENT_MAX_ORDERS_PER_DAY}) reached"
        if symbol in M.core_symbols():
            return "symbol belongs to the rules bots"
        if not self.cfg["crypto"] and not self.client.get_clock().is_open:
            return "stock market is closed"
        return None

    def check_buy(self, symbol: str, usd: float) -> str | None:
        """None if allowed, otherwise the reason it is blocked."""
        why = self.common(symbol) or self.buys_paused()
        if why:
            return why
        if symbol not in self.universe():
            return "not in the tradable universe (illiquid, under $5, leveraged/inverse ETF, or not on Alpaca)"
        cd = state.cooldown(symbol)
        if cd:
            return f"in cooldown until {cd['until'][:10]} ({cd['why']})"
        if usd < C.MIN_TRADE_USD:
            return f"order under ${C.MIN_TRADE_USD:.0f}"
        if usd > self.available() + 0.01:
            return f"only ${self.available():,.2f} available in this agent's sleeve"
        p = self.held(symbol)
        current = float(p.market_value) if p else 0.0
        cap = C.AGENT_MAX_POSITION_PCT * self.sleeve
        if current + usd > cap + 0.01:
            return f"position would exceed the per-position cap of ${cap:,.2f}"
        if not p and len(self.positions) >= C.AGENT_MAX_POSITIONS:
            return f"already at the max of {C.AGENT_MAX_POSITIONS} positions"
        if usd > C.MAX_ORDER_PCT * self.equity:
            return "order too large for the account"
        return None

    def check_sell(self, symbol: str) -> str | None:
        why = self.common(symbol)
        if why and not why.startswith("daily order limit"):  # selling is always allowed past the limit
            return why
        if not self.held(symbol):
            return "not held by this agent"
        return pdt_block(self.acct, self.equity, symbol, self.cfg["crypto"])


def pdt_block(acct, equity: float, symbol: str, crypto: bool) -> str | None:
    """Pattern-day-trader protection for accounts under $25k (stocks only)."""
    if crypto or equity >= 25_000 or not state.bought_today(symbol):
        return None
    if int(getattr(acct, "daytrade_count", 0) or 0) >= 3:
        return "selling today would be a 4th day trade in 5 days (PDT rule, account under $25k); sell tomorrow"
    return None


# ---------- tools the agent can call ----------

TOOLS = [
    {"name": "get_portfolio",
     "description": "This agent's sleeve: budget, cash available, each position with entry price, P&L, days held "
                    "and the thesis you wrote when buying, plus how many orders you have left today.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "scan_market",
     "description": "Whole-market scan. Stocks: ~1,000+ liquid US names ranked by volatility-scaled 12-1 month "
                    "momentum (only names above their 200-day average) plus a news tilt, today's top gainers/losers "
                    "and most active, and the SPY regime. Crypto: every Alpaca coin ranked by trend vote + news.",
     "input_schema": {"type": "object", "properties": {"top": {"type": "integer", "description": "rows, max 40"}},
                      "additionalProperties": False}},
    {"name": "lookup",
     "description": "Details for up to 10 symbols: live price and today's change, returns over 1d/5d/1m/3m/12-1m, "
                    "position vs 50/200-day averages, volatility, RSI(2), distance from 52-week high, and the latest "
                    "scored headlines. Stocks like NVDA; crypto like ETH/USD.",
     "input_schema": {"type": "object", "properties": {"symbols": {"type": "array", "items": {"type": "string"}}},
                      "required": ["symbols"], "additionalProperties": False}},
    {"name": "get_news",
     "description": "Recent Claude-scored headlines (score -1..1, materiality 0..1, tag). Omit symbol for the most "
                    "material news across the market.",
     "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}, "hours": {"type": "number"}},
                      "additionalProperties": False}},
    {"name": "buy",
     "description": "Buy `usd` dollars of a symbol at market (fractional). Checked against the hard limits; "
                    "returns the fill or the reason it was blocked. `thesis`: why, and what would make you sell.",
     "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}, "usd": {"type": "number"},
                                                       "thesis": {"type": "string"}},
                      "required": ["symbol", "usd", "thesis"], "additionalProperties": False}},
    {"name": "sell",
     "description": "Sell `fraction` (0-1, 1 = all) of a position at market. `reason` is shown to the owner.",
     "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}, "fraction": {"type": "number"},
                                                       "reason": {"type": "string"}},
                      "required": ["symbol", "fraction", "reason"], "additionalProperties": False}},
]


def system_prompt(agent: str) -> str:
    what = ("US stocks and ETFs (any liquid name, long only, fractional shares)" if agent == "stock"
            else "crypto spot pairs on Alpaca (any coin, long only)")
    stop = AGENTS[agent]["stop"]()
    return f"""You are the {agent} trading agent for one person's small Alpaca account. You trade {what}
with a slice of the account (your "sleeve"). Other parts of the account are run by tested rules bots
(SPY/index ETFs, BTC, SPY options); you never touch those symbols.

You wake up on a schedule and whenever something happens (a material headline, a holding moving sharply,
the market dropping). Each run: understand why you were woken, check your portfolio, look at what matters,
decide, act with the buy/sell tools, then finish with a short plain-English summary (3-6 sentences) of what
you did and why. That summary is saved as your journal and you will see your recent entries next time.

What the evidence says works, use it as your base:
- Momentum: stocks that rose most over the past 12 months (skipping the last month), scaled by volatility,
  tend to keep outperforming for months. scan_market ranks the whole market this way. Prefer names near the
  top of that ranking and in an uptrend. Crypto: coins with most trend signals on (trend_vote near 1).
- News: material news predicts the next few days' returns, mostly in the first day. Strong positive news on a
  name that is already trending is a reason to buy or add; material negative news on a holding (guidance cut,
  fraud, hack, delisting, dilution) is a reason to sell now rather than wait.
- Regime: when SPY is below its 200-day average (or BTC is in a downtrend for crypto), losses cluster. Hold
  fewer positions, smaller, or cash.
- Cut losers, let winners run. A position below its thesis should be sold, not averaged down.

Discipline:
- Doing nothing is a valid and often the best decision. Fees, spreads, taxes (short-term gains are taxed as
  income) and your own API cost all eat returns. Don't trade just because you were woken up.
- Don't chase: a stock already up 20%+ today on news has usually priced it in. Don't buy into earnings blind.
- Avoid acquisition targets (price pinned), biotech binary events and anything you can't explain.
- Size positions by conviction within the limits. Keep some cash unless the setup is strong.
- Every buy needs a thesis with a clear sell condition.

Hard limits enforced in code (orders that break them are rejected, so don't try):
- At most {C.AGENT_MAX_POSITIONS} positions, each at most {C.AGENT_MAX_POSITION_PCT:.0%} of the sleeve;
  at most {C.AGENT_MAX_ORDERS_PER_DAY} orders a day; buys only with cash available in the sleeve.
- Positions are sold automatically if they fall {stop:.0%} below entry, then can't be rebought for 10 days.
- Long only. No options, leverage, shorting, leveraged/inverse ETFs, stocks under ${C.AGENT_MIN_PRICE:.0f}
  or with under ${C.AGENT_MIN_DOLLAR_VOL / 1e6:.0f}M daily volume.
- Stocks: orders only while the market is open; a stock bought today may be blocked from selling today (PDT rule).
Be concise in tool use: a few targeted calls, not dozens."""


def _days_held(sym: str) -> int | None:
    e = state.entry(sym)
    return (datetime.fromisoformat(state.today()) - datetime.fromisoformat(e["day"])).days if e else None


def portfolio(gate: RiskGate) -> dict:
    pos = []
    for sym, p in gate.positions.items():
        e = state.entry(sym)
        pos.append({"symbol": sym, "value": round(float(p.market_value or 0), 2), "entry_price": float(p.avg_entry_price),
                    "price": float(p.current_price or 0), "pnl_pct": round(float(p.unrealized_plpc or 0) * 100, 2),
                    "today_pct": round(float(p.change_today or 0) * 100, 2), "days_held": _days_held(sym),
                    "thesis": e["thesis"] if e else "(bought before the agent kept notes)"})
    return {"sleeve_budget": round(gate.sleeve, 2), "invested": round(gate.invested, 2),
            "cash_available": round(gate.available(), 2), "positions": pos,
            "orders_left_today": max(0, C.AGENT_MAX_ORDERS_PER_DAY - state.orders_today(gate.agent)),
            "buys_paused": gate.buys_paused(), "account_equity": round(gate.equity, 2)}


def lookup(agent: str, symbols) -> dict:
    symbols = [to_order_symbol(agent, s) for s in symbols][:10]
    out = {}
    if agent == "crypto":
        closes = M.crypto_closes(symbols)
    else:
        closes = M.stock_closes(symbols, days=400)
    live = M.live_prices(symbols)
    for s in symbols:
        info = {}
        if s in closes:
            info.update(S.symbol_stats(closes[s].dropna()))
        if s in live:
            info["live_price"], info["today_pct"] = round(live[s][0], 4), round(live[s][1] * 100, 2)
        info["headlines"] = [{"t": r["ts"][:16], "score": r["score"], "m": r["materiality"], "tag": r["tag"],
                              "headline": r["headline"][:140]} for r in state.recent_scores(s.replace("/", ""), 72)[:6]]
        out[s] = info or {"error": "no data"}
    return out


def news(agent: str, symbol: str | None, hours: float) -> list:
    sym = to_order_symbol(agent, symbol).replace("/", "") if symbol else None
    rows = state.recent_scores(sym, min(hours or 24, 168), min_materiality=0.0 if sym else 0.5)
    if not sym:
        rows = [r for r in rows if r["symbol"].endswith("USD") == (agent == "crypto")]
    return [{"symbol": r["symbol"], "t": r["ts"][:16], "score": r["score"], "m": r["materiality"],
             "tag": r["tag"], "headline": r["headline"][:140], "why": r["reason"]} for r in rows[:30]]


def do_buy(gate: RiskGate, symbol: str, usd: float, thesis: str) -> dict:
    symbol = to_order_symbol(gate.agent, symbol)
    usd = round(float(usd), 2)
    why = gate.check_buy(symbol, usd)
    if why:
        return {"ok": False, "blocked": why}
    p = gate.held(symbol)
    current = float(p.market_value) if p else 0.0
    price = M.live_prices([symbol]).get(symbol, (0, 0))[0] or 1.0
    order = B.rebalance(gate.client, f"{gate.agent} agent", symbol, current + usd, price, crypto=gate.cfg["crypto"],
                        eq=gate.equity, note=thesis[:120])
    state.log_agent_order(gate.agent, symbol.replace("/", ""), "buy", usd, thesis)
    gate.__init__(gate.client, gate.agent)  # refresh cash/positions
    return {"ok": True, "status": B.fill_text(order) if order is not None else "dry run"}


def do_sell(gate: RiskGate, symbol: str, fraction: float, reason: str) -> dict:
    symbol = to_order_symbol(gate.agent, symbol)
    fraction = max(0.0, min(1.0, float(fraction)))
    why = gate.check_sell(symbol)
    if why:
        return {"ok": False, "blocked": why}
    p = gate.held(symbol)
    value = float(p.market_value)
    if fraction >= 0.99:
        B.flatten(gate.client, symbol, f"{gate.agent} agent", reason[:120], warn=False)
        state.drop_entry(symbol.replace("/", ""))
        order = None
    else:
        order = B.rebalance(gate.client, f"{gate.agent} agent", symbol, value * (1 - fraction),
                            float(p.current_price), crypto=gate.cfg["crypto"], eq=gate.equity, note=reason[:120])
    state.log_agent_order(gate.agent, symbol.replace("/", ""), "sell", value * fraction, reason)
    gate.__init__(gate.client, gate.agent)
    return {"ok": True, "sold_usd": round(value * fraction, 2),
            "status": B.fill_text(order) if order is not None else "closed"}


def run_tool(gate: RiskGate, name: str, args: dict):
    a = gate.agent
    if name == "get_portfolio":
        return portfolio(gate)
    if name == "scan_market":
        top = min(int(args.get("top", 25)), 40)
        return M.scan_crypto(gate.client, top) if a == "crypto" else M.scan_stocks(gate.client, top)
    if name == "lookup":
        syms = args.get("symbols") or args.get("symbol") or []
        return lookup(a, syms.replace(" ", "").split(",") if isinstance(syms, str) else syms)
    if name == "get_news":
        return news(a, args.get("symbol"), args.get("hours", 24))
    if name == "buy":
        return do_buy(gate, args["symbol"], args["usd"], args["thesis"])
    if name == "sell":
        return do_sell(gate, args["symbol"], args["fraction"], args["reason"])
    return {"error": f"unknown tool {name}"}


# ---------- one agent run ----------

def can_run(agent: str) -> str | None:
    if not C.AGENTS_ENABLED:
        return "agents disabled"
    if AGENTS[agent]["pct"]() <= 0:
        return "sleeve is 0%"
    if state.runs_today(agent) >= C.AGENT_MAX_RUNS_PER_DAY:
        return "max runs for today reached"
    if not CC.allow("agent"):
        return CC.available() or "Claude's hourly allowance is used; the run waits for the next slot"
    return None


def _call_tool(gate: RiskGate, name: str, args: dict, actions: list) -> tuple[dict, bool]:
    try:
        out, err = run_tool(gate, name, args), False
    except Exception as e:  # tool errors go back to the agent, not up the stack
        out, err = {"error": str(e)[:300]}, True
    if name in ("buy", "sell"):
        actions.append({"tool": name, "input": args, "result": out})
    return out, err


def _api_loop(gate: RiskGate, first: dict, actions: list) -> str:
    """Claude API with native tool use."""
    messages = [{"role": "user", "content": json.dumps(first, default=str)}]
    resp = None
    for _ in range(MAX_TURNS):
        resp = CC.create(f"agent-{gate.agent}", C.AGENT_MODEL, max_tokens=8000,
                         system=[{"type": "text", "text": system_prompt(gate.agent)}],
                         tools=TOOLS, cache_control={"type": "ephemeral"},
                         output_config={"effort": C.AGENT_EFFORT}, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                out, err = _call_tool(gate, b.name, b.input, actions)
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(out, default=str)[:20000], "is_error": err})
        messages.append({"role": "user", "content": results})
    summary = CC.text_of(resp).strip() if resp else ""
    if resp is not None and resp.stop_reason == "tool_use":
        summary = (summary + " (stopped: too many steps)").strip()
    return summary


STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "calls": {"type": "array", "items": {
            "type": "object",
            "properties": {"tool": {"type": "string", "enum": [t["name"] for t in TOOLS]},
                           "input": {"type": "object"}},
            "required": ["tool", "input"]}},
        "summary": {"type": "string"},
    },
    "required": ["calls", "summary"],
}


def _pro_system(agent: str) -> str:
    tools = "\n".join(f"- {t['name']}: {t['description']} Input: {json.dumps(t['input_schema']['properties'])}"
                       for t in TOOLS)
    return (system_prompt(agent) + "\n\nTools (you call them by replying with JSON):\n" + tools +
            "\n\nEach reply is one JSON object: {\"calls\": [{\"tool\": ..., \"input\": {...}}, ...], \"summary\": \"\"}."
            " The calls run in order and you get their results in the next message under `history`."
            " When you are finished, reply with \"calls\": [] and your final summary.")


def _pro_loop(gate: RiskGate, first: dict, actions: list) -> str:
    """Claude Code on the owner's subscription: Claude replies with JSON tool calls, we run them."""
    history = []
    for _ in range(MAX_TURNS):
        prompt = json.dumps({"context": first, "history": history}, default=str)
        step = CC.cli_json(f"agent-{gate.agent}", _pro_system(gate.agent), prompt, STEP_SCHEMA)
        calls = step.get("calls") or []
        if not calls:
            return str(step.get("summary", "")).strip()
        results = []
        for c in calls[:8]:
            out, _ = _call_tool(gate, c.get("tool", ""), c.get("input") or {}, actions)
            results.append({"tool": c.get("tool"), "input": c.get("input"), "result": out})
        history.append({"your_calls_and_results": results})
        if len(json.dumps(history, default=str)) > 150_000:  # keep the prompt a sane size
            history = history[-4:]
    return "(stopped: too many steps)"


def run(agent: str, trigger: str, client=None) -> str:
    why_not = can_run(agent)
    if why_not:
        B.log.info("[%s agent] not running: %s", agent, why_not)
        return why_not
    client = client or B.trading_client()
    B.safety_gate(client, f"{agent} agent", [p.symbol for p in agent_positions(client, agent)])
    gate = RiskGate(client, agent)
    recent = [{"t": j["ts"][:16], "trigger": j["trigger"], "summary": j["summary"]} for j in state.journal(agent, 6)]
    first = {"now_utc": state.now_utc().isoformat(timespec="minutes"), "woken_because": trigger,
             "portfolio": portfolio(gate), "your_recent_journal": recent,
             # Pre-gathered so the agent needs fewer calls (and less of your Claude usage):
             "material_news_24h": news(agent, None, 24)[:12]}
    actions = []
    try:
        summary = (_pro_loop if CC.use_pro() else _api_loop)(gate, first, actions)
    except CC.BudgetExceeded as e:
        summary = f"Stopped early: {e}"
    state.add_journal(agent, trigger, summary or "(no summary)", actions)
    traded = [a for a in actions if a["result"].get("ok")]
    if traded:
        B._trade_lines.append(f"Why: {summary[:600]}")
        B.flush_trades(f"{agent.title()} agent traded")
    else:
        B._trade_lines.clear()
    B.log.info("[%s agent] %s", agent, summary)
    return summary


# ---------- automatic protection (no Claude involved) ----------

def enforce_stops(client) -> list:
    """Sell agent positions that fell past their stop. Runs every minute from daemon.py."""
    sold = []
    clock_open = None
    acct = client.get_account()
    eq = float(acct.equity)
    for agent, cfg in AGENTS.items():
        for p in agent_positions(client, agent):
            plpc = float(p.unrealized_plpc or 0)
            if plpc > -cfg["stop"]():
                continue
            if not cfg["crypto"]:
                if clock_open is None:
                    clock_open = client.get_clock().is_open
                if not clock_open:
                    continue
            sym = to_order_symbol(agent, p.symbol)
            block = pdt_block(acct, eq, p.symbol, cfg["crypto"])
            if block:
                B.alert(f"[{agent} agent] {sym} is {plpc:.0%} but can't be sold today: {block}")
                continue
            B.flatten(client, sym, f"{agent} agent", f"stop-loss: {plpc:.1%} from entry", warn=False)
            state.log_agent_order(agent, p.symbol, "sell", float(p.market_value or 0), "stop-loss")
            state.drop_entry(p.symbol)
            state.set_cooldown(sym, 10, "stopped out")
            sold.append(sym)
    if sold:
        B.flush_trades("Stop-loss sold")
    return sold


def flatten_all(client, reason: str):
    for agent in AGENTS:
        for p in agent_positions(client, agent):
            B.flatten(client, to_order_symbol(agent, p.symbol), f"{agent} agent", reason, warn=False)
            state.drop_entry(p.symbol)
    B.flush_trades("Agents sold everything")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in AGENTS:
        sys.exit("usage: python agent.py stock|crypto [reason]")
    print(run(sys.argv[1], " ".join(sys.argv[2:]) or "manual run"))
