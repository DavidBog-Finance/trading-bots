import numpy as np
import pandas as pd
import pytest

import strategies as S


def series(values, start="2020-01-01"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="D"), dtype=float)


def test_trend_is_on_in_uptrend_and_off_in_downtrend():
    up = series(np.linspace(100, 200, 300))
    down = series(np.linspace(200, 100, 300))
    assert S.trend_positions(up).iloc[-1] == 1.0
    assert S.trend_positions(down).iloc[-1] == 0.0


def test_trend_band_holds_state_inside_band():
    c = series(np.r_[np.linspace(100, 150, 250), np.full(5, 0)])
    sma_last = c.iloc[-205:-5].mean()
    c.iloc[-5:] = sma_last * 1.002  # inside the 1% band -> keep previous state (on)
    assert S.trend_positions(c).iloc[-1] == 1.0


def test_rsi_bounds():
    rng = np.random.default_rng(0)
    c = series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 500))))
    r = S.rsi_wilder(c, 2)
    assert r.between(0, 100).all()


def test_rsi2_buys_dip_in_uptrend_and_exits_on_bounce():
    base = list(np.linspace(100, 160, 260))
    dip = [155, 150, 146]          # sharp 3-day drop -> RSI(2) very low
    bounce = [158, 162]            # back above 5-day SMA -> exit
    c = series(base + dip + bounce)
    pos = S.rsi2_positions(c)
    assert pos.iloc[260 + 2] == 1.0
    assert pos.iloc[-1] == 0.0


def test_stock_blend_is_average_of_sleeves():
    rng = np.random.default_rng(1)
    c = series(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, 800))))
    blend = S.stock_positions(c, "blend")
    assert set(np.unique(blend)).issubset({0.0, 0.5, 1.0})


def test_crypto_weights_never_leverage():
    rng = np.random.default_rng(2)
    c = series(100 * np.exp(np.cumsum(rng.normal(0.001, 0.04, 800))))
    w = S.crypto_raw_weights(c)
    assert (w >= 0).all() and (w <= 1).all()


def test_rebalance_band():
    assert not S.needs_rebalance(0.50, 0.55, 0.10)
    assert S.needs_rebalance(0.50, 0.61, 0.10)
    assert S.needs_rebalance(0.05, 0.0, 0.10)   # always exit fully when target is 0


def test_banded_path_matches_live_rule():
    rng = np.random.default_rng(3)
    c = series(100 * np.exp(np.cumsum(rng.normal(0.001, 0.04, 600))))
    raw = S.crypto_raw_weights(c)
    banded = S.apply_rebalance_band(raw, 0.10)
    held = 0.0
    for r, b in zip(raw, banded):
        if S.needs_rebalance(held, r, 0.10):
            held = r
        assert b == pytest.approx(held)
