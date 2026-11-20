"""Local AI + Claude working together, pacing, data store, scorecard, bedroom mode (no network)."""
import types
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

import claude_client as CC
import config as C
import daemon as D
import local_llm as L
import news as N
import scorecard as SC
import state


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(C, "LOCAL_LLM_ENABLED", True)
    monkeypatch.setattr(C, "CLAUDE_BACKEND", "pro")
    monkeypatch.setattr(C, "CLAUDE_CALLS_PER_HOUR", 10)
    monkeypatch.setattr(C, "AUDIT_SHARE", 0.10)
    monkeypatch.setattr(CC, "available", lambda: None)


def art(i, sym, headline="x"):
    return {"id": i, "time": state.now_utc().isoformat(), "symbols": [sym], "headline": headline, "summary": ""}


def setup_news(monkeypatch, arts, local_items, claude_items=None, local_fails=False):
    monkeypatch.setattr(N, "fetch_new_articles", lambda: arts)
    for a in arts:
        state.add_news(a["id"], a["time"], a["symbols"], a["headline"], "")

    def fake_local(system, prompt, schema):
        if local_fails:
            raise L.LocalUnavailable("down")
        return {"items": local_items}
    monkeypatch.setattr(L, "chat_json", fake_local)
    sent = []

    def fake_claude(purpose, system, prompt, schema):
        import json
        ids = [a["id"] for a in json.loads(prompt)]
        sent.append(ids)
        state.add_spend(purpose, "pro-plan", 0.0)
        return {"items": [it for it in (claude_items or []) if it["id"] in ids]}
    monkeypatch.setattr(CC, "cli_json", fake_claude)
    return sent


def li(i, sym, mat, tag="other", score=0.1):
    return {"id": i, "symbol": sym, "score": score, "materiality": mat, "tag": tag, "reason": "r"}


def test_local_screens_and_only_important_news_reaches_claude(monkeypatch):
    arts = [art(101, "NVDA"), art(102, "AAPL"), art(103, "XYZ"), art(150, "SOLUSD"), art(200, "MSFT")]
    local = [li(101, "NVDA", 0.2), li(102, "AAPL", 0.8), li(103, "XYZ", 0.5, "bankruptcy"), li(150, "SOLUSD", 0.1),
             li(200, "MSFT", 0.1)]   # id 200 % 100 = 0 -> in the 10% audit sample
    claude = [li(102, "AAPL", 0.9, score=0.7), li(101, "NVDA", 0.6, score=-0.5)]
    sent = setup_news(monkeypatch, arts, local, claude)
    rows = {r["symbol"]: r for r in N.poll(held={"NVDA"})}
    flat = [i for batch in sent for i in batch]
    assert flat[0] == 101                      # held news goes first
    assert set(flat) == {101, 102, 103, 200}   # SOLUSD noise stays local
    assert rows["AAPL"]["source"] == "claude" and rows["AAPL"]["score"] == 0.7
    assert rows["SOLUSD"]["source"] == "local"
    assert state.news_tilt("SOLUSD") == pytest.approx(0.1)


def test_pacing_keeps_claude_for_high_priority(monkeypatch):
    for _ in range(8):                         # 8 of 10 hourly calls already used
        state.add_spend("agent-stock", "pro-plan", 0.0)
    assert CC.allow("agent") and CC.allow("held")
    assert not CC.allow("material") and not CC.allow("audit")
    arts = [art(1, "NVDA"), art(2, "AAPL")]
    sent = setup_news(monkeypatch, arts, [li(1, "NVDA", 0.2), li(2, "AAPL", 0.9)], [])
    N.poll(held={"NVDA"})
    assert sent == [[1]]                       # held news still got Claude; the material one waited locally


def test_without_local_ai_claude_scores_everything(monkeypatch):
    monkeypatch.setattr(C, "LOCAL_LLM_ENABLED", False)
    arts = [art(1, "NVDA"), art(2, "AAPL")]
    sent = setup_news(monkeypatch, arts, [], [li(1, "NVDA", 0.5), li(2, "AAPL", 0.5)])
    assert len(N.poll()) == 2 and sent == [[1, 2]]


def test_local_outage_falls_back_to_claude(monkeypatch):
    arts = [art(1, "NVDA"), art(2, "AAPL")]
    sent = setup_news(monkeypatch, arts, [], [li(1, "NVDA", 0.5)], local_fails=True)
    N.poll()
    assert sent == [[1, 2]]


def test_calibration_moves_threshold_both_ways(monkeypatch):
    def seed(n, l_mat, c_mat, start):
        for i in range(start, start + n):
            ts = state.now_utc().isoformat()
            state.add_local_score(i, "S", ts, 0, l_mat, "other")
            state.add_score(i, "S", ts, 0, c_mat, "other", "", source="claude")
    seed(25, 0.2, 0.8, 0)                      # local calls important news unimportant
    assert N.calibrate() == pytest.approx(0.45)
    with state.db() as con:
        con.execute("delete from local_scores"); con.execute("delete from scores")
    seed(25, 0.2, 0.1, 100); seed(10, 0.6, 0.1, 200)   # Claude keeps confirming noise
    assert N.calibrate() == pytest.approx(0.5)


def test_local_buy_ideas_dont_wake_agents_but_held_news_does():
    rows = [{"symbol": "AMD", "score": 0.9, "materiality": 0.9, "tag": "product", "headline": "h", "source": "local"},
            {"symbol": "NVDA", "score": -0.8, "materiality": 0.9, "tag": "guidance", "headline": "h", "source": "local"}]
    w = D.news_wakes(rows, {"stock": {"NVDA"}, "crypto": set()}, {"AMD", "NVDA"}, set())
    assert len(w["stock"]) == 1 and "NVDA" in w["stock"][0]


def test_bedroom_mode_windows():
    assert D.in_hours("12-20", 12) and not D.in_hours("12-20", 20)
    assert D.in_hours("22-6", 23) and D.in_hours("22-6", 3) and not D.in_hours("22-6", 12)
    assert D.heavy_window(datetime(2026, 10, 23, 14)) and not D.heavy_window(datetime(2026, 10, 24, 14))  # Sat


def test_scorecard_measures_news_against_spy():
    idx = pd.bdate_range("2026-09-01", periods=30)
    spy = pd.Series(np.linspace(100, 103, 30), index=idx)
    good = pd.Series(np.linspace(50, 60, 30), index=idx)       # beats SPY
    closes = pd.DataFrame({"SPY": spy, "AAA": good})
    assert SC.forward_excess(closes, "AAA", "SPY", idx[5], 3) > 0
    rows = [{"symbol": "AAA", "ts": str(idx[i].date()), "score": 0.8, "materiality": 0.7, "source": "claude"} for i in range(12)]
    rows += [{"symbol": "AAA", "ts": str(idx[i].date()), "score": -0.8, "materiality": 0.7, "source": "claude"} for i in range(12, 20)]
    s = SC.summarize_news(SC.news_table(rows, closes))
    assert s["claude"]["headlines"] == 20 and s["claude"]["hit_rate_material_3d"] == pytest.approx(0.6)
    assert "Not enough" in SC.text({"news": {}, "agents": {}})


def test_datastore_only_downloads_missing(tmp_path, monkeypatch):
    import datastore
    monkeypatch.setattr(datastore, "DB_PATH", str(tmp_path / "m.duckdb"))
    calls = []

    def fetch(syms, days):
        calls.append(tuple(syms))
        idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=30, freq="D")
        return pd.concat([pd.DataFrame({"symbol": s, "date": idx, "close": 1.0, "volume": 1.0}) for s in syms])
    datastore.closes(["A", "B"], 30, fetch)
    calls.clear()
    out = datastore.closes(["A", "B", "C"], 30, fetch)
    assert calls[0] == ("C",) and list(out.columns) == ["A", "B", "C"]
