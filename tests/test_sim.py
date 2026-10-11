"""ペーパートレードのエンジン。合成した価格だけで、ネットワークには出ない。"""

import pandas as pd
import pytest

from src.sim import Book, Costs, Prices, backtest, dual_momentum, trend, window_returns

ZERO = Costs(fee_rate=0.0, slippage=0.0, fx_spread=0.0, us_withholding=0.10)


def _px(days, values):
    return pd.Series(values, index=pd.bdate_range("2026-01-05", periods=days))


def _prices(vt, jp=None, fx=150.0, **kw):
    n = len(vt)
    series = {"VT": _px(n, vt)}
    if jp is not None:
        series["1348.T"] = _px(n, jp)
    return Prices(series, _px(n, [fx] * n), **kw)


def test_fill_uses_next_close_not_decision_close():
    p = _prices([100, 110, 120])
    b = Book("t", cash=1_500_000)
    d0, d1, d2 = p.calendar()
    b.decide(p, d0, {"VT": 1.0})
    assert b.pending and b.pending[0]["qty"] == 100   # 1,500,000 / (100 × 150)
    b.process_day(p, d1, ZERO)
    # 判断日の 100 ではなく翌日の 110 で約定する。110 では 100 株は買えないので減らす
    t = b.trades[0]
    assert t["date"] == str(d1.date()) and t["price"] == 110
    assert b.qty["VT"] == 90 and b.notes and "現金不足" in b.notes[0]


def test_costs_fx_spread_and_tax_estimate():
    p = _prices([100, 100, 100, 120])
    c = Costs(fee_rate=0.001, slippage=0.0, fx_spread=0.25)
    b = Book("t", cash=10_000_000)
    d = p.calendar()
    b.decide(p, d[0], {"VT": 0.5})
    b.process_day(p, d[1], c)
    unit = 100 * 150.25
    assert b.qty["VT"] == 333
    assert b.cash == pytest.approx(10_000_000 - 333 * unit * 1.001)
    b.decide(p, d[2], {})
    b.process_day(p, d[3], c)
    proceeds = 333 * 120 * 149.75
    assert b.realized == pytest.approx(proceeds * 0.999 - 333 * unit * 1.001)
    assert "VT" not in b.qty


def test_dividend_paid_on_ex_date_with_us_withholding():
    vt = [100] * 4
    p = _prices(vt)
    d = p.calendar()
    p.dividends = {"VT": pd.Series([1.0], index=[d[2]])}
    b = Book("t", cash=1_600_000)
    b.decide(p, d[0], {"VT": 1.0})
    b.process_day(p, d[1], ZERO)
    cash_before = b.cash
    b.process_day(p, d[2], ZERO)
    assert b.cash - cash_before == pytest.approx(106 * 1.0 * 150 * 0.9)


def test_jp_holiday_fills_on_next_jp_bar():
    idx = pd.bdate_range("2026-01-05", periods=3)
    p = Prices({"VT": pd.Series([100, 100, 100], index=idx),
                "1348.T": pd.Series([4000, 4100], index=[idx[0], idx[2]])},
               pd.Series([150] * 3, index=idx))
    b = Book("t", cash=1_000_000)
    b.decide(p, idx[0], {"1348.T": 0.5})
    b.process_day(p, idx[1], ZERO)     # 東証が休み
    assert b.pending and not b.trades
    b.process_day(p, idx[2], ZERO)
    assert b.trades[0]["price"] == 4100 and b.qty["1348.T"] == 125


def test_signals_ignore_trade_prices():
    up = pd.Series(range(1, 301), index=pd.bdate_range("2025-01-01", periods=300), dtype=float)
    down = up[::-1].copy()
    down.index = up.index
    hist = {s: up for s in ("VT", "QQQ", "1348.T", "AGG", "GLD")}
    hist["GLD"] = down
    w = trend(hist)
    assert set(w) == {"VT", "QQQ", "1348.T", "AGG"} and w["VT"] == pytest.approx(0.2)
    assert dual_momentum({"SPY": up, "VEU": down, "BIL": up * 0 + 1}) == {"SPY": 1.0}
    assert dual_momentum({"SPY": down, "VEU": down, "BIL": up}) == {"AGG": 1.0}


def test_backtest_buy_and_hold_trades_once_and_monthly_rebalances():
    idx = pd.bdate_range("2026-01-01", "2026-04-30")
    n = len(idx)
    p = Prices({"VT": pd.Series([100 + i for i in range(n)], index=idx),
                "1348.T": pd.Series([4000 - i for i in range(n)], index=idx)},
               pd.Series([150.0] * n, index=idx))
    res = backtest(p, idx[0], 10_000_000, costs=ZERO, strategies={
        "hold": (lambda h: {"VT": 1.0}, "once"),
        "mix": (lambda h: {"VT": 0.5, "1348.T": 0.5}, "monthly"),
    })
    assert res["hold"].trades == 1
    assert res["mix"].trades > 2          # 月初ごとに配分を戻す
    assert len(window_returns(res["hold"].nav, 21)) == n - 21
