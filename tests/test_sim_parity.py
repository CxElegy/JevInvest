"""ブラウザ版エンジン（web/simengine.js）が Python 版（src/sim.py）と同じ数字を出すか。

同じデータで両方のバックテストを回し、毎日の評価額と約定を突き合わせる。
node が無い環境では飛ばす。
"""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.sim import ALL_STRATEGIES, UNIVERSE, Prices, backtest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node が無い")


def _payload(seed=0, days=700):
    """休場（東証だけ・米国だけ）と為替のある合成データ。トレンドの切り替わりも入れる。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-01-02", periods=days)
    adj = {}
    for k, s in enumerate(UNIVERSE):
        drift = 0.0006 * np.sin(np.arange(days) / (60 + 7 * k))     # 上げ下げを繰り返す
        v = 100 * (1 + k) * np.cumprod(1 + drift + rng.normal(0, 0.01, days))
        col = [round(float(x), 4) for x in v]
        hol = rng.choice(days, 12, replace=False)
        for h in hol:
            if (s.endswith(".T") and h % 2 == 0) or (not s.endswith(".T") and h % 2 == 1):
                col[h] = None
        adj[s] = col
    fx = [round(float(x), 4) for x in 140 * np.cumprod(1 + rng.normal(0, 0.004, days))]
    return {"dates": [d.strftime("%Y-%m-%d") for d in idx], "symbols": UNIVERSE, "adj": adj, "fx": fx}


def _python_prices(data):
    idx = pd.to_datetime(data["dates"])
    series = {s: pd.Series(data["adj"][s], index=idx, dtype=float).dropna() for s in UNIVERSE}
    return Prices(series, pd.Series(data["fx"], index=idx, dtype=float))


@pytest.mark.parametrize("monthly,taxable", [(0, False), (200_000, False), (0, True), (200_000, True)])
def test_js_engine_matches_python(tmp_path, monthly, taxable):
    data = _payload()
    f = tmp_path / "d.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    start = data["dates"][300]
    js = json.loads(subprocess.run(
        ["node", str(ROOT / "tests" / "sim_parity_runner.js"), str(f), start, "5000000", str(monthly), "1" if taxable else "0"],
        capture_output=True, text=True, check=True).stdout)

    py = backtest(_python_prices(data), pd.Timestamp(start), 5_000_000, monthly=monthly,
                  strategies=ALL_STRATEGIES, taxable=taxable)
    assert set(js) == set(py)
    for k, r in py.items():
        j = js[k]
        assert [d for d, _ in j["nav"]] == [d.strftime("%Y-%m-%d") for d in r.nav.index]
        # Python 版は毎日の評価額を銭（0.01 円）で丸めて持つ
        np.testing.assert_allclose([v for _, v in j["nav"]], r.nav.values, rtol=0, atol=0.006)
        assert len(j["trades"]) == r.trades
        assert j["realized"] == pytest.approx(r.realized, rel=1e-9, abs=1e-6)
        assert j["fees"] == pytest.approx(r.fees, rel=1e-9)
        assert j["tax"] == pytest.approx(r.tax_paid, rel=1e-9, abs=1e-6)
        if not taxable:
            assert r.tax_paid == 0
        if monthly:
            assert j["xirr"] == pytest.approx(r.money_weighted(), abs=1e-6)
    # 戦略が実際に売買していること（何もしないまま一致しているのではない）
    assert len(js["trend"]["trades"]) > 5 and len(js["dual_momentum"]["trades"]) >= 1
    for k in ("risk_parity", "momentum_top3", "vol_target", "sell_in_may", "ivy_timing"):
        assert len(js[k]["trades"]) > 3, k
    if taxable:
        assert py["trend"].tax_paid > 0       # 実際に課税が起きていること


def test_real_data_file_matches_python_if_present():
    """web/simdata.json（実データ）でも、2009-07 からの一括投資の最終額が一致する。"""
    f = ROOT / "web" / "simdata.json"
    if not f.exists():
        pytest.skip("web/simdata.json が無い")
    data = json.loads(f.read_text(encoding="utf-8"))
    js = json.loads(subprocess.run(
        ["node", str(ROOT / "tests" / "sim_parity_runner.js"), str(f), "2009-07-01", "5000000", "0", "1"],
        capture_output=True, text=True, check=True).stdout)
    py = backtest(_python_prices(data), pd.Timestamp("2009-07-01"), 5_000_000,
                  strategies=ALL_STRATEGIES, taxable=True)
    for k, r in py.items():
        assert js[k]["nav"][-1][1] == pytest.approx(float(r.nav.iloc[-1]), rel=1e-9)
        assert len(js[k]["trades"]) == r.trades
