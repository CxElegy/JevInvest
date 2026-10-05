"""積立計画。毎月の投入額を目標配分に割り振り、NISA 枠に当てはめる。

3 つの制約を同時に満たす割り当てを出す:

  1. 目標配分         … 各資産クラスの比率
  2. NISA 枠          … つみたて枠は指定インデックス投信のみ・年 120 万円、
                         成長枠は個別株も可・年 240 万円
  3. independence     … 個別株で埋めるクラスは、事前承認が取れた銘柄が
                         無ければ積立を保留する

3 を無視した計画は実行できない。だから「買えない分」は現金に留保して明示する。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .compliance import ComplianceRegistry


@dataclass
class ClassPlan:
    asset_class: str
    target_pct: float
    monthly_amount: float
    nisa_eligibility: str
    tsumitate: float = 0.0
    growth: float = 0.0
    taxable: float = 0.0
    held_cash: float = 0.0       # 承認待ちで留保する分
    status: str = ""
    note: str = ""


@dataclass
class ContributionPlan:
    monthly: float
    classes: list[ClassPlan]
    warnings: list[str] = field(default_factory=list)

    @property
    def invested(self) -> float:
        return sum(c.tsumitate + c.growth + c.taxable for c in self.classes)

    @property
    def parked(self) -> float:
        return sum(c.held_cash for c in self.classes)

    @property
    def tsumitate_total(self) -> float:
        return sum(c.tsumitate for c in self.classes)

    @property
    def growth_total(self) -> float:
        return sum(c.growth for c in self.classes)

    @property
    def taxable_total(self) -> float:
        return sum(c.taxable for c in self.classes)

    def to_frame(self) -> pd.DataFrame:
        rows = [
            {
                "資産クラス": c.asset_class,
                "目標%": c.target_pct,
                "月額": c.monthly_amount,
                "つみたて枠": c.tsumitate,
                "成長枠": c.growth,
                "課税口座": c.taxable,
                "留保(現金)": c.held_cash,
                "状態": c.status,
            }
            for c in self.classes
        ]
        return pd.DataFrame(rows).set_index("資産クラス")


def build(
    cfg: dict,
    compliance: ComplianceRegistry | None = None,
    cleared_symbols_by_class: dict[str, list[str]] | None = None,
) -> ContributionPlan:
    """積立計画を組む。

    cfg は holdings.yaml をそのまま渡す（contributions / nisa_eligibility /
    target_allocation / requires_stock_clearance を読む）。

    cleared_symbols_by_class は「そのクラスを埋められる、承認済みの銘柄」。
    独立性の確認が済んでいないクラスは積立を保留する。
    """
    contrib = cfg.get("contributions") or {}
    monthly = float(contrib.get("monthly", 0))
    if monthly <= 0:
        return ContributionPlan(monthly=0.0, classes=[],
                                warnings=["contributions.monthly が未設定です"])

    nisa = contrib.get("nisa") or {}
    elig = cfg.get("nisa_eligibility") or {}
    needs_clearance = set(cfg.get("requires_stock_clearance") or [])
    target = cfg.get("target_allocation") or {}
    cleared = cleared_symbols_by_class or {}

    warnings: list[str] = []

    # 月あたりの枠。年枠を 12 で割った額を毎月の上限として扱う。
    # （実際には年内で前倒しも可能だが、毎月一定額で積むならこの方が分かりやすい）
    t_annual = float(nisa.get("tsumitate_annual", 0))
    g_annual = float(nisa.get("growth_annual", 0))
    t_used = float(nisa.get("used_tsumitate_this_year", 0))
    g_used = float(nisa.get("used_growth_this_year", 0))

    t_budget = max(t_annual - t_used, 0) / 12.0
    g_budget = max(g_annual - g_used, 0) / 12.0

    invest_all = bool(contrib.get("invest_full_contribution", False))

    # 全額投資モードでは、現金クラス（nisa_eligibility: none）の比率を
    # 投資対象クラスに按分して上乗せする。
    # こうしないと目標配分の現金 5% が毎月の投入額から 5% を抜いてしまう。
    invest_total = sum(
        float(v) for k, v in target.items() if elig.get(k, "growth_only") != "none"
    )
    scale = (100.0 / invest_total) if (invest_all and invest_total > 0) else 1.0

    # 目標比率の大きい順に割り当てる。つみたて枠は対象商品が限られるので、
    # 使える資産クラスから先に埋めないと枠を余らせる。
    plans: list[ClassPlan] = []
    for ac, pct in sorted(target.items(), key=lambda kv: -kv[1]):
        e = elig.get(ac, "growth_only")
        if invest_all and e == "none":
            amount = 0.0
        else:
            amount = monthly * float(pct) / 100.0 * (scale if e != "none" else 1.0)
        plans.append(
            ClassPlan(
                asset_class=ac,
                target_pct=float(pct),
                monthly_amount=amount,
                nisa_eligibility=e,
            )
        )

    # --- 1) 現金クラスの扱い ---
    for p in plans:
        if p.nisa_eligibility != "none":
            continue
        if invest_all:
            p.status = "積立対象外"
            p.note = (
                "全額投資モード。毎月の投入額からは現金を取らず、"
                "この比率は投資対象クラスに按分した"
            )
        else:
            p.held_cash = p.monthly_amount
            p.status = "現金"
            p.note = "NISA 対象外。目標比率どおり現金で保持"

    # --- 2) independence のゲート ---
    #
    # 承認が取れていないクラスは積めない。その分の扱いは 2 通りある:
    #   cash … 現金で留保。承認後そのまま買えるが、待つ間 NISA 枠も消化されない
    #   core … 最大比率のインデックスへ暫定的に回す。資金は遊ばないが
    #          承認後にリバランスが必要になる
    fallback = str(contrib.get("on_hold_fallback", "cash")).lower()
    redirected = 0.0

    for p in plans:
        if p.status:
            continue
        if p.asset_class in needs_clearance:
            ok = cleared.get(p.asset_class) or []
            if not ok:
                if fallback == "core":
                    redirected += p.monthly_amount
                    p.status = "保留→コアへ"
                    p.note = (
                        "事前承認済みの銘柄が無い。暫定的にコアへ回す"
                        "（承認後にリバランスが必要）"
                    )
                else:
                    p.held_cash = p.monthly_amount
                    p.status = "保留"
                    p.note = (
                        "個別株で埋めるクラスだが、事前承認済みの銘柄が無い。"
                        "承認が取れるまで現金で留保する"
                    )
            else:
                p.note = f"承認済み: {', '.join(ok)}"

    # 回した分を、つみたて枠が使える最大比率のクラスに足す
    if redirected > 0:
        core = max(
            (p for p in plans if not p.status and p.nisa_eligibility == "both"),
            key=lambda p: p.target_pct,
            default=None,
        )
        if core is None:
            warnings.append(
                "on_hold_fallback: core を指定していますが、回せるインデックス"
                "クラスがありません。現金留保に切り替えてください"
            )
            for p in plans:
                if p.status == "保留→コアへ":
                    p.held_cash = p.monthly_amount
                    p.status = "保留"
        else:
            core.monthly_amount += redirected
            core.note = (
                f"承認待ちの {redirected:,.0f} 円を暫定的に受け入れている"
                f"（本来の目標は {monthly * core.target_pct / 100:,.0f} 円）"
            )

    # --- 3) つみたて枠を、使える資産クラスから埋める ---
    for p in plans:
        if p.status or p.nisa_eligibility != "both":
            continue
        use = min(p.monthly_amount, t_budget)
        p.tsumitate = use
        t_budget -= use

    # --- 4) 残りを成長枠へ ---
    for p in plans:
        if p.status:
            continue
        rest = p.monthly_amount - p.tsumitate
        if rest <= 0:
            p.status = "つみたて枠"
            continue
        use = min(rest, g_budget)
        p.growth = use
        g_budget -= use
        rest -= use

        # --- 5) 枠が尽きたら課税口座 ---
        if rest > 0:
            p.taxable = rest
            warnings.append(
                f"{p.asset_class}: NISA 枠が足りず {rest:,.0f} 円が課税口座になります"
            )

        parts = [n for n, v in (("つみたて枠", p.tsumitate), ("成長枠", p.growth),
                                ("課税口座", p.taxable)) if v > 0]
        p.status = " + ".join(parts)

    plan = ContributionPlan(monthly=monthly, classes=plans, warnings=warnings)

    if invest_all:
        cash_target = sum(
            float(v) for k, v in target.items() if elig.get(k, "growth_only") == "none"
        )
        if cash_target > 0:
            warnings.append(
                f"全額投資モードのため、毎月の投入額から現金を取っていません。"
                f"目標の現金 {cash_target:.0f}% は初期資金で賄われますが、"
                f"積立が進むと現金比率は目標を下回っていきます。"
                f"維持したくなった時点で一部を現金に振り直してください"
            )

    if t_budget > 1:
        warnings.append(
            f"つみたて枠が月 {t_budget:,.0f} 円分余っています"
            f"（年 {t_budget * 12:,.0f} 円）。"
            "つみたて枠は使わなかった分が翌年に繰り越せないので、"
            "インデックス投信の比率を上げるか、積立額を増やすと枠を活かせます"
        )

    return plan


def annual_projection(cfg: dict, plan: ContributionPlan, years: int = 5) -> pd.DataFrame:
    """投入額の累計と NISA 生涯枠の消化を年ごとに出す。

    リターンは仮定しない。**これは「いくら入れたか」の表であって、
    「いくらに増えるか」の表ではない。** 増え方を仮定した数字は、
    その仮定が外れたときに判断を誤らせるので出さない。
    """
    contrib = cfg.get("contributions") or {}
    nisa = contrib.get("nisa") or {}
    lifetime = float(nisa.get("lifetime", 0))
    growth_max = float(nisa.get("growth_lifetime_max", 0))

    initial_cash = 0.0
    for p in (cfg.get("positions") or []):
        if str(p.get("symbol", "")).startswith("CASH-"):
            initial_cash += float(p.get("quantity", 0)) * float(
                p.get("cost_basis_per_unit", 1.0)
            )

    rows = []
    cum_t = cum_g = cum_tax = 0.0
    for y in range(1, years + 1):
        cum_t += plan.tsumitate_total * 12
        cum_g += plan.growth_total * 12
        cum_tax += plan.taxable_total * 12
        cum_nisa = cum_t + cum_g
        rows.append(
            {
                "年": f"{y}年目",
                "累計投入": initial_cash + (plan.monthly * 12 * y),
                "累計つみたて枠": cum_t,
                "累計成長枠": cum_g,
                "NISA累計": cum_nisa,
                "生涯枠残": max(lifetime - cum_nisa, 0),
                "成長枠残": max(growth_max - cum_g, 0),
                "課税口座累計": cum_tax,
            }
        )
    return pd.DataFrame(rows).set_index("年")
