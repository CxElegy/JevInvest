"""NISA の年間枠を年初にまとめて入れるか、12 か月に分けるか（web/frontload_lab.json）。

    python tools/frontload_lab.py

毎年、年 360 万円を
  lump     その年の最初の営業日に一度に入れる
  monthly  毎月最初の営業日に 30 万円ずつ
で入れ、その年の末の評価額を比べる。12 月に全額入った後は同じ資産を持つので、
その年の末の差（%）は何年持っても変わらない。
配分は「全世界株 1 本」と「目標配分（近似、毎月初に比率を戻す）」。円建て、売買コスト・税なし（NISA）。
価格は web/simdata.json（tools/hedge_lab.py と同じ円建てリターン）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.hedge_lab import daily_jpy_returns, load, portfolio  # noqa: E402

OUT = ROOT / "web" / "frontload_lab.json"
ANNUAL = 3_600_000
MIXES = {"vt": ("全球股一只", {"VT": 1.0}),
         "target": ("目标配置（近似）", {"VT": 0.55, "QQQ": 0.20, "1348.T": 0.05, "BOND": 0.15})}


def compare(nav: pd.Series, year: int, horizon_years: int) -> dict | None:
    days = nav.index[nav.index.year == year]
    end_year = year + horizon_years - 1
    ends = nav.index[nav.index.year == end_year]
    # その年の 1 月から 12 月まで、データがそろっている年だけ
    if len(days) == 0 or len(ends) == 0 or days[0].month != 1 or ends[-1].month != 12:
        return None
    end = ends[-1]
    firsts = pd.Series(days, index=days).groupby(days.month).first()
    lump = ANNUAL * nav[end] / nav[firsts.iloc[0]]
    monthly = sum(ANNUAL / 12 * nav[end] / nav[d] for d in firsts)
    # 年の途中で、一括のほうが分割（まだ入れていない分は現金）より一番下回った額（後悔の大きさ）
    def split_value(d):
        done = [x for x in firsts if x <= d]
        return sum(ANNUAL / 12 * nav[d] / nav[x] for x in done) + ANNUAL / 12 * (12 - len(done))
    worst_gap = min(ANNUAL * nav[d] / nav[firsts.iloc[0]] - split_value(d) for d in days)
    return {"year": year, "lump": lump, "monthly": monthly, "diff": lump / monthly - 1, "worst_gap": worst_gap}


def main() -> None:
    px, fx = load()
    r = daily_jpy_returns(px, fx, basis=0.003)
    out = {"generated_to": str(px.index[-1].date()), "annual": ANNUAL, "mixes": {k: v[0] for k, v in MIXES.items()}, "results": {}}
    for mk, (_, w) in MIXES.items():
        start = "2009-06-01" if "1348.T" in w else "2008-07-01"
        nav = portfolio(r, w, "unhedged", start, None, crisis_proxy=False)
        out["results"][mk] = {}
        for h in (1,):
            rows = [x for y in range(nav.index[0].year + 1, nav.index[-1].year + 1) if (x := compare(nav, y, h))]
            out["results"][mk][str(h)] = rows
            wins = sum(1 for x in rows if x["diff"] > 0)
            avg = sum(x["diff"] for x in rows) / len(rows)
            worst = min(rows, key=lambda x: x["diff"])
            print(f"{mk:<7} {h} 年: 年初一括の勝ち {wins}/{len(rows)}、平均 {avg:+.2%}、最悪 {worst['year']} {worst['diff']:+.2%}、"
                  f"途中の最大の差 {min(x['worst_gap'] for x in rows):,.0f} 円")
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
