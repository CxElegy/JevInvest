"""NISA の枠に入りきらない現金を、枠が空くまで待つか、課税口座で先に投資するか（web/wait_lab.json）。

    python tools/wait_lab.py

100 万円が NISA に入れられるのが k 年後（k = 1, 2, 3）だとして、開始月を 1 か月ずつずらしながら比べる:
  wait      k 年間は円の現金（日銀の政策金利を利息とみなす。預金の実際の金利はこれ以下のことが多い）
            → k 年後に NISA で投資し、H 年後まで持つ（非課税）
  move      今すぐ課税口座で投資 → k 年後に全部売って税を払い、残りを NISA へ → H 年後まで（非課税）
  stay      今すぐ課税口座で投資 → H 年後に全部売って税を払う（NISA は使わない）
H = 10 年。配分は「全世界株 1 本」と「目標配分（近似）」。円建て、売買コストなし、税は譲渡益に 20.315%
（損なら 0、他との相殺は見ない）。価格は web/simdata.json（tools/hedge_lab.py と同じ円建てリターン）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.hedge_lab import daily_jpy_returns, load, portfolio  # noqa: E402

OUT = ROOT / "web" / "wait_lab.json"
TAX = 0.20315
H = 10
MIXES = {"vt": ("全球股一只", {"VT": 1.0}),
         "target": ("目标配置（近似）", {"VT": 0.55, "QQQ": 0.20, "1348.T": 0.05, "BOND": 0.15})}


def monthly_index(nav: pd.Series) -> pd.Series:
    """各月最初の営業日の値（その日に入れて、その日に売るとみなす）。"""
    return nav.groupby([nav.index.year, nav.index.month]).head(1)


def compare(g: pd.Series, cash: pd.Series, k: int) -> list[dict]:
    """g: 月初の評価額（任意の単位）、cash: 月初の現金の累積（同じ月の並び）。1 円あたりの最終額を返す。"""
    rows = []
    n = len(g)
    for i in range(n - H * 12):
        s, m, e = i, i + k * 12, i + H * 12
        grow = lambda a, b: g.iloc[b] / g.iloc[a]  # noqa: E731
        wait = cash.iloc[m] / cash.iloc[s] * grow(m, e)
        gk = grow(s, m)
        move = (gk - TAX * max(gk - 1, 0)) * grow(m, e)
        ge = grow(s, e)
        stay = ge - TAX * max(ge - 1, 0)
        rows.append({"start": str(g.index[s].date()), "wait": wait, "move": move, "stay": stay})
    return rows


def main() -> None:
    px, fx = load()
    r = daily_jpy_returns(px, fx, basis=0.003)
    cash_daily = (1 + r["yen_cash"]).cumprod()
    out = {"generated_to": str(px.index[-1].date()), "H": H, "mixes": {k: v[0] for k, v in MIXES.items()}, "results": {}}
    for mk, (_, w) in MIXES.items():
        start = "2009-06-01" if "1348.T" in w else "2008-07-01"
        nav = monthly_index(portfolio(r, w, "unhedged", start, None, crisis_proxy=False))
        cash = cash_daily.reindex(nav.index)
        out["results"][mk] = {}
        for k in (1, 2, 3):
            rows = compare(nav, cash, k)
            out["results"][mk][str(k)] = rows
            n = len(rows)
            mw = sum(x["move"] > x["wait"] for x in rows)
            sw = sum(x["stay"] > x["wait"] for x in rows)
            avg = lambda key: sum(x[key] for x in rows) / n  # noqa: E731
            worst = min(rows, key=lambda x: x["move"] / x["wait"])
            print(f"{mk:<7} k={k}: 開始 {rows[0]['start']}〜{rows[-1]['start']}（{n} 通り）"
                  f" 先に課税口座→移す が待つに勝つ {mw}/{n}、課税口座のまま {sw}/{n}；"
                  f"100 万円あたり 待つ {avg('wait') * 100:.0f} 万 / 移す {avg('move') * 100:.0f} 万 / そのまま {avg('stay') * 100:.0f} 万；"
                  f"移すの最悪 {worst['start']} {worst['move'] / worst['wait'] - 1:+.1%}")
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
