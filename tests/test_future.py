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


def test_partial_last_month_is_dropped(tmp_path):
    """データの最終日が月の途中なら、その月は使わない（途中までの騰落を 1 か月分とみなさない）。"""
    f = _data(tmp_path, 0.01, months=24)
    d = json.loads(f.read_text())
    out = subprocess.run(["node", "-e", f"""
const E=require({json.dumps(str(ROOT / 'web' / 'simengine.js'))});
const p=new E.Prices(JSON.parse(require('fs').readFileSync({json.dumps(str(f))},'utf8')));
const m=E.monthlyReturns(p,['1348.T']);console.log(JSON.stringify(m.months.slice(-1)));"""],
                         capture_output=True, text=True, check=True).stdout
    last_data_month = d["dates"][-1][:7]
    assert json.loads(out)[0] < last_data_month


def test_nisa_lifetime_cap_taxes_only_the_overflow(tmp_path):
    """毎月 1% 上がる資産。枠を超えた投入だけが課税口座に入り、その含み益にだけ課税される。"""
    f = _data(tmp_path, 0.01)
    r = json.loads(subprocess.run(["node", "-e", f"""
const E=require({json.dumps(str(ROOT / 'web' / 'simengine.js'))});
const p=new E.Prices(JSON.parse(require('fs').readFileSync({json.dumps(str(f))},'utf8')));
const r=E.bootstrap(p,{{weights:{{'1348.T':1}},initial:0,monthly:100000,years:10,paths:50,seed:1,haircut:0,nisa:{{annual:1200000,lifetime:3000000}}}});
console.log(JSON.stringify({{f:r.final.p50,a:r.afterTax.p50}}));"""], capture_output=True, text=True, check=True).stdout)
    # 手計算: 30 か月目までが NISA（月 10 万 × 30 = 300 万）、以降の 90 か月分が課税口座
    nv = tv = cost = 0.0
    for m in range(120):
        nv *= 1.01
        tv *= 1.01
        if m < 30:
            nv += 100_000
        else:
            tv += 100_000
            cost += 100_000
    expect = nv + tv - (tv - cost) * 0.20315
    assert r["a"] == pytest.approx(expect, rel=1e-9)
    assert r["f"] == pytest.approx(nv + tv, rel=1e-9)


def test_nisa_fill_matches_annual_and_lifetime_caps():
    out = subprocess.run(["node", "-e", f"""
const E=require({json.dumps(str(ROOT / 'web' / 'simengine.js'))});
const n=x=>JSON.stringify(x);
console.log(n([E.nisaFill(1000000,200000,40,{{annual:3600000,lifetime:18000000}}),
               E.nisaFill(10000000,300000,20,{{annual:3600000,lifetime:18000000}}),
               E.nisaFill(0,0,40,{{annual:3600000,lifetime:18000000}})]));"""],
                         capture_output=True, text=True, check=True).stdout
    a, b, c = json.loads(out)
    assert a["filledAt"] == 85 and a["over"] > 0           # 100 万 + 月 20 万 → 7 年 1 か月で埋まる
    assert b["firstYearOver"] == 10_000_000 + 300_000 * 11 - 3_600_000
    assert 4.5 < b["filledAt"] / 12 < 5.5                  # 初期の 1,000 万は年 360 万の枠に縛られる
    assert c["filledAt"] is None and c["over"] == 0
