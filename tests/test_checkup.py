"""計画の健康診断（src/checkup.py）。ネットワーク不要。"""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src import checkup
from src.sim import Prices

ROOT = Path(__file__).resolve().parents[1]


def _prices():
    idx = pd.bdate_range("2008-01-01", "2012-12-31")
    n = len(idx)
    t = np.arange(n)
    # 2008 年後半に半値まで下げて戻る株、ゆっくり上がる債券
    eq = 100 * np.where((idx >= "2008-09-01") & (idx <= "2009-03-01"), 0.5, 1.0) * 1.0002 ** t
    s = lambda v: pd.Series(v, index=idx, dtype=float)  # noqa: E731
    series = {"VT": s(eq), "QQQ": s(eq * 1.1), "AGG": s(100 * 1.0001 ** t), "GLD": s(100 + 0 * t)}
    return Prices(series, s(np.full(n, 100.0)))


def test_to_weights_maps_classes_and_keeps_cash():
    w, _ = checkup.to_weights({"developed_equity": 60, "bond": 30, "cash": 10})
    assert w == pytest.approx({"VT": 0.6, "AGG": 0.3})


def test_to_weights_rejects_unknown_class():
    with pytest.raises(ValueError, match="crypto"):
        checkup.to_weights({"developed_equity": 90, "crypto": 10})


def test_nisa_fill_years():
    assert checkup.nisa_fill_years(1_000_000, 200_000) == pytest.approx(85 / 12)
    assert checkup.nisa_fill_years(0, 300_000) == 5.0          # 年 360 万ちょうど
    assert checkup.nisa_fill_years(20_000_000, 0) is None       # 一括でも年枠に縛られる
    assert checkup.nisa_fill_years(0, 0) is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node が無い")
def test_nisa_fill_matches_browser():
    cases = [[1e6, 2e5], [0, 5e4], [2e7, 0], [5e6, 3e5], [0, 3e5]]
    js = ("const E=require(process.argv[1]);const n={annual:3.6e6,lifetime:1.8e7};"
          f"console.log(JSON.stringify({json.dumps(cases)}.map(([a,b])=>E.nisaFill(a,b,40,n).filledAt)))")
    out = json.loads(subprocess.run(["node", "-e", js, str(ROOT / "web" / "simengine.js")],
                                    capture_output=True, text=True, check=True).stdout)
    for (a, b), months in zip(cases, out):
        py = checkup.nisa_fill_years(a, b)
        assert (py is None and months is None) or py == pytest.approx(months / 12)


def test_run_flags_drawdown_and_foreign_share():
    r = checkup.run({"developed_equity": 80, "bond": 20}, 0, 100_000, tolerance=0.2, prices=_prices())
    kinds = {t for _, t, _ in r.findings}
    assert r.crisis_dd < -0.3
    assert "最悪時の下落が許容を超える" in kinds
    assert r.foreign == pytest.approx(0.8 * 0.95 + 0.2)
    assert any("外貨建て" in t for t in kinds)


def test_run_calm_mix_passes_tolerance():
    r = checkup.run({"bond": 50, "cash": 50}, 0, 100_000, tolerance=0.4, prices=_prices())
    assert r.findings[0][0] == "ok"
    assert r.foreign == pytest.approx(0.5)


def test_cli_checkup_prints_no_holdings_amounts(capsys):
    from src.cli import main
    main(["--holdings", str(ROOT / "config" / "holdings.example.yaml"), "checkup"])
    out = capsys.readouterr().out
    assert "2008 年金融危機の下落" in out and "外貨建て" in out
