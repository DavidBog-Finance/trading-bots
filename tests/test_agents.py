"""Agents, risk gate, news scoring and the Mac runner's helpers, with fake Alpaca + fake Claude (no network)."""
import json
import types
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

import agent as A
import broker as B
import claude_client as CC
import config as C
import daemon as D
import market as M
import news as N
import state
import strategies as S
from tests.test_broker import FakeClient


def pos(symbol, value, plpc=0.0, asset="us_equity"):
    return types.SimpleNamespace(symbol=symbol, asset_class=asset, market_value=str(value), qty=str(value / 100),
                                 unrealized_pl=str(value * plpc), unrealized_plpc=str(plpc), avg_entry_price="100",
                                 current_price="100", change_today="0")


class AgentFake(FakeClient):
    def __init__(self, positions=(), equity=10000, cash=10000, daytrades=0, is_open=True):
        self.objs = {p.symbol: p for p in positions}
        super().__init__({s: (float(p.market_value), float(p.qty)) for s, p in self.objs.items()})
        self.acct = types.SimpleNamespace(equity=str(equity), cash=str(cash), last_equity=str(equity),
                                          daytrade_count=daytrades, trading_blocked=False, account_blocked=False)
        self.is_open = is_open

    def get_all_positions(self):
        return list(self.objs.values())

    def get_account(self):
        return self.acct

    def get_clock(self):
        return types.SimpleNamespace(is_open=self.is_open)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "DB_PATH", str(tmp_path / "state.db"))
    import datastore
    monkeypatch.setattr(datastore, "DB_PATH", str(tmp_path / "market.duckdb"))
    monkeypatch.setattr(C, "LOCAL_LLM_ENABLED", False)
    monkeypatch.setattr(B, "LOG_FILE", str(tmp_path / "t.csv"))
    for k, v in {"DRY_RUN": False, "NTFY_TOPIC": "", "ALERT_WEBHOOK_URL": "", "KILL_SWITCH": False,
                 "NET_DEPOSITS_OVERRIDE": 0, "ANTHROPIC_API_KEY": "test", "AGENTS_ENABLED": True,
                 "AGENT_STOCK_PCT": 0.20, "AGENT_CRYPTO_PCT": 0.15, "STOCK_MAX_USD": 0, "CRYPTO_MAX_USD": 0,
                 "CLAUDE_DAILY_BUDGET_USD": 2.0, "CLAUDE_BACKEND": "api"}.items():
        monkeypatch.setattr(C, k, v)
    monkeypatch.setattr(M, "stock_universe", lambda c: ["NVDA", "AAPL", "MSFT", "AMD", "META", "TSLA", "AMZN", "GOOGL"])
    monkeypatch.setattr(M, "crypto_universe", lambda c: ["ETH/USD", "SOL/USD"])
    monkeypatch.setattr(M, "live_prices", lambda syms: {s: (100.0, 0.0) for s in syms})
    B._trade_lines.clear()


# ---------- risk gate ----------

def test_gate_allows_normal_buy_and_blocks_bad_ones():
    g = A.RiskGate(AgentFake(), "stock")          # sleeve = 20% of $10k = $2,000
    assert g.check_buy("NVDA", 500) is None
    assert "per-position cap" in g.check_buy("NVDA", 700)     # 30% of $2,000 = $600
    assert "rules bots" in g.check_buy("SPY", 100)
    assert "universe" in g.check_buy("TQQQ", 100)
    assert "under" in g.check_buy("NVDA", 1)


def test_gate_respects_sleeve_cash_and_position_count(monkeypatch):
    monkeypatch.setattr(C, "AGENT_MAX_POSITIONS", 2)
    fc = AgentFake([pos("AAPL", 500), pos("MSFT", 500)])
    g = A.RiskGate(fc, "stock")
    assert "max of 2" in g.check_buy("NVDA", 100)
    assert g.check_buy("AAPL", 50) is None                   # adding to a holding is fine
    g = A.RiskGate(AgentFake(cash=50), "stock")
    assert "available" in g.check_buy("NVDA", 100)


def test_gate_blocks_stocks_when_closed_and_after_pause_and_cooldown():
    assert "closed" in A.RiskGate(AgentFake(is_open=False), "stock").check_buy("NVDA", 100)
    assert A.RiskGate(AgentFake(is_open=False), "crypto").check_buy("ETH/USD", 100) is None
    state.put("paused_stock", True)
    assert "paused" in A.RiskGate(AgentFake(), "stock").check_buy("NVDA", 100)
    state.put("paused_stock", False)
    state.set_cooldown("NVDA", 10, "stopped out")
    assert "cooldown" in A.RiskGate(AgentFake(), "stock").check_buy("NVDA", 100)


def test_gate_sells_only_after_big_open_losses():
    fc = AgentFake([pos("AAPL", 300, plpc=-0.5), pos("MSFT", 300, plpc=-0.5)])  # -$300 > 25% of $2,000? no: 15%
    assert A.RiskGate(fc, "stock").buys_paused() is None
    fc = AgentFake([pos("AAPL", 600, plpc=-1.0)])                               # -$600 = 30% of sleeve
    assert "sells only" in A.RiskGate(fc, "stock").buys_paused()


def test_pdt_guard_blocks_same_day_sell_under_25k():
    fc = AgentFake([pos("NVDA", 300)], daytrades=3)
    state.log_agent_order("stock", "NVDA", "buy", 300, "test")
    assert "PDT" in A.RiskGate(fc, "stock").check_sell("NVDA")
    fc = AgentFake([pos("NVDA", 300)], daytrades=3, equity=30000)
    assert A.RiskGate(fc, "stock").check_sell("NVDA") is None


def test_agents_ignore_core_positions():
    fc = AgentFake([pos("SPY", 1000), pos("NVDA", 200), pos("BTCUSD", 300, asset="crypto"), pos("ETHUSD", 100, asset="crypto")])
    assert [p.symbol for p in A.agent_positions(fc, "stock")] == ["NVDA"]
    assert [p.symbol for p in A.agent_positions(fc, "crypto")] == ["ETHUSD"]
    assert A.to_order_symbol("crypto", "ethusd") == "ETH/USD"


# ---------- a whole agent run with a scripted Claude ----------

def block(**kw):
    return types.SimpleNamespace(**kw)


def test_agent_run_buys_through_gate_and_journals(monkeypatch):
    fc = AgentFake()
    script = [
        block(stop_reason="tool_use", content=[block(type="tool_use", id="1", name="get_portfolio", input={})]),
        block(stop_reason="tool_use", content=[
            block(type="tool_use", id="2", name="buy", input={"symbol": "NVDA", "usd": 400, "thesis": "top momentum + beat"}),
            block(type="tool_use", id="3", name="buy", input={"symbol": "TQQQ", "usd": 100, "thesis": "should be blocked"})]),
        block(stop_reason="end_turn", content=[block(type="text", text="Bought NVDA on strong earnings.")]),
    ]
    seen = []

    def fake_create(purpose, model, **kw):
        seen.append(kw["messages"][-1])
        return script.pop(0)

    monkeypatch.setattr(CC, "create", fake_create)
    sent = []
    monkeypatch.setattr(B, "notify", lambda msg, title="", priority="": sent.append((title, msg)))
    out = A.run("stock", "test", fc)
    assert out == "Bought NVDA on strong earnings."
    assert len(fc.orders) == 1 and fc.orders[0].symbol == "NVDA" and fc.orders[0].notional == 400
    results = {r["tool_use_id"]: json.loads(r["content"]) for r in seen[-1]["content"]}
    assert results["2"]["ok"] and not results["3"]["ok"] and "universe" in results["3"]["blocked"]
    assert state.journal("stock")[0]["summary"] == out
    assert state.entry("NVDA")["thesis"] == "top momentum + beat"
    assert len(sent) == 1 and "Stock agent traded" in sent[0][0] and "NVDA" in sent[0][1]


def test_agent_run_without_trades_sends_nothing(monkeypatch):
    monkeypatch.setattr(CC, "create", lambda p, m, **kw: block(stop_reason="end_turn",
                                                              content=[block(type="text", text="Nothing worth doing.")]))
    sent = []
    monkeypatch.setattr(B, "notify", lambda *a, **k: sent.append(a))
    A.run("crypto", "scheduled", AgentFake())
    assert not sent


def test_agent_does_not_run_over_budget_or_without_key(monkeypatch):
    monkeypatch.setattr(C, "ANTHROPIC_API_KEY", "")
    assert "ANTHROPIC_API_KEY" in A.run("stock", "x", AgentFake())
    monkeypatch.setattr(C, "ANTHROPIC_API_KEY", "k")
    state.add_spend("agent-stock", "claude-opus-5-5", 5.0)
    assert "budget" in A.run("stock", "x", AgentFake())


def test_stop_loss_sells_and_sets_cooldown():
    fc = AgentFake([pos("NVDA", 80, plpc=-0.15), pos("AAPL", 100, plpc=-0.05), pos("SOLUSD", 70, plpc=-0.25, asset="crypto")])
    sold = A.enforce_stops(fc)
    assert sorted(sold) == ["NVDA", "SOL/USD"] and sorted(fc.closed) == ["NVDA", "SOLUSD"]
    assert state.cooldown("NVDA") and state.cooldown("SOL/USD")


def test_stop_loss_waits_for_market_open_for_stocks():
    fc = AgentFake([pos("NVDA", 80, plpc=-0.15)], is_open=False)
    assert A.enforce_stops(fc) == []


# ---------- Claude client ----------

def test_claude_client_fallbacks_budget_and_refusal(monkeypatch):
    calls = []
    usage = types.SimpleNamespace(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0, cache_read_input_tokens=0)

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            return types.SimpleNamespace(stop_reason=kw.pop("_stop", "end_turn"), usage=usage, content=[])

    monkeypatch.setattr(CC, "client", lambda: types.SimpleNamespace(beta=types.SimpleNamespace(messages=FakeMessages())))
    CC.create("t", "claude-opus-5-5", max_tokens=10, messages=[])
    assert calls[-1]["fallbacks"] == "default" and calls[-1]["betas"] == ["server-side-fallback-2026-07-01"]
    CC.create("t", "claude-haiku-5-5", max_tokens=10, messages=[])
    assert "fallbacks" not in calls[-1]
    assert state.spend_today() == pytest.approx((4 * 1000 + 20 * 500 + 0.1 * 1000 + 0.5 * 500) / 1e6)
    with pytest.raises(RuntimeError):
        CC.create("t", "claude-opus-5-5", max_tokens=10, messages=[], _stop="refusal")
    state.add_spend("x", "claude-opus-5-5", 3.0)
    with pytest.raises(CC.BudgetExceeded):
        CC.create("t", "claude-opus-5-5", max_tokens=10, messages=[])


# ---------- news ----------

def test_news_scores_are_clamped_and_invented_symbols_dropped(monkeypatch):
    arts = [{"id": 1, "time": state.now_utc().isoformat(), "symbols": ["NVDA"], "headline": "NVDA beats", "summary": ""}]
    items = [{"id": 1, "symbol": "NVDA", "score": 3, "materiality": 0.9, "tag": "earnings", "reason": "beat"},
             {"id": 1, "symbol": "AMD", "score": 0.5, "materiality": 0.5, "tag": "other", "reason": "made up"}]
    monkeypatch.setattr(CC, "create", lambda *a, **k: types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=json.dumps({"items": items}))]))
    for a in arts:
        state.add_news(a["id"], a["time"], a["symbols"], a["headline"], "")
    out = N.score(arts)
    assert len(out) == 1 and out[0]["score"] == 1.0
    assert state.news_tilt("NVDA") == pytest.approx(1.0) and state.news_tilt("AMD") == 0.0


# ---------- scanner ----------

def test_rank_momentum_prefers_steady_winners_and_skips_downtrends():
    idx = pd.date_range("2024-01-01", periods=300)
    t = np.arange(300)
    steady = 100 * np.exp(0.002 * t) * (1 + 0.005 * np.sin(t))      # same trend, low volatility
    choppy = 100 * np.exp(0.002 * t) * (1 + 0.05 * np.sin(t))       # same trend, high volatility
    falling = 100 * np.exp(-0.002 * t) * (1 + 0.005 * np.sin(t))
    df = pd.DataFrame({"STEADY": steady, "CHOPPY": choppy, "FALL": falling}, index=idx)
    r = S.rank_momentum(df)
    assert "FALL" not in r.index and r.index[0] == "STEADY"
    r = S.rank_momentum(df, news_tilt={"CHOPPY": 1.0}, news_weight=5)
    assert r.index[0] == "CHOPPY"
    assert 0 <= S.crypto_trend_vote(pd.Series(steady)) <= 1


# ---------- Mac runner helpers ----------

def test_checkins_and_commands():
    t = datetime(2026, 10, 22, 12, 45)
    assert D.due_checkins(t, []) == ["10:00", "12:30"]
    assert D.due_checkins(t, ["10:00", "12:30"]) == []
    assert D.parse_command("Pause crypto") == ("pause", "crypto")
    assert D.parse_command("kill") == ("kill", "all")
    assert D.parse_command("buy everything") == (None, None)


def test_news_wakes_route_to_the_right_agent():
    rows = [{"symbol": "NVDA", "score": -0.8, "materiality": 0.9, "tag": "guidance", "headline": "NVDA cuts guidance"},
            {"symbol": "AMD", "score": 0.2, "materiality": 0.9, "tag": "product", "headline": "AMD launches chip"},
            {"symbol": "SOLUSD", "score": -0.9, "materiality": 0.95, "tag": "hack_or_exploit", "headline": "Solana exploit"},
            {"symbol": "AAPL", "score": 0.9, "materiality": 0.3, "tag": "analyst", "headline": "minor"}]
    w = D.news_wakes(rows, {"stock": {"NVDA"}, "crypto": set()}, {"NVDA", "AMD", "AAPL"}, {"SOL/USD"})
    assert len(w["stock"]) == 1 and w["stock"][0].startswith("HELD NVDA")
    assert "SOLUSD" in w["crypto"][0]


def test_runner_tick_schedules_bots_and_agents(monkeypatch):
    from datetime import timedelta, timezone
    now = datetime.now(timezone.utc)
    fc = AgentFake()
    fc.get_clock = lambda: types.SimpleNamespace(is_open=True, timestamp=now, next_close=now + timedelta(minutes=30))
    monkeypatch.setattr(B, "trading_client", lambda: fc)
    monkeypatch.setattr(N, "poll", lambda held=None: [])
    monkeypatch.setattr(C, "CONTROL_TOPIC", "")
    monkeypatch.setattr(D, "due_checkins", lambda now_et, done: [] if done else ["15:10"])
    monkeypatch.setattr(D, "heavy_window", lambda now_et: False)
    monkeypatch.setattr(C, "OPTIONS_RECORDER", False)
    ran, woke = [], []
    monkeypatch.setattr(D.Runner, "run_bot", lambda self, script, env=None: ran.append(script))
    monkeypatch.setattr(A, "run", lambda agent, trigger, client=None: woke.append((agent, trigger)))
    r = D.Runner()
    r.tick()
    assert ran[:2] == ["stock_bot.py", "options_bot.py"]
    assert ("stock", "scheduled check-in 15:10 New York time") in woke
    assert any(a == "crypto" for a, _ in woke)
    ran.clear(), woke.clear()
    r.tick()                                   # nothing runs twice
    assert "stock_bot.py" not in ran and not any(t.startswith("scheduled") for _, t in woke)


def test_phone_commands_pause_resume_kill(monkeypatch):
    fc = AgentFake([pos("NVDA", 200)])
    monkeypatch.setattr(B, "trading_client", lambda: fc)
    ran = []
    monkeypatch.setattr(D.Runner, "run_bot", lambda self, script, env=None: ran.append((script, env)))
    r = D.Runner()
    r.handle("pause", "crypto")
    assert state.get("paused_crypto")
    r.handle("resume", "all")
    assert not state.get("paused_crypto")
    r.handle("kill", "all")
    assert fc.closed == ["NVDA"] and state.get("paused_all")
    assert all(env["KILL_SWITCH"] == "true" for _, env in ran) and len(ran) == 3


# ---------- Pro plan backend (Claude Code) ----------

def test_pro_agent_loop_runs_json_tool_calls(monkeypatch):
    monkeypatch.setattr(C, "CLAUDE_BACKEND", "pro")
    monkeypatch.setattr(CC, "available", lambda: None)
    steps = [{"calls": [{"tool": "get_portfolio", "input": {}},
                        {"tool": "buy", "input": {"symbol": "AMD", "usd": 300, "thesis": "momentum leader"}}], "summary": ""},
             {"calls": [], "summary": "Bought AMD."}]
    prompts = []

    def fake_cli(purpose, system, prompt, schema):
        prompts.append(json.loads(prompt))
        assert "buy" in system and schema["required"] == ["calls", "summary"]
        return steps.pop(0)

    monkeypatch.setattr(CC, "cli_json", fake_cli)
    monkeypatch.setattr(B, "notify", lambda *a, **k: None)
    fc = AgentFake()
    assert A.run("stock", "test", fc) == "Bought AMD."
    assert fc.orders[0].symbol == "AMD"
    assert prompts[1]["history"][0]["your_calls_and_results"][1]["result"]["ok"]


def test_cli_json_disables_tools_and_handles_limits(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "CLAUDE_BACKEND", "pro")
    monkeypatch.setattr(C, "CLAUDE_BIN", str(tmp_path / "claude"))
    (tmp_path / "claude").write_text("")
    monkeypatch.setattr(CC, "SANDBOX", str(tmp_path / "sb"))
    calls = []
    replies = [{"type": "result", "subtype": "success", "is_error": False, "result": "{}", "structured_output": {"items": []}},
               {"type": "result", "subtype": "success", "is_error": True, "result": "Claude usage limit reached"}]

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return types.SimpleNamespace(stdout=json.dumps(replies.pop(0)), stderr="", returncode=0)

    monkeypatch.setattr(CC.subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-abc\ndef \n")   # pasted from a wrapped line
    assert CC.cli_json("news", "sys", "prompt", {"type": "object"}) == {"items": []}
    cmd, kw = calls[0]
    assert kw["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-abcdef"
    assert cmd[cmd.index("--tools") + 1] == "" and "--json-schema" in cmd and kw["input"] == "prompt"
    assert "ANTHROPIC_API_KEY" not in kw["env"] and kw["cwd"].endswith("sb")
    with pytest.raises(CC.BudgetExceeded):
        CC.cli_json("news", "sys", "prompt", {"type": "object"})
    assert "usage limit" in CC.available()          # paused for an hour after hitting the limit


def test_cloud_once_mode_remembers_between_runs(monkeypatch):
    """Cloud mode is a fresh process every 30 min: timers, check-ins and queued wakes must persist."""
    from datetime import timedelta, timezone
    now = datetime.now(timezone.utc)
    fc = AgentFake()
    fc.get_clock = lambda: types.SimpleNamespace(is_open=True, timestamp=now, next_close=now + timedelta(minutes=30))
    monkeypatch.setattr(B, "trading_client", lambda: fc)
    monkeypatch.setattr(N, "poll", lambda held=None: [])
    monkeypatch.setattr(C, "CONTROL_TOPIC", "")
    monkeypatch.setattr(D, "due_checkins", lambda now_et, done: [] if done else ["10:00"])
    ran, woke = [], []
    monkeypatch.setattr(D.Runner, "run_bot", lambda self, script, env=None: ran.append(script))
    monkeypatch.setattr(A, "run", lambda agent, trigger, client=None: woke.append((agent, trigger)))
    D.Runner(once=True).tick_once()
    assert ("stock", "scheduled check-in 10:00 New York time") in woke
    assert any(a == "crypto" for a, _ in woke)
    assert ran == []                           # rules bots stay on bots.yml in the cloud
    woke.clear()
    r2 = D.Runner(once=True)                   # next run = new process
    assert not r2.every("news", 600)           # timer survived
    r2.pending["crypto"].append("queued headline")
    state.put("last_event_crypto", __import__("time").time())  # too soon to wake again
    r2.tick_once()
    assert not any(t.startswith("scheduled") for _, t in woke)
    assert "queued headline" in D.Runner(once=True).pending["crypto"]   # queue survived
