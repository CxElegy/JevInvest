"""年末の損出し・益出しの点検（src/harvest.py）。ネットワーク不要。"""

import pytest

from src.analytics import PositionValuation
from src.harvest import TAX_RATE, defer_value, render, review
from src.holdings import Position

TYPES = {"特定口座": "taxable", "NISA": "nisa"}


def _v(symbol, pnl, value=1_000_000, account="特定口座", currency="JPY", fx=None, priced=True, rate=1.0):
    p = Position(symbol, symbol, "x", account, 1, value - pnl, currency, cost_fx_rate=fx)
    return PositionValuation(p, priced, "test", 1.0, rate, value if priced else 0.0, value - pnl,
                             pnl if priced else 0.0, 0.0)


def test_loss_harvest_uses_losses_up_to_realized_gain():
    vals = [_v("AAA", -300_000), _v("BBB", -100_000), _v("CCC", +200_000)]
    r = review(vals, TYPES, 350_000, "test", 2026, can_buy=lambda s: s == "AAA")
    assert r.mode == "loss"
    assert [c.symbol for c in r.candidates] == ["AAA", "BBB"]          # 含み益の CCC は出さない
    used = {c.symbol: c.used for c in r.candidates}
    assert used == {"AAA": 300_000, "BBB": 50_000}
    assert r.tax_effect == pytest.approx(350_000 * TAX_RATE)
    bbb = r.candidates[1]
    assert "一部だけ" in bbb.note and "買い戻しは不可" in bbb.note
    assert "繰り延べ" in " ".join(r.notes)


def test_gain_harvest_when_year_is_a_loss():
    vals = [_v("AAA", -300_000), _v("CCC", +200_000)]
    r = review(vals, TYPES, -150_000, "test", 2026, can_buy=lambda s: True)
    assert r.mode == "gain"
    assert [(c.symbol, c.used) for c in r.candidates] == [("CCC", 150_000)]


def test_nisa_and_unknown_fx_are_excluded_not_zeroed():
    vals = [_v("NIS", -500_000, account="NISA"),
            _v("USD1", -200_000, currency="USD"),                       # 取得時の為替なし
            _v("USD2", -200_000, currency="USD", fx=150.0, rate=150.0),
            _v("GONE", -1, priced=False)]
    r = review(vals, TYPES, 1_000_000, "test", 2026, can_buy=lambda s: True)
    assert [c.symbol for c in r.candidates] == ["USD2"]
    assert r.skipped_nisa == 1
    assert any("USD1" in u and "cost_fx_rate" in u for u in r.unknown)
    assert any("GONE" in u and "評価不能" in u for u in r.unknown)
    assert r.candidates[0].cost == pytest.approx(2 * 22 * 150)        # 約 6,667 ドル: 0.495% は 33 ドルで上限 22 ドル


def test_unknown_realized_lists_losses_without_tax_claim():
    r = review([_v("AAA", -300_000), _v("CCC", 10)], TYPES, None, "なし", 2026, can_buy=lambda s: True)
    assert r.mode is None and r.tax_effect == 0
    assert [c.symbol for c in r.candidates] == ["AAA"]
    out = render(r)
    assert "不明" in out and "--realized" in out


def test_pct_only_hides_amounts():
    r = review([_v("AAA", -300_000)], TYPES, 123_456, "test", 2026, can_buy=lambda s: True)
    out = render(r, pct_only=True)
    assert "123" not in out and "300,000" not in out and " 円" not in out
    assert "-30.0%" in out or "-23.1%" in out


def test_defer_value_is_less_than_the_tax_itself():
    assert 0 < defer_value(100.0) < 100.0


def test_us_commission_is_capped_at_22_usd_per_trade():
    from src.harvest import Candidate, round_trip_cost

    big = Candidate("NVDA", "特定口座", "loss", -1_000_000, 15_000_000, currency="USD", fx_rate=150.0)
    assert round_trip_cost(big) == pytest.approx(2 * 22 * 150)      # 10 万ドル: 0.495% なら 495 ドルだが上限 22 ドル
    small = Candidate("X", "特定口座", "loss", -10_000, 150_000, currency="USD", fx_rate=150.0)
    assert round_trip_cost(small) == pytest.approx(2 * 1_000 * 0.00495 * 150)


def test_dividends_widen_loss_room_and_shrink_gain_room():
    vals = [_v("AAA", -300_000), _v("CCC", +200_000)]
    # 実現益 0 でも、課税口座の配当 100,000 円の源泉税は損出しで戻る
    r = review(vals, TYPES, 0, "test", 2026, can_buy=lambda s: True, dividends=100_000)
    assert r.mode == "loss" and r.candidates[0].used == 100_000
    # 実現損 150,000 のうち 100,000 は配当と相殺済み → 益出しで非課税にできるのは 50,000
    r = review(vals, TYPES, -150_000, "test", 2026, can_buy=lambda s: True, dividends=100_000)
    assert r.mode == "gain" and r.candidates[0].used == 50_000
    # 繰越損失があると益出しの余地が増える
    r = review(vals, TYPES, -150_000, "test", 2026, can_buy=lambda s: True, dividends=100_000, carryover=120_000)
    assert r.candidates[0].used == 170_000 and any("確定申告" in n for n in r.notes)
