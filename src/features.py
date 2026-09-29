"""指標計算。

Jev は計算が苦手だと公式が明記しているので、数値はすべてここで計算し切って
から state に載せる。Jev には「計算済みの値を見て判断する」ことだけをさせる。

すべての窓は後ろ向き（backward-looking）。中心化 rolling や shift(-1) は
使わない — 1 箇所でも未来を見たらバックテストは無意味になる。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # 損失ゼロ（一方的な上昇）は RSI 100 に寄せる
    return out.where(avg_loss != 0.0, 100.0)


def macd_hist(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    line = _ema(close, fast) - _ema(close, slow)
    return line - _ema(line, signal)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def _rolling_z(s: pd.Series, n: int) -> pd.Series:
    mu = s.rolling(n, min_periods=n).mean()
    sd = s.rolling(n, min_periods=n).std(ddof=0)
    return (s - mu) / sd.replace(0.0, np.nan)


def _slope_annual_pct(close: pd.Series, n: int = 20) -> pd.Series:
    """対数価格の線形回帰の傾きを年率 % に換算する。

    numpy の polyfit を rolling で回すのは遅いので、
    傾き = cov(x, y) / var(x) の閉形式で出す。x は 0..n-1 固定なので var(x) は定数。
    """
    logp = np.log(close)
    x_mean = (n - 1) / 2.0
    var_x = ((np.arange(n) - x_mean) ** 2).sum()

    def _slope(window: np.ndarray) -> float:
        y = window - window.mean()
        return float((y * (np.arange(n) - x_mean)).sum() / var_x)

    daily = logp.rolling(n, min_periods=n).apply(_slope, raw=True)
    return (np.exp(daily * TRADING_DAYS) - 1.0) * 100.0


def compute(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV から state に載せる指標を全部計算する。

    df は date 昇順の index、列は open/high/low/close/volume。
    戻り値は同じ index で、state_schema の OBSERVATION_FIELDS / REGIME_FIELDS
    に対応する列を持つ。
    """
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    out = pd.DataFrame(index=df.index)

    sma20, sma50, sma200 = _sma(close, 20), _sma(close, 50), _sma(close, 200)
    out["price_vs_sma20_pct"] = (close / sma20 - 1.0) * 100.0
    out["price_vs_sma50_pct"] = (close / sma50 - 1.0) * 100.0
    out["price_vs_sma200_pct"] = (close / sma200 - 1.0) * 100.0
    out["sma50_vs_sma200_pct"] = (sma50 / sma200 - 1.0) * 100.0

    out["rsi14"] = rsi(close, 14)
    out["macd_hist_z"] = _rolling_z(macd_hist(close), 60)

    atr14 = atr(high, low, close, 14)
    out["atr14_pct"] = atr14 / close * 100.0

    ret1 = close.pct_change()
    out["realized_vol_20d_annual_pct"] = (
        ret1.rolling(20, min_periods=20).std(ddof=0) * np.sqrt(TRADING_DAYS) * 100.0
    )

    bb_mid = sma20
    bb_sd = close.rolling(20, min_periods=20).std(ddof=0)
    bb_low, bb_high = bb_mid - 2 * bb_sd, bb_mid + 2 * bb_sd
    out["bb_position"] = ((close - bb_low) / (bb_high - bb_low).replace(0.0, np.nan)).clip(-1, 2)

    out["return_1d_pct"] = ret1 * 100.0
    out["return_5d_pct"] = close.pct_change(5) * 100.0
    out["return_20d_pct"] = close.pct_change(20) * 100.0
    out["return_60d_pct"] = close.pct_change(60) * 100.0

    high_252 = close.rolling(TRADING_DAYS, min_periods=20).max()
    low_252 = close.rolling(TRADING_DAYS, min_periods=20).min()
    out["drawdown_from_252d_high_pct"] = (close / high_252 - 1.0) * 100.0
    out["distance_to_252d_low_pct"] = (close / low_252 - 1.0) * 100.0

    out["volume_vs_avg20"] = vol / vol.rolling(20, min_periods=20).mean().replace(0.0, np.nan)

    # --- レジーム ---
    rv = out["realized_vol_20d_annual_pct"]
    out["vol_percentile_1y"] = rv.rolling(TRADING_DAYS, min_periods=60).rank(pct=True)
    out["trend_slope_20d_annual_pct"] = _slope_annual_pct(close, 20)
    out["up_days_ratio_20d"] = (ret1 > 0).rolling(20, min_periods=20).mean()

    return out
