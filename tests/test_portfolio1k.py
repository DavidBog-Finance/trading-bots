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


def test_full_port_only_when_every_rule_holds():
    sig = R.Signal("NVDA", "orb", "call", 100, extra={"also": ["news"]})
    assert R.choose_tier(sig, "A+", True, {}, "2026-W47")[0] == "full"
    assert R.choose_tier(sig, "A", True, {}, "2026-W47")[0] == "standard"
    assert R.choose_tier(R.Signal("NVDA", "orb", "call", 100), "A+", True, {}, "2026-W47")[0] == "standard"
    assert R.choose_tier(sig, "A+", True, {"last_result": "loss"}, "2026-W47")[0] == "standard"
    assert R.choose_tier(sig, "A+", True, {"full_week": "2026-W47", "full_count": 1}, "2026-W47")[0] == "standard"
    assert R.choose_tier(sig, "A+", True, {"full_week": "2026-W46", "full_count": 1}, "2026-W47")[0] == "full"
    assert R.choose_tier(sig, "A+", True, {"full_enabled": False}, "2026-W47")[0] == "standard"
    assert R.choose_tier(sig, "A+", True, {"standard_only_left": 2}, "2026-W47")[0] == "standard"


# ---------- risk limits ----------

def test_entry_blocks():
    now = at("10:30")
    base = {"day": DAY.isoformat(), "day_start_equity": 1000, "week": R.iso_week(DAY), "week_start_equity": 1000}
    ok = lambda st=base, eq=1000, **kw: R.entry_block(st, kw.get("now", now), eq, 1000, kw.get("pos", False), kw.get("dt", 0), kw.get("fomc", set()))
    assert ok() is None
    assert "KILL" in ok(eq=500)
    assert "today" in ok(eq=790)
    assert "week" in ok(st={**base, "day_start_equity": 760}, eq=690)
    assert "one position" in ok(pos=True)
    assert "hours" in ok(now=at("09:40")) and "hours" in ok(now=at("14:31"))
    assert "Fed" in ok(now=at("13:45"), fomc={DAY.isoformat()})
    assert "PDT" in ok(dt=3)
    assert "trades today" in ok(st={**base, "trades_day": 2})
    assert "paused" in ok(st={**base, "paused": True})
    assert "killed" in ok(st={**base, "killed": True})


def test_losing_streak_pauses_two_days_then_standard_only():
    st = {}
    for _ in range(3):
        st = R.after_close(st, "loss", DAY)
    assert st["pause_until"] == "2026-11-23" and st["standard_only_left"] == 3
    assert "paused until" in R.entry_block({**st, "day": "x"}, at("10:30", date(2026, 11, 20)), 1000, 1000, False, 0, set())
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
        self.positions, self.orders = {}, {}
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

    def sell(client, sym, qty, bid):
        log["sells"].append((sym, qty))
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
    assert "one position" in R.entry_block(store.state(), clock["now"], 1000, 1000, True, 0, set())
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
