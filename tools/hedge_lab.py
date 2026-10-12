"""債券の部分を為替ヘッジするか（web/hedge_lab.json）。

    python tools/hedge_lab.py

債券の部分を 3 通りに置き換えて、円で見た成績を比べる:
  unhedged  米国債券 AGG をそのまま（ドル建て。円高で目減りする）
  hedged    AGG を為替ヘッジした場合（合成。下の式）
  yen_cash  円の現金（日銀の政策金利を利息とみなす）

ヘッジ後の日次リターン（円）= AGG のドル建てリターン − (米国の短期金利 − 日本の短期金利) − ベーシス
  米国の短期金利: BIL（米国短期国債 ETF）の日次リターン（分配込み）
  日本の短期金利: 日銀の政策金利（下の BOJ_RATE。日銀の公表文の適用日で切り替え。2026-10 時点で 1.25%）
  ベーシス      : 円投資家がドルをヘッジするときの上乗せコスト。既定 年 0.3%（0 と 0.6 でも出す）
実際のヘッジ付き投信は信託報酬も違うので、これは「ヘッジの効果の目安」。毎月初に目標比率へ戻す・売買コストなし。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "web" / "simdata.json"
OUT = ROOT / "web" / "hedge_lab.json"

# 日銀の政策金利（年率）。適用日 → 金利。マイナス金利期間（2016-02〜2024-03）は -0.1%。
# 2025-12 以降は日銀の公表文で確認（2025-12-19・2026-06-16・2026-09-18 の「金融市場調節方針の変更について」）
BOJ_RATE = [("2008-01-01", 0.005), ("2008-10-31", 0.003), ("2008-12-19", 0.001), ("2010-10-05", 0.0005),
            ("2016-02-16", -0.001), ("2024-03-19", 0.0005), ("2024-07-31", 0.0025), ("2025-01-24", 0.005),
            ("2025-12-22", 0.0075), ("2026-06-17", 0.01), ("2026-09-24", 0.0125)]
MIXES = {
    "target": ("目标配置（债券 15%）", {"VT": 0.55, "QQQ": 0.20, "1348.T": 0.05, "BOND": 0.15}),
    "s60": ("股 6 债 4", {"VT": 0.60, "BOND": 0.40}),
}
VARIANTS = {"unhedged": "美元债（不对冲）", "hedged": "美元债（对冲汇率）", "yen_cash": "日元现金"}
PERIODS = {"crisis": ("2008-07-01", "2009-06-30", "2008 年金融危机"), "y2011": ("2010-04-01", "2012-03-30", "2010–2011 日元急升"),
           "covid": ("2020-01-02", "2020-06-30", "2020 年新冠"), "y2022": ("2022-01-03", "2022-12-30", "2022 年加息"),
           "full": ("2009-07-01", None, "2009 年至今")}


def load():
    d = json.loads(DATA.read_text(encoding="utf-8"))
    idx = pd.to_datetime(d["dates"])
    px = pd.DataFrame({s: pd.Series(v, index=idx, dtype=float) for s, v in d["adj"].items()})
    fx = pd.Series(d["fx"], index=idx, dtype=float)
    return px, fx


def daily_jpy_returns(px: pd.DataFrame, fx: pd.Series, basis: float) -> pd.DataFrame:
    """各資産の円建て日次リターン。休場日は前日の値を使う（その日のリターン 0）。"""
    p = px.ffill()
    f = fx.ffill()
    r = pd.DataFrame(index=p.index)
    for s in ("VT", "QQQ", "AGG", "BIL"):
        r[s] = (p[s] * f).pct_change()
    r["1348.T"] = p["1348.T"].pct_change()
    usd_bond = p["AGG"].pct_change()
    usd_cash = p["BIL"].pct_change()
    days = r.index.to_series().diff().dt.days.fillna(1)
    boj = pd.Series(np.nan, index=r.index)
    for d0, v in BOJ_RATE:
        boj[r.index >= pd.Timestamp(d0)] = v
    jpy_cash = (1 + boj) ** (days / 365) - 1
    r["unhedged"] = r["AGG"]
    r["hedged"] = usd_bond - usd_cash + jpy_cash - basis * days / 365
    r["yen_cash"] = jpy_cash
    return r


def portfolio(r: pd.DataFrame, weights: dict[str, float], variant: str, start: str, end: str | None,
              crisis_proxy: bool) -> pd.Series:
    """毎月初に目標比率へ戻すポートフォリオの円建て評価額（1 から）。"""
    w = {(variant if s == "BOND" else s): v for s, v in weights.items()}
    if crisis_proxy and "1348.T" in w:          # 日本株のデータが無い期間は全世界株で代用
        w["VT"] = w.get("VT", 0) + w.pop("1348.T")
    rest = 1 - sum(w.values())                  # 残りは円の現金（利息 0）
    sub = r.loc[start:end] if end else r.loc[start:]
    sub = sub.iloc[1:]
    nav, out = 1.0, []
    hold = dict(w)
    month = None
    for day, row in sub.iterrows():
        if day.month != month:
            month = day.month
            hold = {s: nav * v for s, v in w.items()}
            cash = nav * rest
        hold = {s: v * (1 + (row[s] if pd.notna(row[s]) else 0.0)) for s, v in hold.items()}
        nav = sum(hold.values()) + cash
        out.append((day, nav))
    return pd.Series(dict(out))


def stats(nav: pd.Series) -> dict:
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    total = float(nav.iloc[-1] / 1.0 - 1)
    return {"total": total, "cagr": float(nav.iloc[-1] ** (1 / years) - 1) if years >= 1 else None,
            "max_dd": float((nav / nav.cummax() - 1).min())}


def main() -> None:
    px, fx = load()
    out = {"generated_to": str(px.index[-1].date()), "variants": VARIANTS, "periods": {k: v[2] for k, v in PERIODS.items()},
           "mixes": {k: v[0] for k, v in MIXES.items()}, "results": {}, "sleeve": {}}
    for basis in (0.0, 0.003, 0.006):
        r = daily_jpy_returns(px, fx, basis)
        key = f"{basis:.3f}"
        out["results"][key] = {}
        for mk, (_, w) in MIXES.items():
            out["results"][key][mk] = {}
            for pk, (s0, s1, _) in PERIODS.items():
                out["results"][key][mk][pk] = {v: stats(portfolio(r, w, v, s0, s1, crisis_proxy=pk == "crisis"))
                                               for v in VARIANTS}
        # 債券の部分だけの性質: 円で見た全世界株が大きく下げた月に、債券の部分がどう動いたか
        m = (1 + r[["VT", "unhedged", "hedged", "yen_cash"]].loc["2008-07-01":]).resample("ME").prod() - 1
        bad = m["VT"] < -0.05
        out["sleeve"][key] = {
            v: {"corr_vt": float(m[v].corr(m["VT"])), "avg_in_bad_months": float(m.loc[bad, v].mean()),
                "vol": float(m[v].std() * np.sqrt(12)),
                "cagr": float((1 + m[v]).prod() ** (12 / len(m)) - 1)} for v in VARIANTS}
        out["sleeve"][key]["_bad_months"] = int(bad.sum())
        out["sleeve"][key]["_vt_in_bad"] = float(m.loc[bad, "VT"].mean())
        print(f"basis {basis:.1%}")
        for mk in MIXES:
            for pk in PERIODS:
                row = out["results"][key][mk][pk]
                print(f"  {mk:<7}{pk:<9}" + "  ".join(f"{v}: {row[v]['total'] * 100:+6.1f}% dd {row[v]['max_dd'] * 100:5.1f}%"
                                                   for v in VARIANTS))
        print("  sleeve:", {v: {k: round(x, 3) for k, x in d.items()} for v, d in out["sleeve"][key].items() if not v.startswith("_")},
              "bad months", out["sleeve"][key]["_bad_months"])
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"→ {OUT}")


if __name__ == "__main__":
    sys.exit(main())
