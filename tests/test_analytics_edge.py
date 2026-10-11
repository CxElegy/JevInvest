"""評価不能・複数口座・現金を含む台帳での集計の正しさ。"""

import math

import pytest

from src.analytics import analyze
from src.holdings import Account, Holdings, Position
from src.report import to_markdown

from conftest import FakeMarketData


def _pos(**k):
    return Position(**{"name": k["symbol"], **k})


NVDA = _pos(symbol="NVDA", asset_class="us", account="A", quantity=100,
            cost_basis_per_unit=100, currency="USD", cost_fx_rate=150)
CASH = _pos(symbol="CASH-JPY", asset_class="cash", account="A", quantity=1_000_000,
            cost_basis_per_unit=1, currency="JPY")


def test_report_does_not_crash_when_every_position_is_unpriced(tmp_path):
    md = FakeMarketData(tmp_path / "m.sqlite", {})
    h = Holdings("JPY", {"us": 100}, [Account("A", "taxable")], [NVDA], guards={})
    a = analyze(h, md, with_risk=False)
    text = to_markdown(a)
    assert "評価不能" in text and "nan" not in text.lower()
    md.close()


def test_report_shows_unpriced_rows_without_nan(market_data):
    h = Holdings("JPY", {"us": 80, "cash": 20}, [Account("A", "taxable")], [NVDA, CASH],
                 guards={})
    a = analyze(h, market_data, with_risk=False)
    assert "nan" not in to_markdown(a).lower()


def test_rebalance_does_not_tell_to_buy_a_class_held_only_via_unpriced_positions(market_data):
    h = Holdings("JPY", {"us": 80, "cash": 20}, [Account("A", "taxable")], [NVDA, CASH],
                 guards={})
    a = analyze(h, market_data, with_risk=False)
    us = a.rebalance.loc["us"]
    assert math.isnan(us["調整額"]) and math.isnan(us["現在%"])
    assert "評価不能" in us["状態"]
    assert any("調整額" in w for w in a.warnings)
    assert "評価不能" in to_markdown(a)


def _multi_account_with_cash():
    return Holdings(
        "JPY", {"eq": 60, "jp": 20, "cash": 20},
        [Account("A", "taxable"), Account("B", "nisa")],
        [
            _pos(symbol="VT", asset_class="eq", account="A", quantity=100,
                 cost_basis_per_unit=100, currency="USD", cost_fx_rate=150),
            _pos(symbol="VT", asset_class="eq", account="B", quantity=100,
                 cost_basis_per_unit=100, currency="USD", cost_fx_rate=150),
            _pos(symbol="7203.T", asset_class="jp", account="A", quantity=100,
                 cost_basis_per_unit=2800, currency="JPY"),
            _pos(symbol="CASH-JPY", asset_class="cash", account="A", quantity=3_000_000,
                 cost_basis_per_unit=1, currency="JPY"),
        ],
        guards={},
    )


def _weight(a, sym):
    return sum(v.weight_pct for v in a.valuations if v.position.symbol == sym)


def test_concentration_sums_a_symbol_across_accounts_and_ignores_cash(market_data):
    a = analyze(_multi_account_with_cash(), market_data, with_risk=False)
    c = a.concentration
    assert c["実銘柄数"] == 2                                   # VT と 7203.T（現金は銘柄ではない）
    assert c["最大1銘柄%"] == pytest.approx(_weight(a, "VT"))   # 2 口座の VT を合算


def test_risk_weights_sum_accounts_and_count_yen_cash(market_data):
    a = analyze(_multi_account_with_cash(), market_data, years=1)
    w = a.risk["銘柄別"]["ウェイト%"]
    assert w["VT"] == pytest.approx(_weight(a, "VT"))           # 口座別に上書きしない
    assert w["7203.T"] == pytest.approx(_weight(a, "7203.T"))
    assert w.sum() == pytest.approx(100.0)                       # 円現金も無リスク資産として入る
    assert a.risk["カバー率%"] == pytest.approx(100.0)
