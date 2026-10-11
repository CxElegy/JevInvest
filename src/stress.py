"""過去の急落を今の配分に当てはめる（ストレステスト）。

    python -m src.cli stress                 # 保有があれば保有、無ければ目標配分で
    python -m src.cli stress --target        # 目標配分で
    python -m src.cli stress --bond-proxy JPY0   # 債券を国内債券（値動き 0）とみなす

場面は有名な出来事の日付で固定した（結果を見て選んだ期間ではない）。
各資産クラスは代表的な指数 ETF で近似し、円建てに直す:

    developed_equity  SPY 60% + EFA 40%（先進国株。VT は 2008 年以前が無いため）
    us_equity         QQQ（米国大型グロース。個別株の代わり）
    jp_equity         EWJ × USDJPY（日本株を円建てに戻したもの）
    bond              AGG（米国債券・為替ヘッジなし）。--bond-proxy JPY0 で値動き 0 の円資産
    cash              その通貨のまま（外貨現金は為替だけ動く）

損失は「値動き」と「為替」の 2 つに分けて出す。円高は外貨建て資産を円で目減りさせる。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

FX = "USDJPY=X"

EPISODES = [
    ("2008 リーマン・ショック", "2007-10-09", "2009-03-09", "米国株の天井から底まで。円高も同時に進んだ"),
    ("2011 超円高", "2011-04-29", "2011-10-31", "震災後。1 ドル 75 円台の最高値"),
    ("2020 コロナ", "2020-02-19", "2020-03-23", "1 か月で株が 3 割下落"),
    ("2022 利上げ", "2022-01-03", "2022-10-12", "株と債券が同時に下落。円安が円建ての損失を和らげた"),
    ("2024-07 円キャリー巻き戻し", "2024-07-10", "2024-08-05", "円高と日本株急落が同時に起きた"),
]

# 資産クラス → [(代用 ETF, 比率)]。通貨は ETF の上場地で決まる
PROXIES: dict[str, list[tuple[str, float]]] = {
    "developed_equity": [("SPY", 0.6), ("EFA", 0.4)],
    "us_equity": [("QQQ", 1.0)],
    "jp_equity": [("EWJ", 1.0)],
    "bond": [("AGG", 1.0)],
}

YEN_CLASSES = {"jp_equity"}

SERIES = sorted({s for v in PROXIES.values() for s, _ in v} | {FX})


@dataclass
class Exposure:
    asset_class: str
    currency: str      # 現金のときの通貨（それ以外は代用 ETF で決まる）
    weight: float      # 全体に対する比率（0〜1）


@dataclass
class ScenarioResult:
    name: str
    start: str
    end: str
    note: str
    total: float                       # 円建ての損益率
    price_part: float                  # 値動きの寄与
    fx_part: float                     # 為替の寄与（交差項込み）
    by_class: dict[str, float]         # クラスごとの寄与（全体比）
    fx_move: float                     # USDJPY の変化率


def _at(s: pd.Series, d: str) -> float:
    """その日以前で最新の値。休場日を指定しても前の営業日で取る。"""
    v = s.loc[: pd.Timestamp(d)].dropna()
    if v.empty:
        raise ValueError(f"{d} 以前のデータがありません")
    return float(v.iloc[-1])


def class_return(cls: str, start: str, end: str, series: dict[str, pd.Series],
                 bond_proxy: str = "AGG") -> tuple[float, float]:
    """(現地通貨での騰落率, 円建て騰落率)。"""
    fx_r = _at(series[FX], end) / _at(series[FX], start) - 1
    if cls == "bond" and bond_proxy == "JPY0":
        return 0.0, 0.0
    legs = PROXIES.get(cls)
    if legs is None:
        raise KeyError(cls)
    usd = sum(w * (_at(series[s], end) / _at(series[s], start) - 1) for s, w in legs)
    jpy = (1 + usd) * (1 + fx_r) - 1      # 代用はすべて米国上場の USD 建て ETF
    if cls in YEN_CLASSES:
        # 日本株は円の資産。EWJ のドル建て騰落には円相場が入っているので、
        # 円に戻した値がそのまま「値動き」で、為替の寄与は無い
        return jpy, jpy
    return usd, jpy


def replay(exposures: list[Exposure], series: dict[str, pd.Series],
           bond_proxy: str = "AGG") -> list[ScenarioResult]:
    out = []
    for name, start, end, note in EPISODES:
        fx_r = _at(series[FX], end) / _at(series[FX], start) - 1
        total = price = 0.0
        by_class: dict[str, float] = {}
        for e in exposures:
            if e.asset_class == "cash":
                local, jpy = 0.0, (fx_r if e.currency == "USD" else 0.0)
            else:
                local, jpy = class_return(e.asset_class, start, end, series, bond_proxy)
            by_class[e.asset_class] = by_class.get(e.asset_class, 0.0) + e.weight * jpy
            total += e.weight * jpy
            price += e.weight * local
        out.append(ScenarioResult(name, start, end, note, total, price, total - price,
                                  by_class, fx_r))
    return out


def usd_share(exposures: list[Exposure], bond_proxy: str = "AGG") -> float:
    """ドル建て（円高で目減りする）部分の比率。"""
    total = 0.0
    for e in exposures:
        if e.asset_class == "cash":
            total += e.weight if e.currency == "USD" else 0.0
        elif e.asset_class in YEN_CLASSES or (e.asset_class == "bond" and bond_proxy == "JPY0"):
            continue
        else:
            total += e.weight
    return total
