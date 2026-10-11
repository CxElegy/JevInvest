"""積立計画の NISA 枠の当てはめ（年の途中・生涯枠・外貨現金）。"""

from datetime import date
from pathlib import Path

import pytest
import yaml

from src.plan import annual_projection, build

ROOT = Path(__file__).resolve().parents[1]
CLEARED = {"developed_equity": ["VT"], "us_equity": ["X"], "jp_equity": ["Y"], "bond": ["B"]}


def _cfg(monthly=200000, **nisa):
    cfg = yaml.safe_load((ROOT / "config" / "holdings.example.yaml").read_text(encoding="utf-8"))
    cfg["contributions"]["monthly"] = monthly
    cfg["contributions"]["nisa"].update(nisa)
    return cfg


def test_remaining_annual_allowance_is_spread_over_months_left_in_the_year():
    # 10 月に成長枠を 230 万使っている → 残り 10 万を 10〜12 月の 3 か月で使える
    cfg = _cfg(used_growth_this_year=2_300_000)
    plan = build(cfg, cleared_symbols_by_class=CLEARED, today=date(2026, 10, 5))
    assert plan.months_left == 3
    assert plan.growth_total == pytest.approx(100_000 / 3)

    proj = annual_projection(cfg, plan, years=2)
    first, second = proj.iloc[0], proj.iloc[1]
    assert "残り 3 か月" in proj.index[0]
    assert first["累計投入"] == pytest.approx(1_000_000 + 200_000 * 3)
    # 来年は年枠が満額に戻る（今年の減った月額を写さない）
    full = plan.full_year
    assert full.growth_total + full.tsumitate_total == pytest.approx(200_000)
    assert second["課税口座累計"] - first["課税口座累計"] == pytest.approx(full.taxable_total * 12)
    assert full.taxable_total == pytest.approx(0)


def test_projection_respects_growth_and_total_lifetime_limits():
    cfg = _cfg(monthly=300_000)
    plan = build(cfg, cleared_symbols_by_class=CLEARED, today=date(2026, 1, 10))
    proj = annual_projection(cfg, plan, years=8)
    assert proj["累計成長枠"].max() <= 12_000_000 + 1e-6
    assert proj["NISA累計"].max() <= 18_000_000 + 1e-6
    # 上限を超えた分は消えずに課税口座に回る
    last = proj.iloc[-1]
    assert last["NISA累計"] + last["課税口座累計"] == pytest.approx(300_000 * 12 * 8)


def test_projection_starts_from_lifetime_already_used():
    cfg = _cfg(used_lifetime=17_000_000, used_growth_lifetime=11_500_000)
    plan = build(cfg, cleared_symbols_by_class=CLEARED, today=date(2026, 1, 10))
    # 今年の割り当ても生涯枠の残り 100 万で頭打ちになる
    assert (plan.tsumitate_total + plan.growth_total) * 12 == pytest.approx(1_000_000)
    assert plan.growth_total * 12 <= 500_000 + 1e-6
    proj = annual_projection(cfg, plan, years=2)
    assert proj.iloc[0]["生涯枠残"] == pytest.approx(0)
    assert proj.iloc[-1]["NISA累計"] == pytest.approx(18_000_000)


def test_foreign_cash_is_not_counted_as_yen_in_projection():
    cfg = _cfg()
    cfg["positions"].append({"symbol": "CASH-USD", "asset_class": "cash", "account": "特定口座",
                             "quantity": 1000, "cost_basis_per_unit": 1, "currency": "USD"})
    plan = build(cfg, cleared_symbols_by_class=CLEARED, today=date(2026, 1, 10))
    proj = annual_projection(cfg, plan, years=1)
    assert proj.iloc[0]["累計投入"] == pytest.approx(1_000_000 + 200_000 * 12)
    assert any("CASH-USD" in n for n in proj.attrs["notes"])

    cfg["positions"][-1]["cost_fx_rate"] = 150
    proj = annual_projection(cfg, plan, years=1)
    assert proj.iloc[0]["累計投入"] == pytest.approx(1_000_000 + 150_000 + 200_000 * 12)


def test_cleared_candidate_not_yet_held_opens_its_class(tmp_path):
    """まだ持っていない候補でも、購入可と記録され asset_class があればクラスに数える。"""
    from datetime import date

    from src.compliance import cleared_by_class, load as load_c

    c = tmp_path / "c.yaml"
    c.write_text(
        "securities:\n"
        "  - {symbol: FUND-ALLCOUNTRY, desc_result: clear, desc_checked: 2026-10-01,"
        " preclearance: not_required, asset_class: developed_equity}\n"
        "  - {symbol: FUND-BOND, desc_result: clear, desc_checked: 2026-10-01,"
        " preclearance: pending, asset_class: bond}\n", encoding="utf-8")
    reg = load_c(c, today=date(2026, 10, 11))
    assert cleared_by_class(reg, []) == {"developed_equity": ["FUND-ALLCOUNTRY"]}
