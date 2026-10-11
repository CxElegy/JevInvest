"""計画の健康診断（シミュレーターの「我的计划体检」の Python 版）。

    python -m src.cli checkup                 # holdings.yaml の target_allocation で
    python -m src.cli checkup --tolerance 0.3 # 耐えられる下落を 30% として判定

資産クラスを代表的な ETF に置き換え（下の CLASS_PROXY）、実データ（web/simdata.json）で
過去の成績・2008 年の金融危機での下落・外貨の比率・NISA の枠が埋まる年数を出す。
計算は src/sim.py（ブラウザ版と一致を確認済み）。金額は台帳から読まない（比率と積立額だけ）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .sim import UNIVERSE, Prices, allcountry, backtest

ROOT = Path(__file__).resolve().parents[1]
SIMDATA = ROOT / "web" / "simdata.json"

# 資産クラス → 代わりの ETF（cash は円の現金 = リターン 0）
CLASS_PROXY = {
    "developed_equity": "VT",
    "us_equity": "QQQ",
    "jp_equity": "1348.T",
    "bond": "AGG",
    "gold": "GLD",
    "cash": None,
}
FOREIGN = {"VT": 0.95, "QQQ": 1.0, "SPY": 1.0, "AGG": 1.0, "GLD": 1.0}   # VT の約 5% は日本株
NISA_ANNUAL, NISA_LIFETIME = 3_600_000, 18_000_000


@dataclass
class Checkup:
    weights: dict[str, float]
    start: str
    cagr: float
    max_dd: float
    base_cagr: float
    base_dd: float
    crisis_dd: float
    base_crisis_dd: float
    foreign: float
    nisa_years: float | None
    findings: list[tuple[str, str, str]] = field(default_factory=list)   # (ok/warn/info, 見出し, 説明)
    notes: list[str] = field(default_factory=list)


def load_prices(path: Path = SIMDATA) -> Prices:
    d = json.loads(path.read_text(encoding="utf-8"))
    idx = pd.to_datetime(d["dates"])
    series = {s: pd.Series(d["adj"][s], index=idx, dtype=float).dropna() for s in UNIVERSE if s in d["adj"]}
    return Prices(series, pd.Series(d["fx"], index=idx, dtype=float))


def to_weights(target: dict[str, float]) -> tuple[dict[str, float], list[str]]:
    """target_allocation（% の合計 100）→ 代用 ETF のウェイト。対応の無いクラスは止める。"""
    total = sum(target.values())
    unknown = [c for c in target if c not in CLASS_PROXY]
    if unknown:
        raise ValueError(f"代用の ETF が決まっていない資産クラス: {', '.join(unknown)}（src/checkup.py の CLASS_PROXY）")
    w: dict[str, float] = {}
    for c, pct in target.items():
        sym = CLASS_PROXY[c]
        if sym and pct > 0:
            w[sym] = w.get(sym, 0.0) + pct / total
    notes = []
    if "us_equity" in target:
        notes.append("us_equity（個別株）は QQQ（ナスダック 100）で近似。実際の数銘柄はもっと振れる")
    return w, notes


def nisa_fill_years(initial: float, monthly: float, years: int = 40) -> float | None:
    """web/simengine.js の nisaFill と同じ規則（年 360 万・生涯 1,800 万、初期の一括も年枠に縛られる）。"""
    used = year_used = 0.0

    def put(amt: float) -> None:
        nonlocal used, year_used
        room = max(0.0, min(NISA_ANNUAL - year_used, NISA_LIFETIME - used))
        to_n = min(amt, room)
        used += to_n
        year_used += to_n

    put(initial)
    if used >= NISA_LIFETIME:
        return 0.0
    for t in range(years * 12):
        if (t + 1) % 12 == 0:
            year_used = 0.0
        put(monthly)
        if used >= NISA_LIFETIME:
            return (t + 1) / 12
    return None


def run(target: dict[str, float], initial: float, monthly: float, tolerance: float,
        prices: Prices | None = None) -> Checkup:
    P = prices or load_prices()
    w, notes = to_weights(target)
    fixed = lambda h, d=None: dict(w)  # noqa: E731
    # 過去: 使う銘柄すべてに価格がある最初の日から
    start = max([P.series[s].index[0] for s in list(w) + ["VT"]] + [pd.Timestamp("2008-07-01")])
    res = backtest(P, start, 5_000_000, strategies={"mine": (fixed, "monthly"), "base": (allcountry, "once")})
    st, sb = res["mine"].stats(), res["base"].stats()
    # 2008 年: 日本株はデータが無いので全世界株で代用
    w08: dict[str, float] = {}
    for s, v in w.items():
        k = "VT" if s == "1348.T" else s
        w08[k] = w08.get(k, 0.0) + v
    if "1348.T" in w:
        notes.append("2008 年の期間は日本株のデータが無いため、その部分を全世界株で代用")
    c = backtest(P, pd.Timestamp("2008-07-01"), 5_000_000, end=pd.Timestamp("2009-06-30"),
                 strategies={"m": (lambda h, d=None: dict(w08), "monthly"), "b": (allcountry, "once")})
    dd = lambda r: float((r.nav / r.nav.cummax() - 1).min())  # noqa: E731
    foreign = sum(v * FOREIGN.get(s, 0.0) for s, v in w.items())
    out = Checkup(weights=w, start=str(start.date()), cagr=st["cagr"], max_dd=st["max_dd"],
                  base_cagr=sb["cagr"], base_dd=sb["max_dd"], crisis_dd=dd(c["m"]), base_crisis_dd=dd(c["b"]),
                  foreign=foreign, nisa_years=nisa_fill_years(initial, monthly), notes=notes)
    worst = min(out.max_dd, out.crisis_dd)
    if -worst <= tolerance:
        out.findings.append(("ok", "下落は耐えられる範囲", f"過去の最大下落 {worst * 100:.0f}%（許容 {tolerance * 100:.0f}%）"))
    else:
        out.findings.append(("warn", "最悪時の下落が許容を超える",
                             f"過去の最大下落 {worst * 100:.0f}%（許容 {tolerance * 100:.0f}%）。株の比率を下げるか円の現金を増やす"))
    if foreign > 0.7:
        out.findings.append(("warn", f"外貨建てが約 {foreign * 100:.0f}%", "円高で目減りする（2008〜2011 年、1 ドル 106 円 → 76 円で約 30% 減）"))
    us = w.get("QQQ", 0.0)
    if us > 0.05:
        out.findings.append(("warn", f"個別株の部分が {us * 100:.0f}%", "Bogle の「遊び枠」の目安 5% を超える。1 銘柄ごとに DESC と事前承認が必要"))
    if out.cagr < out.base_cagr - 0.005:
        out.findings.append(("info", f"年率は全世界株 1 本より {(out.base_cagr - out.cagr) * 100:.1f} ポイント低い",
                             f"代わりに最大下落が小さい（{out.max_dd * 100:.0f}% 対 {out.base_dd * 100:.0f}%）"))
    if monthly * 12 > NISA_ANNUAL or initial + monthly * 11 > NISA_ANNUAL:
        out.findings.append(("warn", "NISA の年間枠を超える投入", "超えた分は課税口座になる"))
    return out
