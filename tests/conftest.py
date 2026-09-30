"""テスト共通の部品。ネットワークには一切出ない（_download を差し替える）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.marketdata import MarketData


def make_bars(start: float, days: int = 300, drift: float = 0.0004, vol: float = 0.01,
              seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize() - pd.Timedelta(days=1),
                         periods=days)
    close = start * np.cumprod(1.0 + rng.normal(drift, vol, days))
    return pd.DataFrame(
        {
            "open": close, "high": close * 1.005, "low": close * 0.995,
            "close": close, "adj_close": close * 1.02, "volume": 1_000_000.0,
        },
        index=pd.DatetimeIndex(idx, name="date"),
    )


class FakeMarketData(MarketData):
    """yfinance の代わりに、用意した日足を返す。"""

    def __init__(self, db_path, data: dict[str, pd.DataFrame]):
        super().__init__(db_path)
        self.data = data
        self.calls: list[list[str]] = []

    def _download(self, symbols, start=None, period=None):
        self.calls.append(list(symbols))
        return {s: self.data[s].copy() for s in symbols if s in self.data}


@pytest.fixture
def market_data(tmp_path):
    data = {
        "VT": make_bars(120.0, seed=1),
        "7203.T": make_bars(2800.0, seed=2, vol=0.015),
        "USDJPY=X": make_bars(150.0, seed=3, vol=0.004),
    }
    md = FakeMarketData(tmp_path / "market.sqlite", data)
    yield md
    md.close()
