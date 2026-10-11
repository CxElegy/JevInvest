"""遊び枠の成績表。ネットワークには出ない。"""

from datetime import date

import pandas as pd
import pytest

from src.scoreboard import shadow
from src.transactions import Transaction


def _bench(values, start="2026-01-05"):
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), dtype=float)


def test_same_cash_flows_into_benchmark():
    bench = _bench([100, 100, 200, 200, 300])     # 基準は 3 倍に
    tx = [
        Transaction(date(2026, 1, 5), "buy", "NVDA", quantity=10, price=100, fee=0),
        Transaction(date(2026, 1, 7), "sell", "NVDA", quantity=5, price=100, fee=0),   # 500 円回収
        Transaction(date(2026, 1, 8), "dividend", "NVDA", amount=10),
        Transaction(date(2026, 1, 6), "buy", "VT", quantity=1, price=1, fee=0),        # 対象外
    ]
    r = shadow(tx, {"NVDA"}, bench, current_value=600)
    # 基準: 1000/100 = 10 口 → 500/200 = 2.5 口売り → 7.5 口 × 300 = 2250
    assert r.shadow_value == pytest.approx(2250)
    assert r.actual_value == pytest.approx(610)
    assert r.edge == pytest.approx(-1640)
    assert r.invested == 1000 and r.withdrawn == 500


def test_negative_shadow_is_reported_not_hidden():
    bench = _bench([100, 50])
    tx = [Transaction(date(2026, 1, 5), "buy", "X", quantity=1, price=1000),
          Transaction(date(2026, 1, 6), "sell", "X", quantity=1, price=3000)]
    r = shadow(tx, {"X"}, bench, current_value=0)
    assert r.shadow_units < 0 and r.edge > 0
    assert any("マイナス" in w for w in r.warnings)


def test_trade_before_benchmark_history_is_excluded_with_warning():
    bench = _bench([100, 100], start="2026-02-02")
    tx = [Transaction(date(2026, 1, 5), "buy", "X", quantity=1, price=100)]
    r = shadow(tx, {"X"}, bench, current_value=100)
    assert r.flows == [] and r.warnings and not r.comparable


def test_truncated_sell_counts_only_what_the_ledger_applied(tmp_path):
    from src.transactions import load as load_tx

    p = tmp_path / "tx.yaml"
    p.write_text("transactions:\n"
                 "  - {date: 2026-01-05, type: buy, symbol: X, quantity: 10, price: 100}\n"
                 "  - {date: 2026-01-06, type: sell, symbol: X, quantity: 30, price: 100}\n",
                 encoding="utf-8")
    led = load_tx(p)
    r = shadow(led.transactions, {"X"}, _bench([100, 100]), current_value=0)
    assert r.withdrawn == pytest.approx(1000)      # 30 株ではなく、台帳に反映した 10 株
    assert r.edge == pytest.approx(0)


def test_trade_without_benchmark_price_makes_result_not_comparable():
    bench = _bench([100, 100], start="2026-02-02")
    tx = [Transaction(date(2026, 1, 5), "buy", "X", quantity=1, price=100)]
    r = shadow(tx, {"X"}, bench, current_value=100)
    assert not r.comparable and r.invested == 0
