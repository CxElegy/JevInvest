"""債券の為替ヘッジ比較（tools/hedge_lab.py）の式。ネットワーク不要。"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.hedge_lab import daily_jpy_returns, portfolio  # noqa: E402


def _px():
    idx = pd.bdate_range("2024-01-01", periods=4)
    px = pd.DataFrame({"VT": [100, 100, 100, 100], "QQQ": [100] * 4, "1348.T": [100] * 4,
                       "AGG": [100, 101, 101, 101], "BIL": [100, 100.5, 100.5, 100.5]}, index=idx, dtype=float)
    fx = pd.Series([150, 140, 140, 140], index=idx, dtype=float)
    return px, fx


def test_hedged_bond_ignores_fx_and_pays_rate_gap():
    px, fx = _px()
    r = daily_jpy_returns(px, fx, basis=0.0)
    day = r.index[1]
    # ドル建て +1%、円高 150→140: 為替そのままなら円で約 -5.7%
    assert r.loc[day, "unhedged"] == pytest.approx(101 * 140 / (100 * 150) - 1)
    # ヘッジ後: +1% − 米短期 0.5% + 日本の金利（2024-01 は -0.1%/年、1 日分）
    assert r.loc[day, "hedged"] == pytest.approx(0.01 - 0.005 + ((1 - 0.001) ** (1 / 365) - 1))


def test_portfolio_rebalances_monthly_and_keeps_rest_in_cash():
    px, fx = _px()
    r = daily_jpy_returns(px, fx, basis=0.0)
    nav = portfolio(r, {"1348.T": 0.5, "BOND": 0.25}, "yen_cash", "2024-01-01", None, crisis_proxy=False)
    # 日本株は横ばい、円の現金はほぼ 0、残り 25% は利息なしの現金 → ほぼ 1 のまま
    assert nav.iloc[-1] == pytest.approx(1.0, abs=1e-4)


def test_us_stock_loses_when_yen_rises():
    px, fx = _px()
    r = daily_jpy_returns(px, fx, basis=0.0)
    nav = portfolio(r, {"VT": 1.0}, "yen_cash", "2024-01-01", None, crisis_proxy=False)
    assert nav.iloc[-1] == pytest.approx(140 / 150)
