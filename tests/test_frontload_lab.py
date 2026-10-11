"""年初一括 vs 毎月（tools/frontload_lab.py）の計算。ネットワーク不要。"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.frontload_lab import ANNUAL, compare  # noqa: E402


def _nav(growth_per_day):
    idx = pd.bdate_range("2019-12-02", "2021-01-29")
    return pd.Series([(1 + growth_per_day) ** i for i in range(len(idx))], index=idx)


def test_rising_market_favours_lump():
    r = compare(_nav(0.0005), 2020, 1)
    assert r["lump"] > r["monthly"] and r["diff"] > 0
    assert r["worst_gap"] == pytest.approx(0, abs=1e-6) or r["worst_gap"] >= -1


def test_flat_market_is_a_tie():
    r = compare(_nav(0.0), 2020, 1)
    assert r["lump"] == pytest.approx(ANNUAL) and r["monthly"] == pytest.approx(ANNUAL)


def test_falling_market_favours_monthly_and_shows_regret():
    r = compare(_nav(-0.0005), 2020, 1)
    assert r["diff"] < 0 and r["worst_gap"] < 0


def test_incomplete_year_is_skipped():
    assert compare(_nav(0.0), 2021, 1) is None
