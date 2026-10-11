"""今月の積立の振り分け（src/nextbuy.py）。ネットワーク不要。"""

import math

import pytest

from src.nextbuy import render, split

T = {"developed_equity": 60, "bond": 30, "cash": 10}
OK = {"developed_equity": ["AAA"], "bond": ["BBB"]}


def test_money_goes_to_underweight_first():
    # 株が上がって 70%、債券 20% → 今月の 10 万は債券に多く入る
    s = split({"developed_equity": 700_000, "bond": 200_000, "cash": 100_000}, T, 100_000, OK)
    a = {r.asset_class: r.amount for r in s.rows}
    assert a["bond"] > a["developed_equity"]
    assert sum(a.values()) == pytest.approx(100_000)
    assert a["developed_equity"] == pytest.approx(0)          # 目標を超えているので 0
    assert s.drift_after < s.drift_naive < s.drift_before


def test_when_gaps_are_small_rest_follows_target():
    s = split({"developed_equity": 600_000, "bond": 300_000, "cash": 100_000}, T, 100_000, OK)
    a = {r.asset_class: r.amount for r in s.rows}
    assert a == pytest.approx({"developed_equity": 60_000, "bond": 30_000, "cash": 10_000})


def test_class_without_cleared_symbol_is_parked_not_bought():
    s = split({"developed_equity": 0.0, "bond": 0.0, "cash": 0.0}, T, 100_000, {"developed_equity": ["AAA"]})
    rows = {r.asset_class: r for r in s.rows}
    assert rows["bond"].amount == 0 and rows["bond"].parked == pytest.approx(30_000)
    assert "購入可の銘柄なし" in rows["bond"].status
    assert s.parked == pytest.approx(30_000)
    out = render(s)
    assert "留保" in out


def test_unpriced_class_refuses():
    with pytest.raises(ValueError, match="評価不能"):
        split({"developed_equity": math.nan, "bond": 1.0}, T, 100_000, OK)


def test_over_band_is_flagged_but_not_sold():
    s = split({"developed_equity": 800_000, "bond": 100_000, "cash": 100_000}, T, 50_000, OK)
    assert s.over_band and s.over_band[0][0] == "developed_equity"
    assert all(r.amount >= 0 for r in s.rows)
    assert "/before-trade" in render(s)


def test_pct_only_hides_amounts():
    s = split({"developed_equity": 700_000, "bond": 200_000, "cash": 100_000}, T, 123_456, OK)
    out = render(s, pct_only=True)
    assert "123" not in out and " 円" not in out
