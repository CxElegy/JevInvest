"""台帳 → 評価 → ガードレール → レポート → ダッシュボード → CLI を、ネットワーク無しで通す。"""

from pathlib import Path

import pytest
import yaml

from src import cli, compliance
from src.analytics import analyze
from src.dashboard import to_html
from src.holdings import HoldingsError, load
from src.plan import annual_projection, build
from src.report import to_markdown

from conftest import FakeMarketData

ROOT = Path(__file__).resolve().parents[1]

LEDGER = """
base_currency: JPY
accounts:
  - {name: 特定口座, type: taxable}
  - {name: NISA, type: nisa}
target_allocation: {developed_equity: 60, jp_equity: 30, cash: 10}
guards: {max_single_position_pct: 20.0, min_cash_pct: 5.0, high_correlation_threshold: 0.8}
positions:
  - {symbol: CASH-JPY, asset_class: cash, account: 特定口座, quantity: 500000,
     cost_basis_per_unit: 1, currency: JPY}
  - {symbol: VT, name: World ETF, asset_class: developed_equity, account: 特定口座,
     quantity: 10, cost_basis_per_unit: 120.0, currency: USD, cost_fx_rate: 140.0}
  - {symbol: VT, name: World ETF, asset_class: developed_equity, account: NISA,
     quantity: 10, cost_basis_per_unit: 120.0, currency: USD, cost_fx_rate: 140.0}
  - {symbol: 7203.T, name: Toyota, asset_class: jp_equity, account: 特定口座,
     quantity: 100, cost_basis_per_unit: 2800, currency: JPY}
  - {symbol: FUND-X, name: 手入力ファンド, asset_class: jp_equity, account: NISA,
     quantity: 1000, cost_basis_per_unit: 100, currency: JPY, price_override: 110,
     price_as_of: 2026-09-28}
"""


@pytest.fixture
def ledger_path(tmp_path):
    p = tmp_path / "holdings.yaml"
    p.write_text(LEDGER, encoding="utf-8")
    return p


def test_example_holdings_loads_cleanly():
    h = load(ROOT / "config" / "holdings.example.yaml")
    assert h.base_currency == "JPY" and not h.warnings


def test_duplicate_symbol_in_same_account_is_rejected(tmp_path):
    p = tmp_path / "h.yaml"
    p.write_text(LEDGER.replace("account: NISA,\n     quantity: 10", "account: 特定口座,\n     quantity: 10"),
                 encoding="utf-8")
    with pytest.raises(HoldingsError, match="重複"):
        load(p)


def test_valuation_splits_price_gain_from_fx_gain(ledger_path, market_data):
    h = load(ledger_path)
    a = analyze(h, market_data, years=1)

    vt = [v for v in a.valuations if v.position.symbol == "VT"][0]
    assert vt.priced and vt.fx_rate > 0
    assert vt.price_pnl_base + vt.fx_pnl_base == pytest.approx(vt.pnl_base)   # 分解が損益に一致

    fund = [v for v in a.valuations if v.position.symbol == "FUND-X"][0]
    assert fund.market_value_base == pytest.approx(110_000)                   # 手入力価格
    assert a.total_value_base == pytest.approx(sum(v.market_value_base for v in a.valuations))


def test_guard_sums_a_symbol_across_accounts(ledger_path, market_data):
    a = analyze(load(ledger_path), market_data, years=1)
    vt_total = sum(v.weight_pct for v in a.valuations if v.position.symbol == "VT")
    assert vt_total > 20   # 口座ごとは 20% 未満でも合算で超える構成にしてある
    assert any(g.guard == "max_single_position_pct" and g.subject == "VT"
               and g.severity == "violation" for g in a.guards)


def test_unpriced_positions_are_flagged_not_zeroed(ledger_path, tmp_path):
    md = FakeMarketData(tmp_path / "m.sqlite", {})     # 何も取れない
    a = analyze(load(ledger_path), md, years=1, with_risk=False)
    assert a.unpriced_count >= 2 and any("取れず" in w or "取得" in w for w in a.warnings)
    md.close()


def test_risk_metrics_and_report_and_dashboard(ledger_path, market_data):
    a = analyze(load(ledger_path), market_data, years=1, benchmark="VT")
    assert a.risk["年率ボラ%"] > 0 and a.risk["最大DD%"] <= 0 and "ベータ" in a.risk

    reg = compliance.load(ROOT / "config" / "compliance.example.yaml")
    text = to_markdown(a, compliance=reg)
    for needle in ("## ガードレール", "## Independence", "## サマリ", "## 集中度",
                   "## リスク", "為替差益", "この先の判断について"):
        assert needle in text

    anon = to_markdown(a, anonymize=True)
    assert "匿名モード" in anon and "1,000,000" not in anon

    html = to_html(a, anonymize=True)
    assert "__PAYLOAD__" not in html and "持仓ダッシュボード" in html


def test_plan_falls_back_to_core_when_nothing_is_cleared():
    cfg = yaml.safe_load((ROOT / "config" / "holdings.example.yaml").read_text(encoding="utf-8"))
    reg = compliance.ComplianceRegistry()
    plan = build(cfg, compliance=reg, cleared_symbols_by_class={})

    by = {c.asset_class: c for c in plan.classes}
    assert by["us_equity"].status == "保留→コアへ" and by["jp_equity"].status == "保留→コアへ"
    assert by["developed_equity"].monthly_amount > 200000 * 0.55 * 100 / 95 - 1
    assert plan.invested == pytest.approx(200000)              # 全額投資
    assert plan.tsumitate_total <= 1_200_000 / 12 + 1e-6       # つみたて枠の月上限を守る
    assert len(annual_projection(cfg, plan, years=3)) == 3


def test_cli_validate_and_report_end_to_end(ledger_path, market_data, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "OUT_DIR", tmp_path / "reports")
    monkeypatch.setattr("src.marketdata.DEFAULT_DB", tmp_path / "cli.sqlite")
    monkeypatch.setattr("src.marketdata.MarketData._download",
                        lambda self, symbols, start=None, period=None:
                        {s: market_data.data[s].copy() for s in symbols if s in market_data.data})

    common = ["--holdings", str(ledger_path), "--compliance", str(tmp_path / "none.yaml")]
    cli.main([*common, "validate"])
    assert "ポジション数" in capsys.readouterr().out

    cli.main([*common, "report", "--years", "1"])
    out = capsys.readouterr().out
    assert "レポートを出力しました" in out
    assert (tmp_path / "reports" / "latest.md").read_text(encoding="utf-8").startswith("# 持仓分析レポート")

    cli.main([*common, "compliance"])
    assert "DESC 未確認" in capsys.readouterr().out

    cli.main([*common, "dashboard", "--years", "1", "--anonymize"])
    assert (tmp_path / "reports" / "dashboard-anon.html").exists()


def test_cli_transactions_derives_holdings_and_computes_xirr(tmp_path, market_data, monkeypatch, capsys):
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    for name in ("DEFAULT_HOLDINGS", "DEFAULT_COMPLIANCE", "DEFAULT_TRANSACTIONS"):
        monkeypatch.setattr(cli, name, tmp_path / "config" / f"{name.lower()}.yaml")
    monkeypatch.setattr("src.marketdata.DEFAULT_DB", tmp_path / "cli.sqlite")
    monkeypatch.setattr("src.marketdata.MarketData._download",
                        lambda self, symbols, start=None, period=None:
                        {s: market_data.data[s].copy() for s in symbols if s in market_data.data})

    holdings = tmp_path / "holdings.yaml"
    holdings.write_text("""
base_currency: JPY
accounts: [{name: NISA, type: nisa}]
positions:
  - {symbol: CASH-JPY, asset_class: cash, account: NISA, quantity: 1, cost_basis_per_unit: 1,
     currency: JPY}
  - {symbol: VT, asset_class: developed_equity, account: NISA, quantity: 10,
     cost_basis_per_unit: 120.5, currency: USD, cost_fx_rate: 150.2}
""", encoding="utf-8")
    txs = tmp_path / "tx.yaml"
    txs.write_text("""
transactions:
  - {date: 2025-01-06, type: buy, symbol: VT, account: NISA, quantity: 10, price: 120.5,
     currency: USD, fx_rate: 150.2, asset_class: developed_equity}
""", encoding="utf-8")
    common = ["--holdings", str(holdings), "--transactions", str(txs)]

    cli.main([*common, "transactions", "--no-quotes", "--write-derived"])
    derived = (tmp_path / "config" / "holdings.derived.yaml").read_text(encoding="utf-8")
    assert "symbol: VT" in derived and "cost_fx_rate: 150.2000" in derived
    assert "quantity: 10" in derived

    cli.main([*common, "transactions"])
    out = capsys.readouterr().out
    assert "XIRR" in out and "現金を除く有価証券" in out     # 入出金なし → 売買ベース・有価証券のみ


@pytest.mark.parametrize("extra_position, extra_tx, needle", [
    # 台帳にあるが取引履歴に無い銘柄 → 評価額だけが乗って XIRR が膨らむ
    ("""  - {symbol: 7203.T, asset_class: jp_equity, account: NISA, quantity: 100,
     cost_basis_per_unit: 2800, currency: JPY}
""", "", "7203.T"),
    # 価格の取れない銘柄 → 評価額から黙って抜けて XIRR が縮む
    ("""  - {symbol: NVDA, asset_class: us_equity, account: NISA, quantity: 10,
     cost_basis_per_unit: 100, currency: USD, cost_fx_rate: 150}
""", """  - {date: 2025-01-06, type: buy, symbol: NVDA, account: NISA, quantity: 10, price: 100,
     currency: USD, fx_rate: 150, asset_class: us_equity}
""", "評価不能"),
], ids=["ledger-only-symbol", "unpriced"])
def test_cli_xirr_refuses_when_terminal_value_does_not_match_history(
        tmp_path, market_data, monkeypatch, capsys, extra_position, extra_tx, needle):
    monkeypatch.setattr("src.marketdata.DEFAULT_DB", tmp_path / "cli.sqlite")
    monkeypatch.setattr("src.marketdata.MarketData._download",
                        lambda self, symbols, start=None, period=None:
                        {s: market_data.data[s].copy() for s in symbols if s in market_data.data})
    holdings = tmp_path / "holdings.yaml"
    holdings.write_text("""
base_currency: JPY
accounts: [{name: NISA, type: nisa}]
positions:
  - {symbol: VT, asset_class: developed_equity, account: NISA, quantity: 10,
     cost_basis_per_unit: 120.5, currency: USD, cost_fx_rate: 150.2}
""" + extra_position, encoding="utf-8")
    txs = tmp_path / "tx.yaml"
    txs.write_text("""
transactions:
  - {date: 2025-01-06, type: buy, symbol: VT, account: NISA, quantity: 10, price: 120.5,
     currency: USD, fx_rate: 150.2, asset_class: developed_equity}
""" + extra_tx, encoding="utf-8")

    cli.main(["--holdings", str(holdings), "--transactions", str(txs), "transactions"])
    out = capsys.readouterr().out
    assert "計算しません" in out and needle in out
    assert "XIRR        :" not in out


def test_single_position_cap_can_exempt_core_index_and_bond_classes(tmp_path, market_data):
    p = tmp_path / "h.yaml"
    p.write_text(LEDGER.replace(
        "guards: {max_single_position_pct: 20.0,",
        "guards: {single_position_exempt_classes: [developed_equity], max_single_position_pct: 20.0,"),
        encoding="utf-8")
    a = analyze(load(p), market_data, years=1)

    assert not any(g.guard == "max_single_position_pct" and g.subject == "VT" for g in a.guards)
    assert "single_position_exempt_classes" in a.guard_config
    assert "持仓ダッシュボード" in to_html(a)        # リスト値があってもダッシュボードは作れる
    assert "single_position_exempt_classes" in to_markdown(a)
