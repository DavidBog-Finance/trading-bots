from datetime import date

import strategies as S
import options_bot


def test_parse_occ():
    assert options_bot.parse_occ("SPY261218P00640000") == {"expiry": date(2026, 12, 18), "type": "P", "strike": 640.0}
    assert options_bot.parse_occ("SPY") is None


def test_choose_expiry_closest_to_45_days():
    today = date(2026, 10, 22)
    exps = [date(2026, 11, 20), date(2026, 12, 4), date(2026, 12, 18), date(2027, 1, 15)]
    assert S.choose_expiry(exps, today) == date(2026, 12, 4)  # 43 days
    assert S.choose_expiry([date(2026, 10, 30)], today) is None


def chain():
    rows = []
    for k, d in [(660, -0.40), (650, -0.32), (640, -0.25), (630, -0.19), (620, -0.15), (610, -0.11), (600, -0.08)]:
        rows.append({"strike": k, "delta": d, "bid": abs(d) * 30, "ask": abs(d) * 30 + 0.1})
    return rows


def test_pick_spread_uses_25_delta_and_3pct_width():
    short, long = S.pick_put_spread(chain(), spot=680)
    assert short["strike"] == 640
    assert long["strike"] == 620  # 640 - 3% * 680 = 619.6


def test_sizing_and_exits():
    assert S.spread_contracts(2000, 20, 4.0) == 1      # max loss $1,600
    assert S.spread_contracts(1000, 20, 4.0) == 0      # doesn't fit
    assert S.spread_exit_reason(4.0, 1.9, 30).startswith("take profit")
    assert S.spread_exit_reason(4.0, 3.0, 21).startswith("21 days")
    assert S.spread_exit_reason(4.0, 3.0, 30) is None


def test_bs_delta_sane():
    d = options_bot.bs_put_delta(680, 640, 45 / 365, 0.18)
    assert -0.30 < d < -0.10


# ---------- full options-bot run against fakes ----------
import types
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import broker as B
import config as C
from tests.test_broker import FakeClient


class FakeData:
    def __init__(self, spot, expiry):
        self.spot, self.expiry = spot, expiry

    def get_option_chain(self, req):
        out = {}
        for k in range(560, 690, 5):
            d = options_bot.bs_put_delta(self.spot, k, 45 / 365, 0.18)
            px = max(0.05, abs(d) * 25)
            sym = f"SPY{self.expiry:%y%m%d}P{int(k * 1000):08d}"
            out[sym] = types.SimpleNamespace(latest_quote=types.SimpleNamespace(bid_price=px - 0.05, ask_price=px + 0.05),
                                             greeks=types.SimpleNamespace(delta=d), implied_volatility=0.18)
        return out


def test_options_bot_opens_spread_when_big_enough(monkeypatch, tmp_path):
    fc = FakeClient({}, deposits=[60000])
    now = datetime.now(timezone.utc)
    fc.get_clock = lambda: types.SimpleNamespace(is_open=True, timestamp=now, next_close=now + timedelta(minutes=20))
    fc.get_account = lambda: types.SimpleNamespace(equity="60000", trading_blocked=False, account_blocked=False)
    fc.get_orders = lambda req: []
    for k, v in {"OPTIONS_ENABLED": True, "DRY_RUN": False, "NTFY_TOPIC": "", "ALERT_WEBHOOK_URL": "", "KILL_SWITCH": False}.items():
        monkeypatch.setattr(C, k, v)
    monkeypatch.setattr(B, "trading_client", lambda: fc)
    monkeypatch.setattr(B, "LOG_FILE", str(tmp_path / "t.csv"))
    idx = pd.date_range(end=pd.Timestamp.now().normalize(), periods=300)
    closes = pd.Series(np.linspace(500, 680, 300), index=idx)
    monkeypatch.setattr(options_bot.stock_bot, "load", lambda s: (closes, 680.0))
    expiry = (now + timedelta(days=45)).date()
    monkeypatch.setattr(options_bot, "data_client", lambda: FakeData(680, expiry))
    options_bot.main()
    assert len(fc.orders) == 1
    o = fc.orders[0]
    assert o.order_class.value == "mleg" and o.limit_price < 0 and o.qty >= 1
    assert o.legs[0].position_intent.value == "sell_to_open"


def test_options_bot_waits_when_account_small(monkeypatch, tmp_path):
    fc = FakeClient({}, deposits=[1500])
    now = datetime.now(timezone.utc)
    fc.get_clock = lambda: types.SimpleNamespace(is_open=True, timestamp=now, next_close=now + timedelta(minutes=20))
    fc.get_account = lambda: types.SimpleNamespace(equity="1500", trading_blocked=False, account_blocked=False)
    fc.get_orders = lambda req: []
    for k, v in {"OPTIONS_ENABLED": True, "DRY_RUN": False, "NTFY_TOPIC": "", "ALERT_WEBHOOK_URL": "", "KILL_SWITCH": False}.items():
        monkeypatch.setattr(C, k, v)
    monkeypatch.setattr(B, "trading_client", lambda: fc)
    monkeypatch.setattr(B, "LOG_FILE", str(tmp_path / "t.csv"))
    idx = pd.date_range(end=pd.Timestamp.now().normalize(), periods=300)
    monkeypatch.setattr(options_bot.stock_bot, "load", lambda s: (pd.Series(np.linspace(500, 680, 300), index=idx), 680.0))
    monkeypatch.setattr(options_bot, "data_client", lambda: FakeData(680, (now + timedelta(days=45)).date()))
    options_bot.main()
    assert not fc.orders
