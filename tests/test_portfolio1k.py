"""Portfolio 1k: every rule in the plan, plus a full trade lifecycle against a fake Alpaca (no network)."""
import types
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import broker as B
import config as C
import state as global_state
from portfolio1k import brain, data, rules as R, runner, settings as S, store, trader

ET = ZoneInfo("America/New_York")
DAY = date(2026, 11, 18)  # a Wednesday


def at(hhmm: str, d: date = DAY) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ET)


def bars(closes, start="09:30", vols=None, spread=0.2):
    idx = pd.date_range(at(start), periods=len(closes), freq="5min")
    c = pd.Series(closes, index=idx, dtype=float)
    o = c.shift(1).fillna(c.iloc[0])
    return pd.DataFrame({"open": o, "high": pd.concat([o, c], axis=1).max(axis=1) + spread,
                         "low": pd.concat([o, c], axis=1).min(axis=1) - spread, "close": c,
                         "volume": vols or [1000] * len(closes)}, index=idx)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "DB_PATH", str(tmp_path / "p1k.db"))
    monkeypatch.setattr(global_state, "DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(B, "LOG_FILE", str(tmp_path / "t.csv"))
    sent = []
    monkeypatch.setattr(B, "notify", lambda msg, title="", prio="default": sent.append((title, msg)))
    return sent


# ---------- setups ----------

def test_orb_breakout_fires_once_with_volume_and_vwap():
    df = bars([100, 100.3, 100.1, 100.2, 101.5], vols=[1000, 900, 800, 700, 3000])
    sig = R.orb_signal("NVDA", df, at("09:56"))
    assert sig and sig.direction == "call" and sig.setup == "orb"
    assert sig.level == pytest.approx(R.opening_range(df)[0])
    assert R.orb_signal("NVDA", bars([100, 100.3, 100.1, 100.2, 101.5, 101.9], vols=[1000] * 5 + [3000]), at("10:01")) is None  # no chasing
    weak = bars([100, 100.3, 100.1, 100.2, 101.5], vols=[1000, 900, 800, 700, 500])
    assert R.orb_signal("NVDA", weak, at("09:56")) is None                     # volume too low
    assert R.orb_signal("NVDA", df, at("11:05")) is None                       # window over


def test_orb_put_side():
    df = bars([100, 99.8, 100.1, 99.9, 98.6], vols=[1000, 900, 800, 700, 3000])
    sig = R.orb_signal("QQQ", df, at("09:56"))
    assert sig and sig.direction == "put"


def test_pullback_in_uptrend():
    daily = pd.Series([90 + i * 0.5 for i in range(25)])
    df = bars([100, 100.5, 101, 101.5, 102, 101.2, 100.9, 101.6], spread=0.1)
    df.iloc[-2, df.columns.get_loc("low")] = 100.6
    df.iloc[-1, df.columns.get_loc("low")] = 101.0
    sig = R.pullback_signal("AAPL", df, daily, at("10:15"))
    assert sig and sig.direction == "call"
    assert R.pullback_signal("AAPL", df, daily, at("09:55")) is None
    down = pd.Series([110 - i * 0.5 for i in range(25)])
    assert R.pullback_signal("AAPL", df, down, at("10:15")) is None


def test_news_signal_needs_strong_fresh_news_and_price_confirmation():
    now = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)
    rows = [{"ts": (now - timedelta(minutes=20)).isoformat(), "score": 0.8, "materiality": 0.9, "headline": "Big win"},
            {"ts": (now - timedelta(minutes=200)).isoformat(), "score": 0.9, "materiality": 0.9, "headline": "old"},
            {"ts": (now - timedelta(minutes=5)).isoformat(), "score": 0.3, "materiality": 0.9, "headline": "weak"}]
    n = R.strong_news(rows, now)
    assert n["headline"] == "Big win"
    df = bars([100, 100.2, 100.1, 100.9])
    assert R.news_signal("TSLA", df, n, at("10:00")).direction == "call"
    assert R.news_signal("TSLA", bars([100, 100.2, 100.1, 100.15]), n, at("10:00")) is None


def test_spy_filter_and_merge():
    falling_below = bars([100, 99.5, 99, 98.5])
    assert not R.spy_agrees(falling_below, "call") and R.spy_agrees(falling_below, "put")
    a = R.Signal("NVDA", "orb", "call", 100)
    b = R.Signal("NVDA", "news", "call", 99)
    merged = R.merge_signals([a, b])
    assert len(merged) == 1 and merged[0].extra["also"] == ["news"]


# ---------- contract picker ----------

def contract(**kw):
    c = {"symbol": "NVDA261120C00190000", "expiry": date(2026, 11, 20), "strike": 190, "delta": 0.48,
         "bid": 2.00, "ask": 2.08, "oi": 5000, "volume": 2000}
    c.update(kw)
    return c


def test_contract_filters():
    t = DAY
    assert R.contract_problems(contract(), "NVDA", t, None) == []
    assert "days" in R.contract_problems(contract(expiry=date(2026, 11, 19)), "NVDA", t, None)[0]       # 1 DTE
    assert "delta" in R.contract_problems(contract(delta=0.3), "NVDA", t, None)[0]
    assert "spread" in R.contract_problems(contract(bid=1.80, ask=2.10), "NVDA", t, None)[0]
    assert "spread" in R.contract_problems(contract(bid=2.00, ask=2.05), "SPY", t, None)[0]            # ETF $0.03 rule
    assert "open" in R.contract_problems(contract(oi=300), "NVDA", t, None)[0]
    assert "volume" in R.contract_problems(contract(volume=100), "NVDA", t, None)[0]
    assert R.contract_problems(contract(volume=None), "NVDA", t, None) == []                          # unknown volume
    assert "earnings" in R.contract_problems(contract(), "NVDA", t, date(2026, 11, 19))[0]


def test_pick_prefers_friday_and_fits_budget():
    mon = contract(symbol="A", expiry=date(2026, 11, 23), delta=0.47)      # Monday, 5 DTE
    fri = contract(symbol="B", expiry=date(2026, 11, 20), delta=0.52)      # Friday, 2 DTE: not 3+ days
    nxt_fri = contract(symbol="C", expiry=date(2026, 11, 25), delta=0.50)  # Wednesday
    pick, _ = R.pick_contract([mon, fri, nxt_fri], "NVDA", DAY, None, 500)
    assert pick["symbol"] == "B"                                            # no Friday with 3+ days: nearest expiry
    tue = date(2026, 11, 17)
    fri3 = contract(symbol="F", expiry=date(2026, 11, 20), delta=0.42)      # Friday, 3 days left
    thu = contract(symbol="T", expiry=date(2026, 11, 19), delta=0.48)       # Thursday, 2 days
    pick, _ = R.pick_contract([thu, fri3], "NVDA", tue, None, 500)
    assert pick["symbol"] == "F"                                            # Friday with 3+ days left wins
    pick, why = R.pick_contract([contract(ask=6.0, bid=5.9)], "NVDA", DAY, None, 500)
    assert pick is None and "1" in why                                     # 1 contract = $600 > $500


def test_ticks_and_sizing():
    assert R.round_tick("SPY", 4.567) == 4.56 and R.round_tick("NVDA", 4.567) == 4.55
    assert R.round_tick("NVDA", 2.341, up=True) == 2.35
    assert R.contracts_for(500, 2.08) == 2 and R.contracts_for(100, 2.08) == 0
    assert R.budget_for("standard", 1000, 1000) == 500 and R.budget_for("full", 1000, 800) == 800


def test_full_port_retired_by_default():
    sig = R.Signal("NVDA", "orb", "call", 100, extra={"also": ["news"]})
    assert R.choose_tier(sig, "A+", True, {}, "2026-W47") == ("standard", "full port switched off")


def test_full_port_only_when_every_rule_holds():
    sig = R.Signal("NVDA", "orb", "call", 100, extra={"also": ["news"]})
    on = {"full_enabled": True}
    assert R.choose_tier(sig, "A+", True, on, "2026-W47")[0] == "full"
    assert R.choose_tier(sig, "A", True, on, "2026-W47")[0] == "standard"
    assert R.choose_tier(R.Signal("NVDA", "orb", "call", 100), "A+", True, on, "2026-W47")[0] == "standard"
    assert R.choose_tier(sig, "A+", True, {**on, "last_result": "loss"}, "2026-W47")[0] == "standard"
    assert R.choose_tier(sig, "A+", True, {**on, "full_week": "2026-W47", "full_count": 1}, "2026-W47")[0] == "standard"
    assert R.choose_tier(sig, "A+", True, {**on, "full_week": "2026-W46", "full_count": 1}, "2026-W47")[0] == "full"
    assert R.choose_tier(sig, "A+", True, {"full_enabled": False}, "2026-W47")[0] == "standard"
    assert R.choose_tier(sig, "A+", True, {**on, "standard_only_left": 2}, "2026-W47")[0] == "standard"


# ---------- risk limits ----------

def test_entry_blocks():
    now = at("10:30")
    base = {"day": DAY.isoformat(), "day_start_equity": 1000, "week": R.iso_week(DAY), "week_start_equity": 1000}
    ok = lambda st=base, eq=1000, **kw: R.entry_block(st, kw.get("now", now), eq, 1000, kw.get("pos", False), kw.get("fomc", set()))
    assert ok() is None
    assert "KILL" in ok(eq=500)
    assert "today" in ok(eq=790)
    assert "week" in ok(st={**base, "day_start_equity": 760}, eq=690)
    assert "one position" in ok(pos=True)
    assert "hours" in ok(now=at("09:40")) and "hours" in ok(now=at("14:31"))
    assert "Fed" in ok(now=at("13:45"), fomc={DAY.isoformat()})
    assert "trades today" in ok(st={**base, "trades_day": 2})
    assert "paused" in ok(st={**base, "paused": True})
    assert "killed" in ok(st={**base, "killed": True})


def test_losing_streak_pauses_two_days_then_standard_only():
    st = {}
    for _ in range(3):
        st = R.after_close(st, "loss", DAY)
    assert st["pause_until"] == "2026-11-23" and st["standard_only_left"] == 3
    assert "paused until" in R.entry_block({**st, "day": "x"}, at("10:30", date(2026, 11, 20)), 1000, 1000, False, set())
    st = R.after_close(st, "win", date(2026, 11, 23))
    assert st["standard_only_left"] == 2 and st["loss_streak"] == 0


# ---------- exits ----------

def pos(**kw):
    p = {"entry": 2.0, "qty": 2, "tier": "standard", "direction": "call", "level": 100.0, "expiry": date(2026, 11, 20),
         "tp1_done": False, "high": 2.0, "stop": 1.4}
    p.update(kw)
    return p


def test_exits():
    nxt = date(2026, 11, 19)
    assert R.exit_decision(pos(), 1.39, at("10:30"), 101, nxt).kind == "stop"
    assert R.exit_decision(pos(), 2.1, at("10:30"), 99.5, nxt).kind == "setup_stop"
    tp1 = R.exit_decision(pos(), 2.85, at("10:30"), 101, nxt)
    assert tp1.kind == "tp1" and tp1.qty == 1
    assert R.exit_decision(pos(qty=1), 2.85, at("10:30"), 101, nxt).qty == 1
    assert R.exit_decision(pos(tp1_done=True, qty=1, high=3.0, stop=2.0), 3.65, at("11:00"), 101, nxt).kind == "tp2"
    assert R.exit_decision(pos(tp1_done=True, qty=1, high=3.4, stop=2.0), 2.7, at("11:00"), 101, nxt).kind == "trail"
    assert R.exit_decision(pos(), 2.1, at("12:00"), 101, nxt) is None
    assert R.exit_decision(pos(), 2.1, at("15:46"), 101, nxt).kind == "time"
    keep = pos(expiry=date(2026, 11, 25))
    assert R.exit_decision(keep, 2.5, at("15:46"), 101, nxt) is None                 # +25%, 7 DTE: may hold overnight
    assert R.exit_decision({**keep, "tier": "full"}, 2.5, at("15:46"), 101, nxt).kind == "time"
    assert R.exit_decision(pos(expiry=nxt), 2.1, at("15:31"), 101, nxt).kind == "expiry"
    assert R.exit_decision(pos(direction="put", level=100), 2.1, at("10:30"), 100.5, nxt).kind == "setup_stop"


def test_scorecard_and_go_live():
    trades = [{"pnl": 100, "equity_before": 1000, "slip": None}, {"pnl": -50, "equity_before": 1100, "slip": 8},
              {"pnl": 80, "equity_before": 1050, "slip": None}]
    card = R.scorecard(trades)
    assert card["trades"] == 3 and card["profit_factor"] == pytest.approx(3.6) and card["avg_stop_slip"] == 8
    assert "3/40 trades" in R.go_live_ready(card, 0)


# ---------- the whole loop against a fake Alpaca ----------

class Fake:
    def __init__(self):
        self.equity, self.cash = 1000.0, 1000.0
        self.positions, self.orders, self.avg = {}, {}, {}
        self.is_open = True

    def get_account(self):
        return types.SimpleNamespace(equity=str(self.equity), cash=str(self.cash), daytrade_count=0,
                                     trading_blocked=False, account_blocked=False)

    def get_clock(self):
        now = datetime.now(timezone.utc)
        return types.SimpleNamespace(is_open=self.is_open, next_open=now + timedelta(days=1), timestamp=now,
                                     next_close=now + timedelta(hours=3))

    def get_open_position(self, sym):
        if sym not in self.positions:
            raise Exception("no position")
        return types.SimpleNamespace(qty=str(self.positions[sym]))

    def get_order_by_id(self, oid):
        return self.orders[oid]

    def get_all_positions(self):
        return [types.SimpleNamespace(symbol=s, qty=str(q), avg_entry_price=str(self.avg.get(s, 2.0)))
                for s, q in self.positions.items() if q]

    def get_orders(self, req):
        return []


@pytest.fixture
def world(monkeypatch, isolated):
    fc = Fake()
    monkeypatch.setattr(C, "NET_DEPOSITS_OVERRIDE", 1000)
    monkeypatch.setattr(C, "CONTROL_TOPIC", "")
    monkeypatch.setattr(C, "DRY_RUN", False)
    clock = {"now": at("09:56")}
    monkeypatch.setattr(runner, "now_et", lambda: clock["now"])
    spy = bars([500, 500.5, 500.4, 500.6, 501.2], vols=[1000, 900, 800, 700, 3000])
    nvda = bars([100, 100.3, 100.1, 100.2, 101.5], vols=[1000, 900, 800, 700, 3000])
    feed = {"bars": {"SPY": spy, "NVDA": nvda}, "quote": (2.00, 2.04)}
    monkeypatch.setattr(S, "WATCHLIST", ["SPY", "NVDA"])
    monkeypatch.setattr(data, "intraday_bars", lambda syms, now: feed["bars"])
    monkeypatch.setattr(data, "daily_closes", lambda syms: {})
    monkeypatch.setattr(data, "last_trade_age_ok", lambda df, now: True)
    monkeypatch.setattr(data, "earnings_dates", lambda syms, today, days=10: {"NVDA": None})
    monkeypatch.setattr(data, "candidates", lambda client, u, d, spot, today: [contract(expiry=date(2026, 11, 20))] if u == "NVDA" else [])
    monkeypatch.setattr(data, "option_quote", lambda sym: (*feed["quote"], datetime.now(timezone.utc)))
    monkeypatch.setattr(brain, "available", lambda: None)
    monkeypatch.setattr(brain, "game_plan", lambda w, e: {"summary": "calm", "bias": [], "avoid": []})
    monkeypatch.setattr(brain, "review", lambda payload: {"take": True, "grade": "A", "reason": "clean breakout"})
    log = {"buys": [], "sells": [], "stops": [], "cancels": []}

    def buy(client, sym, root, qty, bid, ask):
        log["buys"].append((sym, qty))
        fc.positions[sym] = qty
        return qty, 2.04

    def place_stop(client, sym, root, qty, price):
        log["stops"].append((sym, qty, price))
        oid = f"stop{len(log['stops'])}"
        fc.orders[oid] = types.SimpleNamespace(status="accepted", filled_qty=0, filled_avg_price=None)
        return oid

    def sell(client, sym, qty, bid, ask=0.0, urgent=True):
        log["sells"].append((sym, qty))
        log.setdefault("urgent", []).append(urgent)
        fc.positions[sym] -= qty
        return bid

    monkeypatch.setattr(trader, "buy", buy)
    monkeypatch.setattr(trader, "place_stop", place_stop)
    monkeypatch.setattr(trader, "sell", sell)
    monkeypatch.setattr(trader, "cancel", lambda client, oid: log["cancels"].append(oid))
    monkeypatch.setattr(S, "USE_CLAUDE", True)
    monkeypatch.setattr(runner.N, "fetch_new_articles", lambda: [])
    return fc, clock, feed, log, isolated


def test_trade_lifecycle_buy_tp1_then_stop(world):
    fc, clock, feed, log, sent = world
    r = runner.Runner(fc)
    r.tick()
    assert log["buys"] == [("NVDA261120C00190000", 2)]                 # 50% of $1,000 / $208 = 2 contracts
    assert log["stops"][0] == ("NVDA261120C00190000", 2, round(2.04 * 0.7, 2))
    assert any(t == "1k BUY" for t, _ in sent)
    assert "one position" in R.entry_block(store.state(), clock["now"], 1000, 1000, True, set())
    # +45%: take profit 1 sells half and moves the stop to breakeven
    clock["now"] = at("10:20")
    feed["bars"]["NVDA"] = bars([100, 100.3, 100.1, 100.2, 101.5, 102, 102.5, 103, 103.2])
    feed["quote"] = (2.94, 2.98)
    r.tick()
    assert log["sells"] == [("NVDA261120C00190000", 1)]
    p = store.get("position")
    assert p["qty"] == 1 and p["tp1_done"] and p["stop"] == 2.04 and log["stops"][-1][2] == 2.04
    # the breakeven stop fills at Alpaca
    fc.orders[p["stop_id"]] = types.SimpleNamespace(status="filled", filled_qty=1, filled_avg_price=2.0)
    clock["now"] = at("10:40")
    r.tick()
    assert store.get("position") is None
    t = store.closed_trades()[0]
    assert t["pnl"] == pytest.approx((2.94 - 2.04) * 100 + (2.0 - 2.04) * 100) and t["kind"] == "stop"
    assert t["slip"] is None                                        # breakeven stop, not the planned -30%
    assert store.state()["last_result"] == "win"
    assert any(title == "1k SELL" for title, _ in sent)


def test_claude_skip_and_fallback(world, monkeypatch):
    fc, clock, feed, log, sent = world
    monkeypatch.setattr(brain, "review", lambda payload: {"take": False, "grade": "B", "reason": "choppy"})
    runner.Runner(fc).tick()
    assert log["buys"] == [] and any("choppy" in e["text"] for e in store.events())


def test_claude_unavailable_rules_still_trade_at_standard(world, monkeypatch):
    fc, clock, feed, log, sent = world
    monkeypatch.setattr(brain, "review", lambda payload: None)
    runner.Runner(fc).tick()
    assert log["buys"] and store.get("position")["tier"] == "standard"


def test_kill_floor_and_phone_commands(world):
    fc, clock, feed, log, sent = world
    r = runner.Runner(fc)
    r.tick()
    fc.equity = 480
    r.tick()
    assert store.state()["killed"] and log["sells"] and store.get("position") is None
    assert any(t == "1k KILLED" for t, _ in sent)
    r.handle("resume")
    assert not store.state()["killed"]
    r.handle("nofull")
    assert store.state()["full_enabled"] is False


def test_stop_replaced_each_morning(world):
    fc, clock, feed, log, sent = world
    r = runner.Runner(fc)
    r.tick()
    p = store.get("position")
    fc.orders[p["stop_id"]] = types.SimpleNamespace(status="expired", filled_qty=0, filled_avg_price=None)
    clock["now"] = at("09:35", date(2026, 11, 19))
    r.tick()
    assert len(log["stops"]) == 2 and store.get("position")["stop_id"] == "stop2"


def test_scorecard_subtracts_real_costs():
    trades = [{"pnl": 100, "qty": 2, "equity_before": 1000, "slip": None},
              {"pnl": -50, "qty": 1, "equity_before": 1100, "slip": None},
              {"pnl": 4, "qty": 1, "equity_before": 1050, "slip": None}]       # a tiny win becomes a loss
    card = R.scorecard(trades, 6.0)
    assert card["paper_net"] == 54 and card["net"] == 30
    assert card["win_rate"] == pytest.approx(1 / 3) and card["profit_factor"] == pytest.approx(88 / 58)
    assert "net $30.00 after est. real costs (paper $54.00)" in runner.scorecard_line(card)


# ---------- upgrades (2026-11-20): patient exits, cost log, stop check, rebuild from Alpaca, PIN, grades ----------

def test_sell_ladder_steps_from_mid_to_bid():
    assert R.sell_ladder("SPY", 2.00, 2.10) == [2.05, 2.03, 2.01, 2.0]
    assert R.sell_ladder("SPY", 2.00, 2.02) == [2.01, 2.0]
    assert R.sell_ladder("NVDA", 4.00, 4.40)[0] == 4.20 and R.sell_ladder("NVDA", 4.00, 4.40)[-1] == 4.0
    assert R.sell_ladder("SPY", 0, 2.0) == []
    assert R.exit_is_urgent("stop") and R.exit_is_urgent("kill") and not R.exit_is_urgent("tp1")


def test_fill_cost_and_cost_report():
    assert R.fill_cost("buy", 2.00, 2.10, 2.07)["slip"] == pytest.approx(0.02)
    assert R.fill_cost("sell", 2.00, 2.10, 2.03)["slip"] == pytest.approx(0.02)
    assert R.fill_cost("buy", 0, 2.10, 2.07)["slip"] is None
    trades = [{"qty": 2, "entry_slip": 0.02, "exit_slip": 0.03, "entry_slip_pct": 0.01, "exit_slip_pct": 0.015,
               "entry_spread_pct": 0.05}, {"qty": 1, "entry_slip": None}]
    c = R.cost_report(trades, 0.05, 36.5)
    assert c["trades"] == 1 and c["avg_round_trip"] == pytest.approx(0.05 * 100 * 2 + 0.05 * 2 * 2)
    assert c["trades_per_year"] == pytest.approx(10) and R.cost_report([{"qty": 1}], 0.05, 10) is None


def test_grade_card():
    import json
    t = lambda g, pnl, reason="x": {"pnl": pnl, "qty": 1, "claude": json.dumps({"grade": g, "reason": reason})}
    card = R.grade_card([t("A+", 50), t("A+", -20), t("A", 10), t("A", 10, "rules only (Claude unavailable)")], 6.0)
    assert card["A+"]["trades"] == 2 and card["A+"]["win_rate"] == 0.5 and card["A+"]["net"] == pytest.approx(18)
    assert card["rules only"]["trades"] == 1 and card["A"]["trades"] == 1


def test_parse_command_pin_and_age():
    assert R.parse_command("status", "4821", 5) == ("status", None)
    assert R.parse_command("kill 1k", "4821", 5) == ("kill", "missing or wrong PIN")
    assert R.parse_command("Kill 1k 4821", "4821", 5) == ("kill", None)
    assert R.parse_command("kill 4821", "4821", 900)[1].startswith("ignored")
    assert R.parse_command("kill", "", 5) == ("kill", None)                 # no PIN set: works as before
    assert R.parse_command("buy NVDA", "", 5)[1] == "unknown command"


def test_parse_occ_reconcile_and_counts():
    assert R.parse_occ("NVDA261120C00190000") == {"root": "NVDA", "expiry": date(2026, 11, 20), "direction": "call", "strike": 190.0}
    assert R.parse_occ("SPY") is None
    p = {"symbol": "NVDA261120C00190000", "qty": 2}
    assert R.reconcile(p, {"NVDA261120C00190000": 2}) == ("ok", "NVDA261120C00190000")
    assert R.reconcile(p, {}) == ("gone", "NVDA261120C00190000")
    assert R.reconcile(p, {"NVDA261120C00190000": 1})[0] == "resize"
    assert R.reconcile(None, {"QQQ261120P00500000": 1}) == ("adopt", "QQQ261120P00500000")
    orders = [{"client_order_id": "p1k-abc", "side": "buy", "status": "filled", "filled_at": "2026-11-20T15:00:00+00:00"},
              {"client_order_id": "p1k-stop-x", "side": "sell", "status": "filled", "filled_at": "2026-11-20T16:00:00+00:00"},
              {"client_order_id": "p1k-def", "side": "buy", "status": "filled", "filled_at": "2026-11-18T15:00:00+00:00"},
              {"client_order_id": "other", "side": "buy", "status": "filled", "filled_at": "2026-11-20T15:00:00+00:00"}]
    assert R.bot_buys_since(orders, "2026-11-20T05:00:00+00:00") == 1
    assert R.bot_buys_since(orders, "2026-11-16T05:00:00+00:00") == 2


def test_expiry_day_close_at_3pm():
    p = {"entry": 2.0, "qty": 1, "tier": "standard", "direction": "call", "level": None, "expiry": DAY,
         "tp1_done": False, "high": 2.0, "stop": 1.4}
    assert R.exit_decision(p, 2.1, at("14:55"), None, date(2026, 11, 19)) is None
    assert R.exit_decision(p, 2.1, at("15:00"), None, date(2026, 11, 19)).kind == "expiry"


def test_cost_log_written_and_patient_exit(world):
    fc, clock, feed, log, sent = world
    r = runner.Runner(fc)
    r.tick()
    with store.db() as con:
        row = dict(con.execute("select * from trades").fetchone())
    # contract quote 2.00 x 2.08, filled at 2.04 = exactly the mid; spread 0.08 / 2.04
    assert row["entry_bid"] == 2.00 and row["entry_slip"] == pytest.approx(0) and row["feed"] == S.OPTION_FEED
    assert row["entry_spread_pct"] == pytest.approx(0.08 / 2.04, abs=1e-4)
    clock["now"] = at("10:20")
    feed["bars"]["NVDA"] = bars([100, 100.3, 100.1, 100.2, 101.5, 102, 102.5, 103, 103.2])
    feed["quote"] = (2.94, 2.98)
    r.tick()
    assert log["urgent"] == [False]                                 # take profit uses the patient ladder
    p = store.get("position")
    assert p["exit_q"] == 1 and p["exit_slip_q"] == pytest.approx(0.02)
    r.handle("close")
    assert log["urgent"][-1] is True                                # your 'close' sells at market
    t = store.closed_trades()[0]
    assert t["exit_slip"] is not None and t["fees"] == pytest.approx(0.2)
    assert "Costs (" in runner.cost_line(store.closed_trades())
    assert "Grades: A:" in runner.grade_lines(store.closed_trades())


def test_missing_stop_alerts_once(world, monkeypatch):
    fc, clock, feed, log, sent = world
    r = runner.Runner(fc)
    r.tick()
    p = store.get("position")
    fc.orders[p["stop_id"]] = types.SimpleNamespace(status="canceled", filled_qty=0, filled_avg_price=None)
    alerts = []
    monkeypatch.setattr(B, "alert", lambda msg: alerts.append(msg))
    monkeypatch.setattr(trader, "place_stop", lambda *a: None)
    clock["now"] = at("10:00")
    r.tick()
    r.tick()
    assert sum("No stop order" in a for a in alerts) == 1


def test_startup_adopts_a_position_the_memory_lost(world):
    fc, clock, feed, log, sent = world
    fc.positions["NVDA261120C00190000"] = 2
    fc.avg["NVDA261120C00190000"] = 2.10
    r = runner.Runner(fc)
    r.startup()
    p = store.get("position")
    assert p["qty"] == 2 and p["entry"] == 2.10 and p["setup"] == "recovered" and p["stop"] == round(2.10 * 0.7, 2)
    assert any(t == "1k Recovered trade" for t, _ in sent)
    fc.positions["NVDA261120C00190000"] = 1
    runner.Runner(fc).startup()
    assert store.get("position")["qty"] == 1


def test_phone_command_needs_pin(world, monkeypatch):
    fc, clock, feed, log, sent = world
    import io, json, time as _t
    monkeypatch.setattr(C, "CONTROL_TOPIC", "t")
    monkeypatch.setattr(S, "CONTROL_PIN", "4821")
    msgs = [{"event": "message", "id": "a", "time": _t.time(), "message": "pause 1k"}]
    monkeypatch.setattr(runner.urllib.request, "urlopen", lambda url, timeout=10: io.BytesIO("\n".join(json.dumps(m) for m in msgs).encode()))
    r = runner.Runner(fc)
    r.commands()
    assert not store.state().get("paused") and any(t == "1k Command ignored" for t, _ in sent)
    msgs[0] = {"event": "message", "id": "b", "time": _t.time(), "message": "pause 1k 4821"}
    r.last_cmd = 0
    r.commands()
    assert store.state().get("paused")


def test_trader_sell_ladder_then_market(monkeypatch):
    """A patient sell steps the limit down; whatever is still unsold goes out at market."""
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(trader.time, "sleep", lambda s: None)
    clock = {"t": 0.0}
    monkeypatch.setattr(trader.time, "time", lambda: clock.__setitem__("t", clock["t"] + 3) or clock["t"])
    calls = []

    class Cl:
        def __init__(self):
            self.o = {}

        def submit_order(self, req):
            oid = f"o{len(self.o)}"
            kind = type(req).__name__
            calls.append((kind, getattr(req, "limit_price", None), req.qty))
            filled = 1 if kind == "LimitOrderRequest" else req.qty
            self.o[oid] = types.SimpleNamespace(id=oid, status="new" if kind == "LimitOrderRequest" else "filled",
                                                filled_qty=0 if kind == "LimitOrderRequest" else filled,
                                                filled_avg_price=None if kind == "LimitOrderRequest" else 1.95)
            return self.o[oid]

        def get_order_by_id(self, oid):
            return self.o[oid]

        def replace_order_by_id(self, oid, req):
            calls.append(("replace", req.limit_price, None))
            return self.o[oid]

        def cancel_order_by_id(self, oid):
            o = self.o[oid]
            o.status, o.filled_qty, o.filled_avg_price = "canceled", 1, 2.03   # 1 of 2 filled before the cancel

    px = trader.sell(Cl(), "SPY261120C00500000", 2, 2.00, 2.10, urgent=False)
    assert calls[0] == ("LimitOrderRequest", 2.05, 2)
    assert [c[1] for c in calls if c[0] == "replace"] == [2.03, 2.01, 2.0]
    assert calls[-1] == ("MarketOrderRequest", None, 1)
    assert px == pytest.approx((2.03 + 1.95) / 2)


def test_near_miss_report():
    ev = [{"kind": "setup", "text": "NVDA orb call"}, {"kind": "skip:spy", "text": "NVDA orb call: SPY disagrees"},
          {"kind": "setup", "text": "QQQ pullback put"}, {"kind": "skip:claude", "text": "QQQ pullback put: Claude B: choppy"},
          {"kind": "setup", "text": "SPY orb call"}, {"kind": "BUY", "text": "Bought 2 SPY..."}]
    txt = R.near_miss(ev)
    assert txt.startswith("3 setups fired today, 1 became a trade.")
    assert "1 skipped: SPY was moving the other way" in txt and "Claude B: choppy" in txt
    assert R.near_miss([{"kind": "skip:spy", "text": "x"}]) == ""


def test_near_miss_logged_in_the_loop(world, monkeypatch):
    fc, clock, feed, log, sent = world
    monkeypatch.setattr(brain, "review", lambda payload: {"take": False, "grade": "B", "reason": "choppy"})
    runner.Runner(fc).tick()
    kinds = [e["kind"] for e in store.events(50)]
    assert "setup" in kinds and "skip:claude" in kinds
    txt = R.near_miss(store.events_since("2000-01-01"))
    assert "Claude said no" in txt


def test_more_session_today_hands_off_only_while_the_session_has_time():
    from datetime import datetime
    d = datetime(2026, 11, 25, 9, 0)
    assert R.more_session_today(d, False, datetime(2026, 11, 25, 9, 30))          # opens later today
    assert not R.more_session_today(d, False, datetime(2026, 11, 26, 9, 30))      # holiday / weekend
    assert R.more_session_today(datetime(2026, 11, 25, 12, 0), True, None)        # open now
    assert not R.more_session_today(datetime(2026, 11, 25, 15, 56), True, None)   # too close to the close
    assert not R.more_session_today(datetime(2026, 11, 25, 17, 0), False, datetime(2026, 11, 27, 9, 30))
