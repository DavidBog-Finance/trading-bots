from datetime import date, datetime

import pandas as pd
import pytest

from flowlog import app, outcomes as O, parse as P, store as S

ET = app.ET
TODAY = date(2026, 11, 20)


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "DB_PATH", str(tmp_path / "flow.db"))
    monkeypatch.setattr(app, "FLOW_TOPIC", "")


def test_parse_basic_call_on_ask():
    a = P.parse("AAPL 12/20 230C ask $250K sweep", TODAY)
    assert (a.symbol, a.expiry, a.strike, a.kind, a.side, a.premium) == ("AAPL", date(2026, 12, 20), 230, "call", "ask", 250_000)
    assert a.tags == ["sweep"] and a.direction == "bullish"


def test_parse_words_emoji_and_options():
    a = P.parse("$tsla 11/28 350 put 🦴 1.2M repeated vol>oi on 11/18 guess bull", TODAY)
    assert (a.symbol, a.strike, a.kind, a.side, a.premium) == ("TSLA", 350, "put", "bid", 1_200_000)
    assert a.tags == ["repeated", "vol>oi"] and a.alert_day == date(2026, 11, 18) and a.guess == "bull"
    assert a.direction == "bullish"   # put sold on the bid


def test_direction_table():
    assert P.direction("call", "ask") == "bullish"
    assert P.direction("call", "bid") == "bearish"
    assert P.direction("put", "ask") == "bearish"
    assert P.direction("put", "bid") == "bullish"
    assert P.direction("put", "") == "unclear"


def test_expiry_rolls_into_next_year_and_full_dates():
    assert P.parse("SPY 1/16 600C ask", date(2026, 12, 1)).expiry == date(2027, 1, 16)
    assert P.parse("SPY 2027-01-15 600P", TODAY).expiry == date(2027, 1, 15)


def test_parse_errors_are_plain():
    with pytest.raises(ValueError, match="ticker"):
        P.parse("bought calls on apple", TODAY)
    with pytest.raises(ValueError, match="call or a put"):
        P.parse("AAPL 12/20 ask 250K", TODAY)


def _closes(values, start="2026-11-16"):
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)))


def test_move_needs_closed_sessions():
    s = _closes([100, 102, 101, 103, 104, 110, 111])          # Mon 11/16 .. Tue 11/24
    assert O.move(s, date(2026, 11, 16), 1)[:2] == (100, 102)
    assert O.move(s, date(2026, 11, 16), 5)[:2] == (100, 110)
    assert O.move(s, date(2026, 11, 22), 1)[:2] == (104, 110)  # Sunday alert uses Friday's close
    assert O.move(s, date(2026, 11, 24), 1) is None              # tomorrow has not happened


def test_full_flow_log_and_scorecard():
    when = datetime(2026, 11, 16, 11, 0, tzinfo=ET)
    replies = app.add_text("AAPL 12/18 230C ask $600K sweep guess bull\nNVDA 12/18 150P ask 150K\nnonsense line", when)
    assert replies[0].startswith("#1 AAPL") and "bullish" in replies[0]
    assert "bearish" in replies[1] and replies[2].startswith("Skipped")
    data = {"AAPL": _closes([100, 101, 99, 98, 97, 105]), "NVDA": _closes([50, 49, 48, 47, 46, 45]),
            "SPY": _closes([600, 601, 602, 603, 604, 606])}
    fn = lambda sym, oldest, now: data[sym]
    done = app.fill_outcomes(datetime(2026, 11, 23, 17, 0, tzinfo=ET), fn)
    assert len(done) == 4 and any("1 week: +5.0%, SPY +1.0% = right" in d for d in done)
    assert app.fill_outcomes(datetime(2026, 11, 23, 18, 0, tzinfo=ET), fn) == []   # nothing left to fill
    rs = S.rows()
    assert rs[0]["v_1d"] == "right" and rs[0]["v_1w"] == "right" and rs[1]["v_1w"] == "right"
    card = O.scorecard(rs)
    assert "1 week 2/2 (100%)" in card and "Your guesses" in card and "mostly luck" in card
    assert "size $500K-1M" in card and "tag sweep" in card


def test_phone_words():
    app.add_text("AMD 12/18 200C ask 300K", datetime(2026, 11, 16, 11, 0, tzinfo=ET))
    assert "#1" in app.command("list")
    assert app.command("delete 1") == "Deleted #1."
    assert app.command("delete 1") == "No alert #1."
    assert "report" in app.command("help")
    assert app.command("AMD 12/18 200C") is None


def test_today_forming_bar_is_ignored(monkeypatch):
    import broker as B
    s = _closes([100, 101, 102, 103, 104])          # last bar is Fri 11/20
    monkeypatch.setattr(B, "stock_daily_closes", lambda sym, days: s)
    during = app._closes("AAPL", date(2026, 11, 16), datetime(2026, 11, 20, 12, 0, tzinfo=ET))
    after = app._closes("AAPL", date(2026, 11, 16), datetime(2026, 11, 20, 16, 30, tzinfo=ET))
    assert len(during) == 4 and len(after) == 5


def test_export(tmp_path):
    app.add_text("META 12/18 700C ask 200K block", datetime(2026, 11, 16, 11, 0, tzinfo=ET))
    p = tmp_path / "out.csv"
    assert app.export(str(p)) == 1 and "META" in p.read_text()
