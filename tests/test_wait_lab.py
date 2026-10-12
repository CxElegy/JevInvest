"""NISA の枠を待つか、課税口座で先に投資するか（tools/wait_lab.py）。ネットワーク不要。"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.wait_lab import H, TAX, compare  # noqa: E402


def _monthly(growth, months=H * 12 + 13):
    idx = pd.date_range("2010-01-01", periods=months, freq="MS")
    return pd.Series([(1 + growth) ** i for i in range(months)], index=idx)


def test_rising_market_closed_form():
    g = _monthly(0.01)
    cash = _monthly(0.0)
    row = compare(g, cash, 1)[0]
    gk, ge = 1.01 ** 12, 1.01 ** (H * 12)
    assert row["wait"] == pytest.approx(ge / gk)
    assert row["move"] == pytest.approx((gk - TAX * (gk - 1)) * ge / gk)
    assert row["stay"] == pytest.approx(ge - TAX * (ge - 1))
    assert row["move"] > row["wait"]


def test_flat_market_waiting_with_interest_wins():
    g = _monthly(0.0)
    cash = _monthly(0.001)
    row = compare(g, cash, 2)[0]
    assert row["wait"] > row["move"] == pytest.approx(1.0)


def test_loss_is_not_taxed():
    g = _monthly(-0.005)
    row = compare(g, _monthly(0.0), 1)[0]
    assert row["stay"] == pytest.approx(g.iloc[H * 12] / g.iloc[0])
