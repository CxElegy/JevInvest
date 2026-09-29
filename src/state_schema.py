"""state の項目定義。

ここが唯一の正。`state.py`（実際の構築）と `tools/estimate_cost.py`（消費量見積り）
の両方がこのファイルを参照するので、項目を増やすとコスト見積りにも自動で反映される。

設計ルール:
  1. 絶対価格・実日付・銘柄名は入れない。
     - 学習データリーク対策（Jev が「その後どうなったか」を知っている可能性を潰す）
     - Jev は計算が苦手なので、水準ではなく「計算済みの相対値」を渡す
  2. 数値は全て Python 側で計算済みのものを渡す。Jev に算術をさせない。
  3. 項目を増やすほど毎リクエストのトークンが増える。安いとはいえ効く。
"""

from __future__ import annotations

# as_of 時点のスナップショット。全て「計算済みの相対値」。
OBSERVATION_FIELDS: tuple[str, ...] = (
    "price_vs_sma20_pct",
    "price_vs_sma50_pct",
    "price_vs_sma200_pct",
    "sma50_vs_sma200_pct",
    "rsi14",
    "macd_hist_z",
    "atr14_pct",
    "realized_vol_20d_annual_pct",
    "bb_position",
    "return_1d_pct",
    "return_5d_pct",
    "return_20d_pct",
    "return_60d_pct",
    "drawdown_from_252d_high_pct",
    "distance_to_252d_low_pct",
    "volume_vs_avg20",
)

REGIME_FIELDS: tuple[str, ...] = (
    "vol_percentile_1y",
    "trend_slope_20d_annual_pct",
    "up_days_ratio_20d",
)

POSITION_FIELDS: tuple[str, ...] = (
    "held",
    "weight_pct",
    "unrealized_pnl_pct",
    "bars_held",
)

PORTFOLIO_FIELDS: tuple[str, ...] = (
    "cash_weight_pct",
    "num_positions",
    "total_unrealized_pnl_pct",
    "drawdown_from_peak_pct",
)

# 直近の終値を「最終値 = 100」に正規化した系列の長さ。
# 絶対価格を渡さずに形だけ伝えるための項目。長くするとトークンが増える。
NORMALIZED_CLOSES_LEN = 20


def sample_state(anonymize: bool = True) -> dict:
    """消費量見積り用の代表的な state。

    実際の `build_state()` が出すものと同じ項目・同じ桁数感で作る。
    トークン数はキー名の長さと桁数でほぼ決まるので、これで十分な精度が出る。
    """
    state: dict = {
        "instrument": "INSTRUMENT_A" if anonymize else "MU",
        "asset_class": "equity",
        "bar_interval": "1d",
        "observation": {f: -12.34 for f in OBSERVATION_FIELDS},
        "regime": {f: 0.72 for f in REGIME_FIELDS},
        "normalized_closes": [round(98.0 + i * 0.11, 2) for i in range(NORMALIZED_CLOSES_LEN)],
        "position": {
            "held": True,
            "weight_pct": 12.0,
            "unrealized_pnl_pct": -3.14,
            "bars_held": 14,
        },
        "portfolio": {
            "cash_weight_pct": 22.5,
            "num_positions": 4,
            "total_unrealized_pnl_pct": 1.87,
            "drawdown_from_peak_pct": -5.42,
        },
    }
    if not anonymize:
        state["as_of"] = "2026-09-25"
    return state
