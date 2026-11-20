"""Runs both bots end to end with fake data and a fake account (no network)."""
import types
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import broker as B
import config as C
import crypto_bot
import stock_bot
from tests.test_broker import FakeClient


def fake_prices(n, end, drift, vol, seed):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end=end, periods=n, freq="D")
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(drift, vol, n))), index=idx)


def setup(monkeypatch, tmp_path, positions):
    fc = FakeClient(positions)
    now = datetime.now(timezone.utc)
    fc.get_clock = lambda: types.SimpleNamespace(is_open=True, timestamp=now, next_close=now + timedelta(minutes=20))
    fc.get_account = lambda: types.SimpleNamespace(equity="1000", trading_blocked=False, account_blocked=False)
    monkeypatch.setattr(B, "trading_client", lambda: fc)
    monkeypatch.setattr(B, "LOG_FILE", str(tmp_path / "t.csv"))
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "NTFY_TOPIC", "")
    monkeypatch.setattr(C, "ALERT_WEBHOOK_URL", "")
    monkeypatch.setattr(C, "KILL_SWITCH", False)
    return fc


def test_stock_bot_runs(monkeypatch, tmp_path):
    fc = setup(monkeypatch, tmp_path, {})
    fc.deposits = [1000]
    yesterday = pd.Timestamp.now().normalize() - pd.Timedelta(days=1)
    closes = fake_prices(500, yesterday, 0.002, 0.005, 0)   # strong uptrend -> trend sleeve on
    monkeypatch.setattr(B, "stock_daily_closes", lambda s: closes)
    monkeypatch.setattr(B, "stock_latest_price", lambda s: float(closes.iloc[-1]))
    stock_bot.main()
    assert fc.orders and fc.orders[0].side.value == "buy"


def test_crypto_bot_runs(monkeypatch, tmp_path):
    fc = setup(monkeypatch, tmp_path, {})
    yesterday = pd.Timestamp.now().normalize() - pd.Timedelta(days=1)
    monkeypatch.setattr(B, "crypto_daily_closes", lambda s: fake_prices(400, yesterday, 0.004, 0.02, 1))
    crypto_bot.main()
    assert fc.orders and fc.orders[0].side.value == "buy"


def test_kill_switch_flattens(monkeypatch, tmp_path):
    fc = setup(monkeypatch, tmp_path, {"BTCUSD": (150.0, 0.002)})
    monkeypatch.setattr(C, "KILL_SWITCH", True)
    import pytest
    with pytest.raises(SystemExit):
        crypto_bot.main()
    assert fc.closed == ["BTCUSD"]


def test_loss_limit_flattens_and_stops(monkeypatch, tmp_path):
    import pytest
    fc = setup(monkeypatch, tmp_path, {"BTCUSD": (150.0, 0.002)})
    fc.deposits = [2000]  # equity is $1,000: down 50% > 35% limit
    with pytest.raises(SystemExit):
        crypto_bot.main()
    assert fc.closed == ["BTCUSD"]


def test_multi_coin_splits_budget(monkeypatch, tmp_path):
    fc = setup(monkeypatch, tmp_path, {})
    monkeypatch.setattr(C, "CRYPTO_SYMBOLS", ["BTC/USD", "ETH/USD"])
    yesterday = pd.Timestamp.now().normalize() - pd.Timedelta(days=1)
    monkeypatch.setattr(B, "crypto_daily_closes", lambda s: fake_prices(400, yesterday, 0.004, 0.02, 1))
    crypto_bot.main()
    assert len(fc.orders) == 2
    assert all(o.notional <= 1000 * C.CRYPTO_PCT / 2 + 0.01 for o in fc.orders)
