"""売買前の点検。ネットワークには出ない。"""

from datetime import datetime, timedelta

import yaml

from src import compliance
from src.analytics import analyze
from src.holdings import load
from src.pretrade import CAUTION, OK, STOP, Intent, run_checks, verdict

from conftest import FakeMarketData  # noqa: F401  (fixture の型)

LEDGER = """
base_currency: JPY
accounts:
  - {name: 特定口座, type: taxable}
  - {name: NISA, type: nisa}
target_allocation: {developed_equity: 50, jp_equity: 30, cash: 20}
guards: {max_single_position_pct: 20.0, min_cash_pct: 5.0,
         single_position_exempt_classes: [developed_equity]}
contributions:
  nisa: {tsumitate_annual: 1200000, growth_annual: 2400000,
         used_tsumitate_this_year: 1200000, used_growth_this_year: 2300000}
nisa_eligibility: {developed_equity: both, jp_equity: growth_only, cash: none}
positions:
  - {symbol: CASH-JPY, asset_class: cash, account: 特定口座, quantity: 1000000,
     cost_basis_per_unit: 1, currency: JPY}
  - {symbol: 7203.T, name: Toyota, asset_class: jp_equity, account: 特定口座,
     quantity: 100, cost_basis_per_unit: 2000, currency: JPY}
"""

NOW = datetime(2026, 10, 11, 12, 0)


def _setup(tmp_path, market_data):
    p = tmp_path / "h.yaml"
    p.write_text(LEDGER, encoding="utf-8")
    h = load(p)
    a = analyze(h, market_data, with_risk=False)
    cfg = yaml.safe_load(LEDGER)
    c = tmp_path / "c.yaml"
    c.write_text("securities:\n  - {symbol: 7203.T, desc_result: clear, desc_checked: 2026-10-01,"
                 " preclearance: approved}\n", encoding="utf-8")
    reg = compliance.load(c, today=NOW.date())
    return h, a, cfg, reg


def _by_title(checks):
    return {c.title: c for c in checks}


def test_unchecked_security_stops_the_buy(tmp_path, market_data):
    h, a, cfg, reg = _setup(tmp_path, market_data)
    checks = run_checks(Intent("buy", "VT", 100_000, "developed_equity", "特定口座"),
                        a, h, cfg, reg, [], tmp_path / "i.yaml", NOW)
    assert _by_title(checks)["independence"].level == STOP
    assert verdict(checks).startswith("実行しない")


def test_cap_cash_floor_and_nisa_allowance(tmp_path, market_data):
    h, a, cfg, reg = _setup(tmp_path, market_data)
    total = a.total_value_base
    checks = run_checks(Intent("buy", "7203.T", total * 0.5, "jp_equity", "NISA"),
                        a, h, cfg, reg, [], tmp_path / "i.yaml", NOW)
    t = _by_title(checks)
    assert t["independence"].level == OK
    assert t["1 銘柄の上限"].level == STOP
    assert t["NISA の年間枠"].level == CAUTION        # 成長枠の残り 10 万円
    assert "100,000" in t["NISA の年間枠"].detail     # growth_only なのでつみたて枠は数えない


def test_taxable_sell_estimates_tax_and_cooling_off(tmp_path, market_data):
    h, a, cfg, reg = _setup(tmp_path, market_data)
    mv = next(v.market_value_base for v in a.valuations if v.position.symbol == "7203.T")
    intents = tmp_path / "i.yaml"
    tx = [{"date": (NOW - timedelta(days=5)).date().isoformat(), "type": "buy", "symbol": "7203.T"}]
    checks = run_checks(Intent("sell", "7203.T", mv, "jp_equity", "特定口座"),
                        a, h, cfg, reg, tx, intents, NOW)
    t = _by_title(checks)
    assert t["税"].level == CAUTION and "20.315%" in t["税"].detail
    assert t["冷却期間"].level == CAUTION
    assert t["保有期間"].level == CAUTION
    # 25 時間後にもう一度点検すると、冷却期間は通る
    later = run_checks(Intent("sell", "7203.T", mv, "jp_equity", "特定口座"),
                       a, h, cfg, reg, tx, intents, NOW + timedelta(hours=25))
    assert _by_title(later)["冷却期間"].level == OK
