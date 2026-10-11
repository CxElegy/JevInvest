"""全戦略の成績表（web/strategy_lab.json）を作る。シミュレーターの「策略排行榜」で使う。

    python tools/strategy_lab.py

データは web/simdata.json（ブラウザ版と同じ実データ）。規則は src/sim.py（JS 版と一致を確認済み）。
出すもの:
  - 全期間（2009-07〜）: 一括投資の年率・変動率・最大下落・年ごとの騰落、課税口座での税と税引後
  - 積立（初期 100 万 + 毎月 20 万）: NISA と課税口座の XIRR
  - 開始年をずらした 10 年積立: 年ごとの XIRR と、全世界株 1 本に勝った回数
  - 前半（2009-07〜2017）と後半（2018〜）: 前半の順位が後半でも続くか（過剰適合の確認）
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.sim import ALL_STRATEGIES, UNIVERSE, Prices, backtest  # noqa: E402

DATA = ROOT / "web" / "simdata.json"
OUT = ROOT / "web" / "strategy_lab.json"
START = "2009-07-01"
SPLIT = "2018-01-01"


def load_prices() -> tuple[Prices, str]:
    d = json.loads(DATA.read_text(encoding="utf-8"))
    idx = pd.to_datetime(d["dates"])
    series = {s: pd.Series(d["adj"][s], index=idx, dtype=float).dropna() for s in UNIVERSE}
    return Prices(series, pd.Series(d["fx"], index=idx, dtype=float)), d["dates"][-1]


def cagr(nav: pd.Series) -> float:
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    return float((nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1) if years > 0 else 0.0


def liq_cagr(r) -> float:
    """課税口座で最終日に全部売った後の年率（含み益にも課税）。"""
    nav = r.nav
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    return float((r.liquidated() / nav.iloc[0]) ** (1 / years) - 1)


def liq_xirr(r) -> float | None:
    from src.transactions import xirr

    return xirr(r.flows + [(r.nav.index[-1].date(), r.liquidated())])


def yearly(nav: pd.Series) -> dict[str, float]:
    ends = nav.groupby(nav.index.year).last()
    prev = nav.iloc[0]
    out = {}
    for y, v in ends.items():
        out[str(y)] = float(v / prev - 1)
        prev = v
    return out


def main() -> None:
    P, last = load_prices()
    start = pd.Timestamp(START)
    names = list(ALL_STRATEGIES)

    print("全期間（一括・NISA / 課税）…")
    lump = backtest(P, start, 5_000_000, strategies=ALL_STRATEGIES)
    lump_tax = backtest(P, start, 5_000_000, strategies=ALL_STRATEGIES, taxable=True)
    print("積立（NISA / 課税）…")
    dca = backtest(P, start, 1_000_000, strategies=ALL_STRATEGIES, monthly=200_000)
    dca_tax = backtest(P, start, 1_000_000, strategies=ALL_STRATEGIES, monthly=200_000, taxable=True)

    print("前半・後半…")
    first = backtest(P, start, 5_000_000, strategies=ALL_STRATEGIES, end=pd.Timestamp(SPLIT) - pd.Timedelta(days=1))
    second = backtest(P, pd.Timestamp(SPLIT), 5_000_000, strategies=ALL_STRATEGIES)

    rolling = {}
    end_all = lump["allcountry"].nav.index[-1]
    for y in range(2009, 2027):
        s0 = pd.Timestamp(f"{y}-07-01")
        e0 = s0 + pd.DateOffset(years=10)
        if e0 > end_all:
            break
        print(f"10 年積立 {y}〜{e0.year}…")
        rr = backtest(P, s0, 1_000_000, strategies=ALL_STRATEGIES, monthly=200_000, end=e0)
        rolling[str(y)] = {k: r.money_weighted() for k, r in rr.items()}

    out = {"generated_from": f"web/simdata.json（〜{last}）", "start": START, "split": SPLIT,
           "end": str(end_all.date()), "strategies": {}}
    for k in names:
        nav = lump[k].nav
        rets = nav.pct_change().dropna()
        vol = float(rets.std() * math.sqrt(252))
        dd = float((nav / nav.cummax() - 1).min())
        yr = yearly(nav)
        full_years = {y: v for y, v in yr.items() if y not in (str(nav.index[0].year), str(nav.index[-1].year))}
        wins = sum(1 for row in rolling.values() if row[k] > row["allcountry"] + 1e-12) if k != "allcountry" else None
        out["strategies"][k] = {
            "cagr": cagr(nav), "vol": vol, "max_dd": dd, "trades": lump[k].trades, "fees": lump[k].fees,
            "final": float(nav.iloc[-1]),
            "worst_year": min(full_years.values()), "best_year": max(full_years.values()),
            "yearly": yr,
            # 課税口座: 「売らずに持っている状態」と「最終日に全部売って含み益にも課税した後」の両方。
            # 比べるのは後者（買って持つだけの戦略は売らないので、前者だと税がかからないように見える）
            "taxable_final": float(lump_tax[k].nav.iloc[-1]), "taxable_cagr": cagr(lump_tax[k].nav),
            "taxable_liq_final": lump_tax[k].liquidated(), "taxable_liq_cagr": liq_cagr(lump_tax[k]),
            "tax_paid": lump_tax[k].tax_paid,
            "tax_total": lump_tax[k].tax_paid + max(lump_tax[k].unrealized, 0.0) * 0.20315,
            "dca_xirr": dca[k].money_weighted(), "dca_tax_xirr": dca_tax[k].money_weighted(),
            "dca_tax_liq_xirr": liq_xirr(dca_tax[k]),
            "dca_final": float(dca[k].nav.iloc[-1]), "dca_tax_final": float(dca_tax[k].nav.iloc[-1]),
            "dca_tax_liq_final": dca_tax[k].liquidated(),
            "first_cagr": cagr(first[k].nav), "second_cagr": cagr(second[k].nav),
            "rolling": {y: row[k] for y, row in rolling.items()},
            "rolling_wins": wins,
            "nav": [[str(d.date()), round(float(v))] for d, v in nav.resample("W-FRI").last().dropna().items()],
        }
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"→ {OUT.relative_to(ROOT)}（{OUT.stat().st_size / 1024:.0f} KB）")
    for k, v in sorted(out["strategies"].items(), key=lambda kv: -kv[1]["dca_xirr"]):
        print(f"  {k:<15} 積立 NISA {v['dca_xirr'] * 100:5.2f}% 課税(全部売却後) {v['dca_tax_liq_xirr'] * 100:5.2f}%  "
              f"前半 {v['first_cagr'] * 100:5.1f}% 後半 {v['second_cagr'] * 100:5.1f}%  10 年勝ち {v['rolling_wins']}")


if __name__ == "__main__":
    main()
