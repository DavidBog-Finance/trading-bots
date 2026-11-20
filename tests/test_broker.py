"""Exercises the order logic against a fake Alpaca client (no network)."""
import types

import pytest
from alpaca.common.exceptions import APIError

import broker as B
import config as C


class FakeClient:
    def __init__(self, positions, deposits=()):
        self.positions = positions  # symbol -> (market_value, qty)
        self.deposits = list(deposits)
        self.orders, self.closed = [], []

    def get_open_position(self, sym):
        if sym not in self.positions:
            raise APIError("position does not exist")
        v, q = self.positions[sym]
        return types.SimpleNamespace(market_value=v, qty=q)

    def get_all_positions(self):
        return []

    def get(self, path, params=None):
        assert path == "/account/activities"
        return [{"id": str(i), "net_amount": str(a)} for i, a in enumerate(self.deposits)]

    def submit_order(self, req):
        self.orders.append(req)
        return types.SimpleNamespace(id="x", status="accepted")

    def close_position(self, sym):
        self.closed.append(sym)


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch, tmp_path):
    monkeypatch.setattr(B, "LOG_FILE", str(tmp_path / "t.csv"))
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "NTFY_TOPIC", "")
    monkeypatch.setattr(C, "ALERT_WEBHOOK_URL", "")
    monkeypatch.setattr(C, "NET_DEPOSITS_OVERRIDE", 0)


def test_buys_the_difference():
    fc = FakeClient({})
    B.rebalance(fc, "t", "SPY", 100, 500, crypto=False, eq=1000)
    assert fc.orders[0].notional == 100 and fc.orders[0].side.value == "buy"


def test_sells_partial_by_qty():
    fc = FakeClient({"BTCUSD": (200.0, 0.002)})
    B.rebalance(fc, "t", "BTC/USD", 100, 100000, crypto=True, eq=1000)
    o = fc.orders[0]
    assert o.side.value == "sell" and o.qty == pytest.approx(0.001)


def test_zero_target_closes_position():
    fc = FakeClient({"SPY": (150.0, 0.3)})
    B.rebalance(fc, "t", "SPY", 0, 500, crypto=False, eq=1000)
    assert fc.closed == ["SPY"] and not fc.orders


def test_order_cap_blocks_big_orders():
    fc = FakeClient({})
    B.rebalance(fc, "t", "SPY", 900, 500, crypto=False, eq=1000)  # 90% of equity > 60% cap
    assert not fc.orders


def test_dust_and_drift_are_ignored():
    fc = FakeClient({"SPY": (98.0, 0.2)})
    B.rebalance(fc, "t", "SPY", 100, 500, crypto=False, eq=1000)
    assert not fc.orders
    fc = FakeClient({"SPY": (480.0, 1)})
    B.rebalance(fc, "t", "SPY", 500, 500, crypto=False, eq=2000, tolerance=0.05)  # $20 < 5% of $500
    assert not fc.orders


def test_net_deposits_sums_deposits_and_withdrawals():
    assert B.net_deposits(FakeClient({}, deposits=[500, 300, -100])) == 700


def test_sleeve_budget_scales_with_equity_and_respects_cap():
    assert B.sleeve_budget(2000, 0.45, 0) == 900
    assert B.sleeve_budget(2000, 0.45, 500) == 500
