"""追加した戦略ごとの規則（合成の価格で、手で確かめられる場面だけ）。"""

import pandas as pd
import pytest

from src import sim


def _hist(trends: dict[str, float], days=300, noise=None):
    """銘柄ごとに一定の日次リターンで動く価格。noise は {銘柄: 振れ幅}（交互に ±）。"""
    idx = pd.bdate_range("2025-01-01", periods=days)
    out = {}
    for s, r in trends.items():
        v, vals = 100.0, []
        for k in range(days):
            amp = (noise or {}).get(s, 0.0)
            v *= 1 + r + (amp if k % 2 else -amp)
            vals.append(v)
        out[s] = pd.Series(vals, index=idx)
    return out


def test_risk_parity_weights_are_inverse_vol_and_sum_to_one():
    h = _hist({"VT": 0.0, "TLT": 0.0, "GLD": 0.0, "DBC": 0.0},
              noise={"VT": 0.02, "TLT": 0.01, "GLD": 0.01, "DBC": 0.02})
    w = sim.risk_parity(h)
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["TLT"] == pytest.approx(2 * w["VT"], rel=1e-6)     # 変動が半分なら倍持つ


def test_momentum_top3_picks_strongest_above_sma():
    trends = {s: 0.0 for s in sim.MOM_ASSETS}
    trends.update({"QQQ": 0.003, "GLD": 0.002, "DBC": 0.001, "TLT": -0.001})
    w = sim.momentum_top3(_hist(trends))
    assert set(w) == {"QQQ", "GLD", "DBC"} and sum(w.values()) == pytest.approx(1.0)


def test_vol_target_scales_down_only_when_volatile():
    calm = sim.vol_target(_hist({"VT": 0.0}, noise={"VT": 0.001}))
    wild = sim.vol_target(_hist({"VT": 0.0}, noise={"VT": 0.03}))
    assert calm == {"VT": 1.0}
    assert wild["VT"] < 0.5 and wild["VT"] + wild["BIL"] == pytest.approx(1.0)


def test_spy_timing_and_sell_in_may_and_dip():
    up, down = _hist({"SPY": 0.001}), _hist({"SPY": -0.001})
    assert sim.spy_timing(up) == {"SPY": 1.0} and sim.spy_timing(down) == {"BIL": 1.0}
    assert sim.sell_in_may({}, pd.Timestamp("2026-05-01")) == {"BIL": 1.0}
    assert sim.sell_in_may({}, pd.Timestamp("2026-11-02")) == {"VT": 1.0}
    assert sim.buy_the_dip(_hist({"VT": 0.001})) is None               # 高値圏では待つ
    assert sim.buy_the_dip(_hist({"VT": -0.001})) == {"VT": 1.0}       # 1 年高値から 10% 超下げ


def test_fixed_allocations_sum_to_at_most_one():
    for k, (fn, _) in sim.ALL_STRATEGIES.items():
        w = fn(_hist({s: 0.0005 for s in sim.UNIVERSE}), pd.Timestamp("2026-01-05"))
        if w is None:
            continue
        assert all(v >= 0 for v in w.values()), k
        assert sum(w.values()) <= 1.0 + 1e-9, k
        assert set(w) <= set(sim.UNIVERSE), k


def test_yearly_rebalance_only_in_january_unless_contributing():
    d = pd.Timestamp
    assert sim.is_due("yearly", d("2027-01-04"), d("2026-12-31"), True)
    assert not sim.is_due("yearly", d("2026-11-02"), d("2026-10-30"), True)
    assert sim.is_due("yearly", d("2026-11-02"), d("2026-10-30"), True, contributing=True)
    assert not sim.is_due("once", d("2026-11-02"), d("2026-10-30"), True)
