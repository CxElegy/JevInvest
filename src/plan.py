"""積立計画。毎月の投入額を目標配分に割り振り、NISA 枠に当てはめる。

3 つの制約を同時に満たす割り当てを出す:

  1. 目標配分         … 各資産クラスの比率
  2. NISA 枠          … つみたて枠は指定インデックス投信のみ・年 120 万円、
                         成長枠は個別株も可・年 240 万円
  3. independence     … 購入可（DESC 確認済み・事前承認済み）と記録された銘柄が
                         無いクラスには積み立てない。インデックスや債券のクラスも同じ

3 を無視した計画は実行できない。だから「買えない分」は現金に留保して明示する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

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
    # この計画が当てはまる今年の残り月数（今月を含む）。NISA の年枠の残りをこれで割る
    months_left: int = 12
    # 来年以降（年枠が満額に戻った年）の毎月の割り当て。None なら今年と同じ
    full_year: "ContributionPlan | None" = None

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


def _nisa_inputs(cfg: dict) -> dict:
    """contributions.nisa を読む。生涯の使用額は今年の使用額を下回らないようにする。"""
    nisa = (cfg.get("contributions") or {}).get("nisa") or {}
    f = lambda k: float(nisa.get(k, 0) or 0)  # noqa: E731
    t_used, g_used = f("used_tsumitate_this_year"), f("used_growth_this_year")
    return {
        "t_annual": f("tsumitate_annual"),
        "g_annual": f("growth_annual"),
        "lifetime": f("lifetime"),
        "growth_max": f("growth_lifetime_max"),
        "t_used": t_used,
        "g_used": g_used,
        # used_lifetime は今年の分を含む累計。書いていなければ今年の分だけとみなす
        "used_lifetime": max(f("used_lifetime"), t_used + g_used),
        "used_growth_lifetime": max(f("used_growth_lifetime"), g_used),
    }


def build(
    cfg: dict,
    compliance: ComplianceRegistry | None = None,
    cleared_symbols_by_class: dict[str, list[str]] | None = None,
    today: date | None = None,
) -> ContributionPlan:
    """積立計画を組む。

    cfg は holdings.yaml をそのまま渡す（contributions / nisa_eligibility /
    target_allocation / requires_stock_clearance を読む）。

    cleared_symbols_by_class は「そのクラスを埋められる、承認済みの銘柄」。
    独立性の確認が済んでいないクラスは積立を保留する。

    today はテスト用。今年の残り月数を決める。
    """
    contrib = cfg.get("contributions") or {}
    monthly = float(contrib.get("monthly", 0))
    if monthly <= 0:
        return ContributionPlan(monthly=0.0, classes=[],
                                warnings=["contributions.monthly が未設定です"])

    cleared = cleared_symbols_by_class or {}
    n = _nisa_inputs(cfg)

    # 月あたりの枠。今年の年枠の残りを、今年の残り月数（今月を含む）で割る。
    # 12 で割ると、年の途中では使える枠を小さく見積もって課税口座に溢れさせる。
    # 生涯枠・成長枠の生涯上限が近ければ、そちらで頭を抑える。
    months_left = 12 - (today or date.today()).month + 1
    t_rem = max(n["t_annual"] - n["t_used"], 0)
    g_rem = max(n["g_annual"] - n["g_used"], 0)
    if n["lifetime"] > 0:
        life_rem = max(n["lifetime"] - n["used_lifetime"], 0)
        t_rem = min(t_rem, life_rem)
        g_rem = min(g_rem, life_rem - t_rem)
    if n["growth_max"] > 0:
        g_rem = min(g_rem, max(n["growth_max"] - n["used_growth_lifetime"], 0))

    plans, warnings, t_left = _allocate(
        cfg, monthly, cleared, t_rem / months_left, g_rem / months_left
    )
    # 来年以降は年枠が満額に戻る。生涯枠の残りは annual_projection で年ごとに当てる
    full_plans, _, full_t_left = _allocate(
        cfg, monthly, cleared, n["t_annual"] / 12.0, n["g_annual"] / 12.0
    )
    full = ContributionPlan(monthly=monthly, classes=full_plans)
    plan = ContributionPlan(monthly=monthly, classes=plans, warnings=warnings,
                            months_left=months_left, full_year=full)

    if months_left < 12 or n["t_used"] > 0:
        if t_left * months_left > 1:
            warnings.append(
                f"今年のつみたて枠は、残り {months_left} か月の積立では "
                f"{t_left * months_left:,.0f} 円使い切れません（翌年に繰り越せない）"
            )
        if full.taxable_total > plan.taxable_total + 1:
            warnings.append(
                f"来年以降は年枠が月あたり {(n['t_annual'] + n['g_annual']) / 12:,.0f} 円に"
                f"なるため、月 {full.taxable_total:,.0f} 円が課税口座になります"
            )
    if full_t_left > 1:
        warnings.append(
            f"つみたて枠が月 {full_t_left:,.0f} 円分余っています"
            f"（年 {full_t_left * 12:,.0f} 円）。"
            "つみたて枠は使わなかった分が翌年に繰り越せないので、"
            "インデックス投信の比率を上げるか、積立額を増やすと枠を活かせます"
        )

    return plan


def _allocate(
    cfg: dict, monthly: float, cleared: dict[str, list[str]],
    t_budget: float, g_budget: float,
) -> tuple[list[ClassPlan], list[str], float]:
    """月 monthly を、月あたりの枠 t_budget / g_budget に当てはめる。

    戻り値は（クラスごとの割り当て、警告、使い残したつみたて枠の月額）。
    """
    contrib = cfg.get("contributions") or {}
    elig = cfg.get("nisa_eligibility") or {}
    needs_clearance = set(cfg.get("requires_stock_clearance") or [])
    target = cfg.get("target_allocation") or {}
    warnings: list[str] = []

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
    # 購入可と記録された銘柄が無いクラスには積めない。個別株のクラスだけでなく、
    # インデックスや債券のクラスも同じ（投信・ETF も DESC の対象になりうる）。
    # requires_stock_clearance は「銘柄ごとの事前承認が要る」ことの表示に使う。
    #
    # 積めない分の扱いは 2 通りある:
    #   cash … 現金で留保。承認後そのまま買えるが、待つ間 NISA 枠も消化されない
    #   core … 購入可の銘柄があるインデックスクラスへ暫定的に回す。資金は遊ばないが
    #          承認後にリバランスが必要になる。回せる先が無ければ現金で留保する
    fallback = str(contrib.get("on_hold_fallback", "cash")).lower()
    redirected = 0.0

    for p in plans:
        if p.status:
            continue
        ok = cleared.get(p.asset_class) or []
        if ok:
            p.note = f"購入可: {', '.join(ok)}"
            continue
        why = (
            "個別株で埋めるクラスだが、事前承認済みの銘柄が無い"
            if p.asset_class in needs_clearance
            else "購入可（DESC 確認済み・事前承認済み）と記録された銘柄が無い"
        )
        if fallback == "core":
            redirected += p.monthly_amount
            p.status = "保留→コアへ"
            p.note = f"{why}。暫定的にコアへ回す（承認後にリバランスが必要）"
        else:
            p.held_cash = p.monthly_amount
            p.status = "保留"
            p.note = f"{why}。確認が取れるまで現金で留保する"

    # 回した分を、購入可の銘柄があり、つみたて枠も使える最大比率のクラスに足す
    if redirected > 0:
        core = max(
            (p for p in plans if not p.status and p.nisa_eligibility == "both"),
            key=lambda p: p.target_pct,
            default=None,
        )
        if core is None:
            warnings.append(
                "on_hold_fallback: core ですが、購入可の銘柄があるインデックスクラスが"
                "無いため、承認待ちの分は現金で留保しました"
            )
            for p in plans:
                if p.status == "保留→コアへ":
                    p.held_cash = p.monthly_amount
                    p.status = "保留"
                    p.note = p.note.replace(
                        "暫定的にコアへ回す（承認後にリバランスが必要）",
                        "回せるコアも無いため現金で留保する",
                    )
        else:
            core.monthly_amount += redirected
            core.note = (
                f"承認待ちの {redirected:,.0f} 円を暫定的に受け入れている"
                f"（本来の目標は {monthly * core.target_pct / 100:,.0f} 円）"
            )

    held = [p for p in plans if p.status == "保留"]
    if held:
        warnings.append(
            f"購入可と記録された銘柄が無いため、月 {sum(p.held_cash for p in held):,.0f} 円を"
            f"現金で留保しています（{', '.join(p.asset_class for p in held)}）。"
            "DESC 確認と事前承認の結果を compliance.yaml に記録すると積立に回ります"
            "（判定するのは台帳に保有している銘柄だけ）"
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

    return plans, warnings, t_budget


def annual_projection(cfg: dict, plan: ContributionPlan, years: int = 5) -> pd.DataFrame:
    """投入額の累計と NISA 生涯枠の消化を年ごとに出す。

    リターンは仮定しない。**これは「いくら入れたか」の表であって、
    「いくらに増えるか」の表ではない。** 増え方を仮定した数字は、
    その仮定が外れたときに判断を誤らせるので出さない。

    1 年目は今年の残り月数（plan.months_left）だけ、2 年目以降は年枠が満額に
    戻った割り当て（plan.full_year）で 12 か月分を積む。NISA の累計は過去の
    使用額（used_lifetime / used_growth_lifetime）から始め、生涯枠 と 成長枠の
    生涯上限を超える分は課税口座に回す（売却による枠の復活は考えない）。

    換算できない外貨現金は累計投入から除き、その旨を df.attrs["notes"] に入れる。
    """
    n = _nisa_inputs(cfg)
    lifetime, growth_max = n["lifetime"], n["growth_max"]
    base = str(cfg.get("base_currency") or "JPY").upper()
    notes: list[str] = []

    initial_cash = 0.0
    for p in (cfg.get("positions") or []):
        sym = str(p.get("symbol", ""))
        if not sym.startswith("CASH-"):
            continue
        native = float(p.get("quantity", 0)) * float(p.get("cost_basis_per_unit", 1.0))
        ccy = str(p.get("currency") or base).upper()
        if ccy == base:
            initial_cash += native
        elif p.get("cost_fx_rate") is not None:
            # 時価の為替は持っていないので、取得時の為替で換算する
            initial_cash += native * float(p["cost_fx_rate"])
            notes.append(f"{sym} は取得時の為替（cost_fx_rate）で {base} に換算して累計投入に含めた")
        else:
            notes.append(f"{sym} は {ccy} 建てで換算レートが無いため、累計投入に含めていない")

    rows = []
    cum_t = max(n["used_lifetime"] - n["used_growth_lifetime"], 0.0)
    cum_g = n["used_growth_lifetime"]
    cum_tax = 0.0
    cum_months = 0
    for y in range(1, years + 1):
        p = plan if y == 1 else (plan.full_year or plan)
        m = plan.months_left if y == 1 else 12
        cum_months += m
        t = p.tsumitate_total * m
        g = p.growth_total * m
        tax = p.taxable_total * m

        # 生涯上限を超える分は課税口座へ。成長枠から先に削る
        if growth_max > 0:
            over = max(g - max(growth_max - cum_g, 0.0), 0.0)
            g -= over
            tax += over
        if lifetime > 0:
            over = max(t + g - max(lifetime - cum_t - cum_g, 0.0), 0.0)
            cut_g = min(g, over)
            g -= cut_g
            t -= over - cut_g
            tax += over

        cum_t += t
        cum_g += g
        cum_tax += tax
        cum_nisa = cum_t + cum_g
        label = f"{y}年目" + (f"（今年の残り {m} か月）" if y == 1 and m < 12 else "")
        rows.append(
            {
                "年": label,
                "累計投入": initial_cash + plan.monthly * cum_months,
                "累計つみたて枠": cum_t,
                "累計成長枠": cum_g,
                "NISA累計": cum_nisa,
                "生涯枠残": max(lifetime - cum_nisa, 0),
                "成長枠残": max(growth_max - cum_g, 0),
                "課税口座累計": cum_tax,
            }
        )
    df = pd.DataFrame(rows).set_index("年")
    df.attrs["notes"] = notes
    return df
