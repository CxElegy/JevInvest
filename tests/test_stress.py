"""ストレステスト。合成の価格だけで、ネットワークには出ない。"""

import pandas as pd
import pytest

from src.stress import EPISODES, FX, SERIES, Exposure, replay, usd_share


def _series(moves: dict[str, float], fx_move: float):
    """全場面で、開始日 → 終了日に moves のとおり動く系列を作る。"""
    days = sorted({pd.Timestamp(d) for _, s, e, _ in EPISODES for d in (s, e)})
    out = {}
    for sym in SERIES:
        m = fx_move if sym == FX else moves.get(sym, 0.0)
        vals, level = [], 100.0
        for d in days:
            # 開始日は 100、終了日は 100 × (1 + m)。場面が重ならないので交互に置ける
            is_end = any(pd.Timestamp(e) == d for _, _, e, _ in EPISODES)
            vals.append(100.0 * (1 + m) if is_end else 100.0)
        out[sym] = pd.Series(vals, index=days)
    return out


def test_usd_asset_splits_into_price_and_fx():
    s = _series({"QQQ": -0.30}, fx_move=-0.10)
    r = replay([Exposure("us_equity", "USD", 1.0)], s)[0]
    assert r.total == pytest.approx(0.7 * 0.9 - 1)
    assert r.price_part == pytest.approx(-0.30)
    assert r.fx_part == pytest.approx(r.total + 0.30)


def test_japan_equity_is_a_yen_asset():
    # EWJ がドル建てで -10%、円高 10%（USDJPY -10%）→ 円建ては -19%、為替の寄与は 0
    s = _series({"EWJ": -0.10}, fx_move=-0.10)
    r = replay([Exposure("jp_equity", "JPY", 1.0)], s)[0]
    assert r.total == pytest.approx(0.9 * 0.9 - 1)
    assert r.fx_part == pytest.approx(0.0)


def test_cash_and_bond_proxy_and_usd_share():
    s = _series({"AGG": 0.05}, fx_move=-0.20)
    ex = [Exposure("cash", "JPY", 0.5), Exposure("cash", "USD", 0.2), Exposure("bond", "JPY", 0.3)]
    r = replay(ex, s)[0]
    assert r.total == pytest.approx(0.2 * -0.20 + 0.3 * (1.05 * 0.8 - 1))
    r0 = replay(ex, s, bond_proxy="JPY0")[0]
    assert r0.total == pytest.approx(0.2 * -0.20)
    assert usd_share(ex) == pytest.approx(0.5)
    assert usd_share(ex, "JPY0") == pytest.approx(0.2)
