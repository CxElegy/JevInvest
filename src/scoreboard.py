"""遊び枠（個別株など）の成績表。同じ日に同じ額を全世界株に入れていたら、と比べる。

自分で選んだ売買のキャッシュフローを、そのまま基準（既定 VT）に当てる:
  - 個別株を X 円買った日 → 基準を X 円買ったことにする
  - 個別株を売って Y 円受け取った日 → 基準を Y 円売ったことにする
  - 配当は実際の受取額を自分の側に足す（基準は配当込みの調整後終値で評価する）

これで「お金を入れた日・抜いた日」が両方で同じになり、差は銘柄選びとタイミングの腕だけになる
（Public Market Equivalent と同じ考え方）。基準側の残高がマイナスになるのは、
基準ならもっと少なかったはずの額を引き出したという意味で、そのまま負けとして数える。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd


@dataclass
class ShadowResult:
    benchmark: str
    invested: float = 0.0            # 個別株に入れた額の合計（手数料込み）
    withdrawn: float = 0.0           # 売却で受け取った額の合計（手数料引き後）
    dividends: float = 0.0
    shadow_units: float = 0.0        # 基準の口数（マイナスもありうる）
    shadow_value: float = 0.0
    actual_value: float = 0.0        # 個別株の今の評価額 + 受け取った配当
    flows: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    comparable: bool = True          # False なら差を出さない（片側だけの比較になるため）

    @property
    def edge(self) -> float:
        """正なら基準に勝っている（円）。"""
        return self.actual_value - self.shadow_value


def _price_on(series: pd.Series, d: date) -> float | None:
    s = series.loc[: pd.Timestamp(d)]
    return float(s.iloc[-1]) if len(s) else None


def shadow(transactions: list, symbols: set[str], bench_jpy: pd.Series,
           current_value: float, benchmark: str = "VT") -> ShadowResult:
    """transactions は src.transactions.Transaction のリスト。

    bench_jpy は基準の円建て価格（配当調整後終値 × 為替）。
    current_value は対象銘柄の今の評価額（基準通貨）。
    """
    r = ShadowResult(benchmark=benchmark)
    for t in transactions:
        if t.symbol not in symbols:
            continue
        if t.type == "dividend":
            r.dividends += t.amount * t.fx_rate
            continue
        if t.type not in ("buy", "sell"):
            continue
        # 台帳で切り詰め・飛ばした売却は、その数量だけ数える（無かった売却代金で勝たせない）
        qty = t.effective_quantity if t.type == "sell" else t.quantity
        if qty <= 0:
            continue
        fee = t.fee * (qty / t.quantity) if t.quantity else t.fee
        px = _price_on(bench_jpy, t.date)
        if px is None:
            # 片側だけ数えると比較が自分に有利に偏る。比較そのものを出さない
            r.warnings.append(f"{t.date}: {benchmark} の価格がその日以前に無いため比較できません")
            r.comparable = False
            continue
        if t.type == "buy":
            cash = qty * t.price * t.fx_rate + fee
            r.invested += cash
            r.shadow_units += cash / px
        else:
            cash = qty * t.price * t.fx_rate - fee
            r.withdrawn += cash
            r.shadow_units -= cash / px
        r.flows.append({"date": t.date.isoformat(), "symbol": t.symbol, "side": t.type,
                        "jpy": round(cash), "bench_px": round(px, 2)})

    last = float(bench_jpy.dropna().iloc[-1]) if len(bench_jpy.dropna()) else 0.0
    r.shadow_value = r.shadow_units * last
    r.actual_value = current_value + r.dividends
    if r.shadow_units < 0:
        r.warnings.append(f"基準側の残高がマイナスです。基準で持っていたら、引き出した額ほどは増えていなかったという意味です")
    return r
