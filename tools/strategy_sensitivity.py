"""戦略の「つまみ」を少し動かすと成績がどれだけ変わるか（web/strategy_sensitivity.json）。

    python tools/strategy_sensitivity.py

見るもの:
  - 移動平均の日数（S&P500 タイミング、5 資産トレンド）: 50〜300 日
  - デュアルモメンタムの比較期間: 3〜15 か月
  - 相対モメンタムで持つ数: 1〜5
  - ボラティリティ・ターゲットの目標: 年 8〜20%
それぞれ全期間・前半（2009-07〜2017）・後半（2018〜）の年率と最大下落を出し、
「前半でいちばん良かった設定を選んでいたら、後半は何位だったか」を記録する（過剰適合の確認）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from src.sim import MOM_ASSETS, TREND_ASSETS, _above_sma, _ret, _vals, _vol, backtest  # noqa: E402
from strategy_lab import SPLIT, START, cagr, load_prices  # noqa: E402

OUT = ROOT / "web" / "strategy_sensitivity.json"


def spy_sma(n):
    return lambda h, d=None: {"SPY": 1.0} if _above_sma(_vals(h, "SPY"), n) is not False else {"BIL": 1.0}


def trend_sma(n):
    def fn(h, d=None):
        return {s: 1.0 / len(TREND_ASSETS) for s in TREND_ASSETS if _above_sma(_vals(h, s), n)}
    return fn


def dual(n):
    def fn(h, d=None):
        r = {s: _ret(_vals(h, s), n) for s in ("SPY", "VEU", "BIL")}
        if any(v is None for v in r.values()):
            return {"AGG": 1.0}
        best = "SPY" if r["SPY"] >= r["VEU"] else "VEU"
        return {best: 1.0} if r[best] > r["BIL"] else {"AGG": 1.0}
    return fn


def mom_top(k):
    def fn(h, d=None):
        scores = []
        for s in MOM_ASSETS:
            a = _vals(h, s)
            rs = [_ret(a, x) for x in (21, 63, 126, 252)]
            if any(r is None for r in rs):
                continue
            scores.append((-(rs[0] + rs[1] + rs[2] + rs[3]) / 4, s))
        scores.sort()
        return {s: 1.0 / k for _, s in scores[:k] if _above_sma(_vals(h, s))}
    return fn


def vol_t(t):
    def fn(h, d=None):
        v = _vol(_vals(h, "VT"), 60)
        if v is None or v <= 0:
            return {"VT": 1.0}
        w = min(1.0, t / v)
        return {"VT": w, "BIL": 1.0 - w} if w < 1.0 else {"VT": 1.0}
    return fn


FAMILIES = {
    "spy_timing": ("S&P500 + N 日均线", "N 日", [(f"{n}", spy_sma(n)) for n in (50, 100, 150, 200, 250, 300)], "200"),
    "trend": ("5 资产 + N 日均线", "N 日", [(f"{n}", trend_sma(n)) for n in (50, 100, 150, 200, 250, 300)], "200"),
    "dual_momentum": ("双动量的比较期间", "N 个月", [(f"{m}", dual(m * 21)) for m in (3, 6, 9, 12, 15)], "12"),
    "momentum_top3": ("相对动量持有数", "前 N 个", [(f"{k}", mom_top(k)) for k in (1, 2, 3, 4, 5)], "3"),
    "vol_target": ("波动率目标", "年化 N%", [(f"{int(t * 100)}", vol_t(t)) for t in (0.08, 0.10, 0.12, 0.15, 0.20)], "12"),
}


def main() -> None:
    P, last = load_prices()
    start, split = pd.Timestamp(START), pd.Timestamp(SPLIT)
    base = {}
    for part, kw in (("full", {}), ("first", {"end": split - pd.Timedelta(days=1)})):
        r = backtest(P, start, 5_000_000, strategies={"allcountry": (lambda h, d=None: {"VT": 1.0}, "once")}, **kw)
        base[part] = cagr(r["allcountry"].nav)
    r = backtest(P, split, 5_000_000, strategies={"allcountry": (lambda h, d=None: {"VT": 1.0}, "once")})
    base["second"] = cagr(r["allcountry"].nav)

    out = {"start": START, "split": SPLIT, "end": last, "base": base, "families": {}}
    for key, (label, unit, variants, default) in FAMILIES.items():
        print(f"{label}…")
        strategies = {name: (fn, "monthly") for name, fn in variants}
        full = backtest(P, start, 5_000_000, strategies=strategies)
        first = backtest(P, start, 5_000_000, strategies=strategies, end=split - pd.Timedelta(days=1))
        second = backtest(P, split, 5_000_000, strategies=strategies)
        rows = []
        for name, _ in variants:
            nav = full[name].nav
            rows.append({"param": name, "cagr": cagr(nav), "max_dd": float((nav / nav.cummax() - 1).min()),
                         "trades": full[name].trades,
                         "first": cagr(first[name].nav), "second": cagr(second[name].nav)})
        best_first = max(rows, key=lambda x: x["first"])
        ranks2 = sorted(rows, key=lambda x: -x["second"])
        out["families"][key] = {
            "label": label, "unit": unit, "default": default, "rows": rows,
            "best_in_first": best_first["param"],
            "its_rank_in_second": 1 + [x["param"] for x in ranks2].index(best_first["param"]),
            "n": len(rows),
            "spread": max(x["cagr"] for x in rows) - min(x["cagr"] for x in rows),
        }
        f = out["families"][key]
        print(f"  前半ベスト {f['best_in_first']}{unit} → 後半 {f['its_rank_in_second']}/{f['n']} 位、"
              f"全期間の年率の幅 {f['spread'] * 100:.1f} ポイント")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
