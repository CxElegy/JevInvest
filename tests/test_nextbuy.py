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


def test_parked_cash_shows_up_without_cash_class_and_rows_sum_to_100():
    s = split({"equity": 600_000, "bond": 300_000}, {"equity": 60, "bond": 40}, 100_000, {"equity": ["VT"]})
    out = render(s)
    assert "（留保した現金）" in out
    after = [float(line.split("|")[6]) for line in out.splitlines() if line.startswith("| ") and "資産クラス" not in line]
    assert sum(after) == pytest.approx(100, abs=0.2)
    assert any("ずれは毎月広がる" in n for n in s.notes)


def test_custom_cash_class_name_holds_parked_money():
    s = split({"eq": 500_000, "bond": 400_000, "mmf": 100_000}, {"eq": 60, "bond": 30, "mmf": 10}, 100_000,
              {"bond": ["B"]}, cash_classes={"mmf"})
    out = render(s)
    after = [float(line.split("|")[6]) for line in out.splitlines() if line.startswith("| ") and "資産クラス" not in line]
    assert sum(after) == pytest.approx(100, abs=0.2)


def test_target_not_summing_to_100_is_reported():
    s = split({"equity": 1.0, "bond": 1.0}, {"equity": 54, "bond": 36}, 100_000, {"equity": ["A"], "bond": ["B"]})
    assert any("合計が 90%" in n for n in s.notes)
