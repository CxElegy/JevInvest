"""未来の分布（web/simengine.js の bootstrap）。合成データで、手計算できる性質を確かめる。"""

import json
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node が無い")


def _data(tmp_path, monthly_growth=0.01, months=60):
    """毎月ちょうど monthly_growth だけ上がる円建て資産（1348.T）と、動かない為替。"""
    days = pd.bdate_range("2015-01-01", periods=months * 21 + 5)
    vals, v, cur = [], 100.0, days[0].month
    for d in days:
        if d.month != cur:
            v *= 1 + monthly_growth
            cur = d.month
        vals.append(round(v, 8))
    data = {"dates": [d.strftime("%Y-%m-%d") for d in days], "symbols": ["1348.T"],
            "adj": {"1348.T": vals}, "fx": [150.0] * len(days)}
    f = tmp_path / "d.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


def _run(f, **opts):
    base = {"weights": {"1348.T": 1.0}, "initial": 1_000_000, "monthly": 0, "years": 10,
            "paths": 300, "block": 12, "seed": 7, "haircut": 0}
    base.update(opts)
    out = subprocess.run(["node", str(ROOT / "tests" / "future_runner.js"), str(f), json.dumps(base)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_constant_returns_give_the_closed_form(tmp_path):
    f = _data(tmp_path, 0.01)
    r = _run(f, monthly=10_000)
    # 毎月 1%、初期 100 万 + 毎月 1 万を 120 か月 → 全経路が同じ
    v = 1_000_000.0
    for _ in range(120):
        v = v * 1.01 + 10_000
    assert r["final"]["p05"] == pytest.approx(v, rel=1e-9)
    assert r["final"]["p95"] == pytest.approx(v, rel=1e-9)
    assert r["paid"] == 1_000_000 + 10_000 * 120
    assert r["pLoss"] == 0 and r["dd"]["p05"] == pytest.approx(0, abs=1e-12)


def test_cash_weight_and_haircut_lower_the_result(tmp_path):
    f = _data(tmp_path, 0.01)
    full = _run(f)["final"]["p50"]
    half = _run(f, weights={"1348.T": 0.5})["final"]["p50"]
    cut = _run(f, haircut=0.05)["final"]["p50"]
    assert half == pytest.approx(1_000_000 * 1.005 ** 120, rel=1e-9)
    assert cut < full and half < full


def test_same_seed_same_paths(tmp_path):
    f = _data(tmp_path, 0.0)
    a = _run(f, seed=3)
    b = _run(f, seed=3)
    assert a == b


def test_haircut_applies_only_to_the_invested_part(tmp_path):
    f = _data(tmp_path, 0.0)
    r = _run(f, weights={"1348.T": 0.5}, haircut=0.12)
    monthly_cut = (1.12 ** (1 / 12) - 1) * 0.5
    assert r["final"]["p50"] == pytest.approx(1_000_000 * (1 - monthly_cut) ** 120, rel=1e-9)
