from datetime import date
from pathlib import Path

import pytest

from src import compliance
from src.transactions import cash_flows, load as load_tx, xirr

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 28)


# ------------------------------------------------------------ compliance

def _reg(tmp_path, body: str):
    p = tmp_path / "c.yaml"
    p.write_text(body, encoding="utf-8")
    return compliance.load(p, today=TODAY)


def test_example_file_covers_every_state():
    reg = compliance.load(ROOT / "config" / "compliance.example.yaml", today=TODAY)
    states = {s: reg.assess(s).state for s in
              ("EXAMPLE-A", "EXAMPLE-B", "EXAMPLE-C", "EXAMPLE-D", "EXAMPLE-E", "UNKNOWN")}
    assert states == {
        "EXAMPLE-A": "ok", "EXAMPLE-B": "pending", "EXAMPLE-C": "blocked",
        "EXAMPLE-D": "escalate", "EXAMPLE-E": "unchecked", "UNKNOWN": "unchecked",
    }


def test_only_ok_can_be_bought_and_unknown_cannot_be_held(tmp_path):
    reg = _reg(tmp_path, """
securities:
  - {symbol: A, desc_result: clear, desc_checked: 2026-09-01, preclearance: approved}
  - {symbol: B, desc_result: clear, desc_checked: 2026-09-01, preclearance: pending}
  - {symbol: C, desc_result: restricted, desc_checked: 2026-09-01}
""")
    assert reg.can_buy("A") and reg.can_hold("A")
    assert not reg.can_buy("B") and reg.can_hold("B")
    assert not reg.can_buy("C") and not reg.can_hold("C")
    assert not reg.can_hold("NOT-LISTED")


def test_stale_check_and_expired_preclearance(tmp_path):
    reg = _reg(tmp_path, """
recheck_after_days: 30
securities:
  - {symbol: OLD, desc_result: clear, desc_checked: 2026-06-01, preclearance: approved}
  - {symbol: EXP, desc_result: clear, desc_checked: 2026-09-20, preclearance: approved,
     preclearance_expires: 2026-09-01}
""")
    assert reg.assess("OLD").state == "stale" and not reg.can_buy("OLD")
    assert reg.assess("EXP").state == "pending" and "期限切れ" in reg.assess("EXP").reason


def test_clear_without_date_is_not_trusted(tmp_path):
    reg = _reg(tmp_path, "securities:\n  - {symbol: A, desc_result: clear, preclearance: approved}\n")
    assert reg.assess("A").state == "unchecked" and reg.warnings


def test_missing_file_means_everything_unchecked(tmp_path):
    reg = compliance.load(tmp_path / "nope.yaml", today=TODAY)
    assert reg.warnings and not reg.can_buy("VT")


# ------------------------------------------------------------ transactions

def test_example_ledger_average_cost_and_realized_pnl():
    led = load_tx(ROOT / "config" / "transactions.example.yaml", base_currency="JPY")
    assert not led.warnings

    lots = {l.symbol: l for l in led.open_lots}
    vt, toyota = lots["VT"], lots["7203.T"]
    assert vt.quantity == 10 and vt.avg_price_native == pytest.approx(120.5)
    assert vt.avg_fx == pytest.approx(150.2)
    assert toyota.quantity == 60 and toyota.avg_price_native == pytest.approx(2800)

    # 売却 40 株: 受取 124,000 − 原価 112,000 − 買い手数料の按分 44 − 売り手数料 110
    assert led.realized_total == pytest.approx(11846)
    assert led.dividends_total == pytest.approx(12.3 * 158.0)
    assert led.fees_total == pytest.approx(220)
    assert led.flow_mode == "external"


def test_foreign_trade_without_fx_rate_is_skipped_with_warning(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("""
transactions:
  - {date: 2026-04-02, type: buy, symbol: VT, account: NISA, quantity: 1, price: 100, currency: USD}
""", encoding="utf-8")
    led = load_tx(p, base_currency="JPY")
    assert led.transactions == [] and any("fx_rate" in w for w in led.warnings)


def test_oversell_is_clamped_and_warned(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("""
transactions:
  - {date: 2026-04-01, type: buy, symbol: A, account: X, quantity: 10, price: 100}
  - {date: 2026-05-01, type: sell, symbol: A, account: X, quantity: 15, price: 110}
""", encoding="utf-8")
    led = load_tx(p)
    assert led.open_lots == [] and any("超えて" in w for w in led.warnings)
    assert led.realized_total == pytest.approx(100)


def test_xirr_known_answer():
    flows = [(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 1100.0)]
    assert xirr(flows) == pytest.approx(0.10, abs=1e-3)


def test_xirr_returns_none_without_sign_change():
    assert xirr([(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), -5.0)]) is None


def test_trade_mode_flows_use_trades_when_no_deposits(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("""
transactions:
  - {date: 2025-01-01, type: buy, symbol: A, account: X, quantity: 10, price: 100, fee: 10}
""", encoding="utf-8")
    led = load_tx(p)
    flows = cash_flows(led, 1100.0, as_of=date(2026, 1, 1))
    assert led.flow_mode == "trade"
    assert flows[0] == (date(2025, 1, 1), -1010.0) and flows[-1][1] == 1100.0


def test_xirr_flows_use_quantity_actually_applied(tmp_path):
    # 切り詰めた売却・飛ばした売却を、元の数量のままキャッシュフローに入れない
    p = tmp_path / "t.yaml"
    p.write_text("""
transactions:
  - {date: 2025-01-01, type: buy, symbol: A, account: X, quantity: 10, price: 100}
  - {date: 2025-07-01, type: sell, symbol: A, account: X, quantity: 12, price: 100}
  - {date: 2025-08-01, type: sell, symbol: B, account: X, quantity: 5, price: 100, fee: 3}
""", encoding="utf-8")
    led = load_tx(p)
    flows = cash_flows(led, 0.0, as_of=date(2026, 1, 1))
    assert (date(2025, 7, 1), 1000.0) in flows                 # 12 株ではなく 10 株分
    assert not any(d == date(2025, 8, 1) for d, _ in flows)    # 保有が無い売却は何も生まない
    assert xirr(flows) is None or abs(xirr(flows)) < 1e-6
