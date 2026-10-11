"""今月の積立をどこに入れるか（売らずに目標配分へ寄せる）。

    python -m src.cli nextbuy              # 台帳の contributions.monthly を振り分ける
    python -m src.cli nextbuy --pct-only   # 金額を出さず、比率だけ（貼り付け用）

目標比率でそのまま割るのではなく、目標より少ないクラスに多く入れる。配分の比べ方は
シミュレーターの「再平衡」と同じ（src/sim.py の buy_only: 不足額に比例して配る）。
web/rebalance_lab.json の検証では、積立中はこの「買い足すだけ」に「5 ポイント以上ずれたら売る」を
足した方法が、毎月売って戻すより 16 通りすべてで手取りが多かった。

independence: 購入可と記録された銘柄の無いクラスには配らない（その分は現金で留保）。
価格の取れない保有があるクラスがあれば、現在額が分からないので振り分けを出さない。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

BAND_PP = 5.0     # これ以上ずれたクラスは「売って戻す」を検討する目安（src/sim.py の DRIFT_BAND）


@dataclass
class Row:
    asset_class: str
    current: float          # 円
    target_pct: float
    amount: float = 0.0     # 今月入れる額
    parked: float = 0.0     # 購入可の銘柄が無いため留保する額
    symbols: list[str] = field(default_factory=list)
    status: str = ""


@dataclass
class Split:
    rows: list[Row]
    new_money: float
    total_before: float
    drift_before: float     # 目標からの最大のずれ（ポイント）
    drift_after: float      # この振り分けの後
    drift_naive: float      # 目標比率でそのまま割った場合の後
    over_band: list[tuple[str, float]] = field(default_factory=list)   # (クラス, 超過ポイント)
    notes: list[str] = field(default_factory=list)

    @property
    def parked(self) -> float:
        return sum(r.parked for r in self.rows)


def _drift(values: dict[str, float], target_pct: dict[str, float]) -> float:
    total = sum(values.values())
    if total <= 0:
        return 0.0
    return max(abs(values.get(c, 0.0) / total * 100 - target_pct.get(c, 0.0)) for c in set(values) | set(target_pct))


def split(current: dict[str, float], target_pct: dict[str, float], new_money: float,
          buyable: dict[str, list[str]], cash_classes: set[str] = frozenset({"cash"})) -> Split:
    """current: クラス → 現在の評価額（円）。target_pct: クラス → 目標 %（合計 100）。

    buyable: クラス → 購入可と記録された銘柄。現金のクラスは銘柄が無くても入れられる。
    """
    bad = [c for c, v in current.items() if v is None or (isinstance(v, float) and math.isnan(v))]
    if bad:
        raise ValueError(f"評価不能の保有があるクラス: {', '.join(sorted(bad))}。価格を取り直すか price_override を入れてから")
    if new_money < 0:
        raise ValueError("投入額が負です")
    tsum = sum(target_pct.values())
    tgt = {c: v * 100.0 / tsum for c, v in target_pct.items()} if tsum > 0 else {}
    classes = sorted(set(current) | set(tgt))
    total = sum(current.values())
    after_total = total + new_money
    gaps = {c: max(0.0, tgt.get(c, 0.0) / 100 * after_total - current.get(c, 0.0)) for c in classes}
    g = sum(gaps.values())
    if g >= new_money and g > 0:
        alloc = {c: gaps[c] * new_money / g for c in classes}
    else:
        rest = new_money - g
        alloc = {c: gaps[c] + rest * tgt.get(c, 0.0) / 100 for c in classes}

    rows = []
    for c in classes:
        r = Row(c, current.get(c, 0.0), tgt.get(c, 0.0), symbols=sorted(buyable.get(c, [])))
        if c in cash_classes or r.symbols:
            r.amount = alloc[c]
        else:
            r.parked = alloc[c]
            if alloc[c] > 0:
                r.status = "購入可の銘柄なし（現金で留保）"
        rows.append(r)

    after = {r.asset_class: r.current + r.amount + r.parked for r in rows}
    # 留保した分は現金のクラスに入ったものとして数える（実際に現金で持つので）
    cash_key = next((c for c in classes if c in cash_classes), None)
    parked = sum(r.parked for r in rows)
    if parked:
        for r in rows:
            if r.parked:
                after[r.asset_class] -= r.parked
        if cash_key:
            after[cash_key] += parked
        else:
            after["（留保した現金）"] = parked
    naive = {c: current.get(c, 0.0) + new_money * tgt.get(c, 0.0) / 100 for c in classes}
    before_pct = {c: (current.get(c, 0.0) / total * 100 if total > 0 else 0.0) for c in classes}
    over = sorted(((c, before_pct[c] - tgt.get(c, 0.0)) for c in classes
                   if before_pct[c] - tgt.get(c, 0.0) >= BAND_PP), key=lambda x: -x[1])
    out = Split(rows, new_money, total, _drift(current, tgt), _drift(after, tgt), _drift(naive, tgt), over)
    if parked:
        out.notes.append("購入可の銘柄が無いクラスの分は現金で留保した。DESC 確認と事前承認の結果を "
                         "config/compliance.yaml に記録すると、次回からそのクラスに回る")
    if g < new_money and total > 0:
        out.notes.append("今月の額で不足はすべて埋まる。残りは目標比率で配った")
    return out


def render(s: Split, pct_only: bool = False) -> str:
    L = []
    T = s.total_before + s.new_money
    L.append("── 今月の積立の振り分け（売らずに目標へ寄せる） ──")
    L.append("")
    head = "| 資産クラス | 現在% | 目標% | 今月の配分% | " + ("" if pct_only else "今月の額 | ") + "入れた後% | 購入可の銘柄 |"
    L.append(head)
    L.append("|" + "---|" * (head.count("|") - 1))
    for r in s.rows:
        share = (r.amount + r.parked) / s.new_money * 100 if s.new_money else 0.0
        now = r.current / s.total_before * 100 if s.total_before else 0.0
        held = s.parked if r.asset_class == "cash" else 0.0       # 留保した分は現金として持つ
        aft = (r.current + r.amount + held) / T * 100 if T else 0.0
        amt = "" if pct_only else (f"{r.amount:,.0f} 円" + (f"（留保 {r.parked:,.0f}）" if r.parked else "") + " | ")
        sym = ", ".join(r.symbols) or ("（現金）" if r.asset_class == "cash" else "なし")
        L.append(f"| {r.asset_class} | {now:.1f} | {r.target_pct:.1f} | {share:.1f} | {amt}{aft:.1f} | {sym}"
                 + (f" — {r.status}" if r.status else "") + " |")
    L.append("")
    L.append(f"目標からの最大のずれ: いま {s.drift_before:.1f} ポイント → この振り分けの後 {s.drift_after:.1f} ポイント"
             f"（目標比率でそのまま割ると {s.drift_naive:.1f} ポイント）")
    if s.over_band:
        L.append("")
        L.append(f"▲ {BAND_PP:.0f} ポイント以上、目標より多いクラス: "
                 + "、".join(f"{c}（+{d:.1f}）" for c, d in s.over_band))
        L.append("  積立だけでは戻りにくい。売って戻すかは、課税口座なら税（利益の 20.315%）、"
                 "NISA なら売った枠が戻るのは翌年（年間枠は戻らない）を考えて決める。"
                 "売る前に /before-trade で点検する")
    for n in s.notes:
        L.append(f"※ {n}")
    L.append("※ NISA のどの枠で買うかは python -m src.cli plan。ここはクラス単位の振り分けだけ")
    return "\n".join(L)
