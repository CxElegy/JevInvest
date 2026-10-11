"""再バランスの方法を実データで比べる（web/rebalance_lab.json）。

    python tools/rebalance_lab.py

同じ配分を、戻し方だけ変えて回す:
  monthly  毎月、売って目標に戻す
  yearly   年 1 回だけ売って戻す（積立の月は買い足すだけ）
  band     どれかが 5 ポイント以上ずれたときだけ売って戻す（それ以外は買い足すだけ）
  cashflow 一度も売らない。積立の新しいお金で、足りない資産を買い足すだけ
           （一括投資で積立が無いときは「買ったまま放置」と同じ）

課税口座（売るたびに 20.315%）と NISA（非課税）の両方で、最終日に全部売った後の手取り・
最大下落・目標からのずれ・税を出す。規則は src/sim.py（JS 版と一致を確認済み）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.sim import backtest  # noqa: E402
from tools.strategy_lab import load_prices  # noqa: E402

OUT = ROOT / "web" / "rebalance_lab.json"
POLICIES = ["monthly", "yearly", "band", "cashflow"]
MIXES = {
    "target": ("目标配分（近似）", {"VT": 0.55, "QQQ": 0.20, "1348.T": 0.05, "AGG": 0.15}),
    "s60": ("股 6 债 4", {"VT": 0.6, "AGG": 0.4}),
    "s80": ("股 8 债 2", {"VT": 0.8, "AGG": 0.2}),
    "permanent": ("永久组合", {"SPY": 0.25, "TLT": 0.25, "GLD": 0.25, "BIL": 0.25}),
}
SCENARIOS = {
    "lump": ("一次性 500 万", 5_000_000, 0),
    "dca": ("初始 100 万 + 每月 10 万", 1_000_000, 100_000),
}
BONDS = ("AGG", "TLT", "BIL")       # これ以外（株・金）を「株などリスク資産」として数える
STARTS = ["2009-07-01", "2015-01-01"]


def run_one(P, start, mix, capital, monthly, taxable):
    drift_log: dict[str, list[float]] = {k: [] for k in POLICIES}
    eq_log: dict[str, list[float]] = {k: [] for k in POLICIES}
    last_month: dict[str, int] = {}
    risky = {s for s in mix if s not in BONDS}

    def on_day(name, book, day):
        # 月末の状態を数える（月初は入金の現金が乗っていて、ずれが大きく見える）。
        # 同じ月の間は上書きし、月が変わったら確定する
        if not book.qty:                                   # 最初の約定前は数えない
            return
        m = day.year * 12 + day.month
        nav = book.value(P, day)
        w = {s: q * P.jpy(s, day) / nav for s, q in book.qty.items() if q and P.jpy(s, day) is not None}
        sample = (max(abs(w.get(s, 0.0) - mix.get(s, 0.0)) for s in set(mix) | set(w)),
                  sum(v for s, v in w.items() if s in risky))
        if last_month.get(name) == m:
            drift_log[name][-1], eq_log[name][-1] = sample
        else:
            last_month[name] = m
            drift_log[name].append(sample[0])
            eq_log[name].append(sample[1])

    fixed = lambda h, d=None: dict(mix)  # noqa: E731
    res = backtest(P, pd.Timestamp(start), capital, monthly=monthly, taxable=taxable, on_day=on_day,
                   strategies={k: (fixed, k) for k in POLICIES})
    out = {}
    for k, r in res.items():
        nav = r.nav
        paid = capital + monthly * (len(r.flows) - 1)
        final = r.liquidated() if taxable else float(nav.iloc[-1])
        from src.transactions import xirr

        out[k] = {
            "final": round(final),
            "paid": round(paid),
            "xirr": xirr(r.flows + [(nav.index[-1].date(), final)]),
            "max_dd": float((nav / nav.cummax() - 1).min()),
            "tax_during": round(r.tax_paid),
            "trades": r.trades,
            "drift_avg": sum(drift_log[k]) / len(drift_log[k]),
            "drift_max": max(drift_log[k]),
            "risky_end": eq_log[k][-1],
            "risky_max": max(eq_log[k]),
        }
    return out


def main() -> None:
    P, last = load_prices()
    out = {"generated_to": last, "policies": POLICIES, "mixes": {k: {"label": v[0], "weights": v[1]} for k, v in MIXES.items()},
           "scenarios": {k: v[0] for k, v in SCENARIOS.items()}, "results": []}
    for start in STARTS:
        for mk, (_, mix) in MIXES.items():
            for sk, (_, cap, mon) in SCENARIOS.items():
                for taxable in (True, False):
                    r = run_one(P, start, mix, cap, mon, taxable)
                    out["results"].append({"start": start, "mix": mk, "scenario": sk, "taxable": taxable, "by": r})
                    best = max(r, key=lambda k: r[k]["final"])
                    print(f"{start} {mk:<9} {sk:<4} {'課税' if taxable else 'NISA'}: "
                          + " | ".join(f"{k} {r[k]['final'] / 1e6:.2f}M dd{r[k]['max_dd'] * 100:.0f}% "
                                       f"ずれ{r[k]['drift_avg'] * 100:.1f}/{r[k]['drift_max'] * 100:.0f}" for k in POLICIES)
                          + f"  → {best}", flush=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
