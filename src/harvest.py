"""年末の損出し・益出しの点検（課税口座だけ）。

    python -m src.cli harvest --realized 300000   # 今年の実現益（証券会社の画面の数字）を入れる
    python -m src.cli harvest                     # transactions.yaml から今年の実現損益を数える
    python -m src.cli harvest --pct-only          # 金額を出さず、比率と件数だけ

日本の上場株式等の譲渡益は一律 20.315%。同じ年の譲渡損と相殺できる（損益通算）。
  - 今年が実現益 → 含み損の銘柄を売って損を確定すると、その年の税が減る（損出し）
  - 今年が実現損 → 含み益の銘柄を売って益を確定しても税がかからず、取得単価が上がる（益出し）
NISA の損益は通算できないので対象外。米国のような「同じ銘柄の買い戻しを禁じる規則」は無い。

ただし損出しの多くは**税の繰り延べ**: 買い戻すと取得単価が下がるので、将来売るときに同じ額の税を払う。
得になるのは、繰り延べた税を運用できる時間と、将来の売却が損失の年と重なる可能性の分。往復の売買コストが
それを上回るなら、やらないほうがよい。益出しは、使わなければ消える損（確定申告で 3 年繰り越さない場合）を
使い切るので、繰り延べではなく将来の税が減る。

このモジュールは判定と計算だけ。売買の指示はしない。買い戻しは購入なので independence の確認が要る。
"""

from __future__ import annotations

from dataclasses import dataclass, field

TAX_RATE = 0.20315
# 往復（売って買い戻す）のコストの目安。--cost-pct で上書きできる
JPY_ROUND_TRIP = 0.002          # 日本株・ETF: 手数料 0 円のコース（SBI・楽天）でも売り買いのスプレッドはかかる
US_FEE, US_FEE_CAP_USD = 0.00495, 22.0   # 米国株: 約定代金の 0.495%（上限 22 ドル）を売りと買いで 2 回。外貨のまま買い戻す前提
FUND_ROUND_TRIP = 0.001         # 投信: 多くはノーロード。信託財産留保額があれば足す
DEFER_YEARS, DEFER_RETURN = 10, 0.05                # 繰り延べの価値の目安: 10 年後に売る、年 5% で運用


@dataclass
class Candidate:
    symbol: str
    account: str
    kind: str                  # "loss"（損出し） / "gain"（益出し）
    pnl: float                 # 含み損益（基準通貨、税務上の円換算）
    value: float               # 評価額
    currency: str = "JPY"
    fx_rate: float = 1.0       # 評価に使った為替（米国株の手数料の上限の換算用）
    is_fund: bool = False
    used: float = 0.0          # 今年の実現損益と相殺に使える額
    cost: float = 0.0          # 往復の売買コスト（概算）
    rebuy_ok: bool = False     # 購入可と記録されているか（買い戻しの可否）
    note: str = ""

    @property
    def tax_effect(self) -> float:
        return self.used * TAX_RATE


@dataclass
class Review:
    year: int
    realized: float | None                 # 今年の実現損益（課税口座の合計）。不明なら None
    realized_source: str
    dividends: float = 0.0                 # 課税口座で受け取った今年の配当等（株式数比例配分方式。損と通算できる）
    carryover: float = 0.0                 # 前年以前から繰り越している損（確定申告で繰り越している場合）
    candidates: list[Candidate] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)      # 税務上の損益が出せない保有
    skipped_nisa: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def base(self) -> float | None:
        """今年の課税の土台: 実現損益 + 配当等 − 繰越損失。正なら税がかかる額、負なら使い切れていない損。"""
        if self.realized is None:
            return None
        return self.realized + self.dividends - self.carryover

    @property
    def mode(self) -> str | None:
        b = self.base
        if b is None or b == 0:
            return None
        return "loss" if b > 0 else "gain"

    @property
    def tax_effect(self) -> float:
        return sum(c.tax_effect for c in self.candidates)

    @property
    def cost(self) -> float:
        return sum(c.cost for c in self.candidates if c.used > 0)


def defer_value(tax: float, years: int = DEFER_YEARS, r: float = DEFER_RETURN) -> float:
    """繰り延べた税 tax を years 年運用した増え分（その増え分にかかる税を引いた後）。"""
    return tax * ((1 + r) ** years - 1) * (1 - TAX_RATE)


def round_trip_cost(c: Candidate, cost_pct: float | None = None) -> float:
    if cost_pct is not None:
        return c.value * cost_pct
    if c.is_fund:
        return c.value * FUND_ROUND_TRIP
    if c.currency == "JPY":
        return c.value * JPY_ROUND_TRIP
    usd = c.value / c.fx_rate if c.fx_rate else c.value
    return 2 * min(usd * US_FEE, US_FEE_CAP_USD) * (c.fx_rate or 1.0)


def review(valuations, account_types: dict[str, str], realized: float | None, realized_source: str,
           year: int, can_buy, cost_pct: float | None = None, min_pnl: float = 1.0,
           dividends: float = 0.0, carryover: float = 0.0) -> Review:
    """valuations: analytics.PositionValuation のリスト。account_types: 口座名 → taxable / nisa など。

    dividends: 課税口座（源泉徴収あり・株式数比例配分方式）で今年受け取った配当等。損と通算できるので、
    損出しで減らせる額が増え、益出しで非課税にできる額が減る。carryover: 繰り越している過去の損。
    """
    out = Review(year, realized, realized_source, dividends=max(dividends, 0.0), carryover=max(carryover, 0.0))
    taxable = []
    for v in valuations:
        p = v.position
        if p.is_cash:
            continue
        kind = account_types.get(p.account)
        if kind is None:
            out.unknown.append(f"{p.symbol}（口座 {p.account} の種類が accounts に無い）")
            continue
        if kind != "taxable":
            out.skipped_nisa += kind == "nisa"
            continue
        if not v.priced:
            out.unknown.append(f"{p.symbol}（価格が取れず評価不能）")
            continue
        if p.currency != "JPY" and p.cost_fx_rate is None:
            # 税務上の取得価額は取得時の為替で円にする。無いと現在の為替で換算され、損益が狂う
            out.unknown.append(f"{p.symbol}（取得時の為替 cost_fx_rate が無く、税務上の損益が出せない）")
            continue
        taxable.append(v)

    if out.mode is None:
        want = None
    else:
        want = "loss" if out.mode == "loss" else "gain"
    room = abs(out.base) if out.base else 0.0
    rows = []
    for v in taxable:
        p = v.position
        if abs(v.pnl_base) < min_pnl:
            continue
        kind = "loss" if v.pnl_base < 0 else "gain"
        if want is not None and kind != want:
            continue
        if want is None and kind != "loss":
            continue          # 今年の実現損益が分からないときは、含み損だけを並べる
        c = Candidate(p.symbol, p.account, kind, v.pnl_base, v.market_value_base, currency=p.currency,
                      fx_rate=v.fx_rate, is_fund=p.is_fund, rebuy_ok=bool(can_buy(p.symbol)))
        c.cost = round_trip_cost(c, cost_pct)
        rows.append(c)
    # 大きい順に、今年の実現損益を埋めるまで使う
    rows.sort(key=lambda c: -abs(c.pnl))
    for c in rows:
        if want is None:
            continue
        c.used = min(abs(c.pnl), room)
        room -= c.used
        if c.used == 0:
            c.note = "今年の分はもう埋まっている" + ("（超えた損は、毎年続けて確定申告すれば 3 年繰り越せる）" if c.kind == "loss" else "")
        elif c.used < abs(c.pnl):
            c.note = "一部だけで足りる（全部売ると超えた分は" + ("繰越控除が要る）" if c.kind == "loss" else "課税される）")
        if not c.rebuy_ok:
            c.note = (c.note + "。" if c.note else "") + "買い戻しは不可（購入可と記録されていない）"
    out.candidates = rows

    if out.dividends:
        out.notes.append("配当等は、源泉徴収ありの特定口座で株式数比例配分方式で受け取った分だけが自動で損と通算される"
                         "（それ以外は確定申告）。米国株の配当で米国に引かれた 10% は戻らない")
    if out.carryover:
        out.notes.append("繰越損失を使うには、今年も確定申告が要る")
    if any(c.is_fund for c in rows):
        out.notes.append("投資信託の税務上の取得価額は個別元本（元本払戻金＝特別分配金を受け取ると下がる）。"
                         "台帳の取得単価と違うことがあるので、証券会社の画面の個別元本で確かめる")
    if out.mode == "loss":
        out.notes.append("損出しは税の繰り延べ: 買い戻すと取得単価が下がり、将来売るときに同じ額の税を払う。"
                         f"{DEFER_YEARS} 年後に売る・年 {DEFER_RETURN:.0%} で運用すると仮定した得は、"
                         f"今年減る税の約 {defer_value(1.0):.0%}")
    elif out.mode == "gain":
        out.notes.append("益出しは、今年の実現損を使い切る（確定申告で繰り越さなければ消える）ので、"
                         "将来の税がその分そのまま減る。売って同じものを買い戻すと取得単価が上がる")
    else:
        out.notes.append("今年の実現損益が分からないので、税がいくら減るかは出していない。"
                         "証券会社の「譲渡益税明細」などの今年の実現損益を --realized で入れる")
    return out


def render(r: Review, pct_only: bool = False) -> str:
    L = [f"── {r.year} 年の損出し・益出しの点検（課税口座のみ） ──", ""]
    if r.realized is None:
        L.append(f"今年の実現損益: 不明（{r.realized_source}）")
    elif pct_only:
        L.append(f"今年の実現損益: {'益' if r.realized > 0 else '損' if r.realized < 0 else '0'}（{r.realized_source}）")
    else:
        L.append(f"今年の実現損益: {r.realized:+,.0f} 円（{r.realized_source}）")
    if r.realized is not None and (r.dividends or r.carryover):
        if pct_only:
            L.append("  配当等・繰越損失を足し引きした今年の土台: " + ("課税あり" if (r.base or 0) > 0 else "損が残る" if (r.base or 0) < 0 else "0"))
        else:
            L.append(f"  + 課税口座の配当等 {r.dividends:,.0f} 円 − 繰越損失 {r.carryover:,.0f} 円 = 今年の土台 {r.base:+,.0f} 円")
    title = {"loss": "含み損（損出しの候補）", "gain": "含み益（益出しの候補）", None: "含み損"}[r.mode]
    if not r.candidates:
        L.append("")
        L.append(f"{title}: なし")
    else:
        L.append("")
        L.append(f"{title}:")
        if pct_only:
            head = "| 銘柄 | 口座 | 含み損益% | 今年の分に使える割合 | 往復コスト% | 買い戻し |"
        else:
            head = "| 銘柄 | 口座 | 含み損益 | 今年の分に使える額 | 税への効果 | 往復コスト | 買い戻し |"
        L.append(head)
        L.append("|" + "---|" * (head.count("|") - 1))
        for c in r.candidates:
            ok = "可" if c.rebuy_ok else "不可"
            cost_basis = c.value - c.pnl
            if pct_only:
                pnl_pct = c.pnl / cost_basis * 100 if cost_basis else 0.0
                use = c.used / abs(c.pnl) * 100 if c.pnl else 0.0
                L.append(f"| {c.symbol} | {c.account} | {pnl_pct:+.1f}% | {use:.0f}% | "
                         f"{c.cost / c.value * 100 if c.value else 0:.1f}% | {ok} |")
            else:
                eff = f"{c.tax_effect:,.0f} 円" if r.mode else "—"
                L.append(f"| {c.symbol} | {c.account} | {c.pnl:+,.0f} 円 | {c.used:,.0f} 円 | {eff} | "
                         f"{c.cost:,.0f} 円 | {ok} |")
            if c.note:
                L.append(f"|  | ↳ {c.note} |" + " |" * (head.count("|") - 3))
        if r.mode and not pct_only:
            L.append("")
            label = "今年減る税（繰り延べ）" if r.mode == "loss" else "将来減る税"
            L.append(f"{label}: 約 {r.tax_effect:,.0f} 円 / 往復の売買コスト: 約 {r.cost:,.0f} 円")
            if r.mode == "loss":
                L.append(f"  繰り延べの得の目安（{DEFER_YEARS} 年・年 {DEFER_RETURN:.0%}）: 約 {defer_value(r.tax_effect):,.0f} 円"
                         " ← これが往復コストより小さいなら、やる意味は薄い")
    if r.unknown:
        L.append("")
        L.append("評価できなかった保有（候補に入れていない）:")
        for u in r.unknown:
            L.append(f"  - {u}")
    if r.skipped_nisa:
        L.append(f"※ NISA の保有 {r.skipped_nisa} 件は対象外（NISA の損は他と通算できず、売っても税は減らない）")
    for n in r.notes:
        L.append(f"※ {n}")
    L.append("※ 税は受渡日の年で決まる。年内に受け渡される最終の売買日は証券会社の案内で確認する")
    L.append("※ 特定口座では、同じ日に同じ銘柄（株式・ETF）を売買すると、約定の順に関係なく「買いが先」とみなして"
             "取得単価を平均するため、損益が一部しか確定しない（益出しも同じ）。投資信託は扱いが異なる。"
             "買い戻しは翌営業日以降が確実（その間の値動きのリスクはある）")
    L.append("※ 別の証券会社の特定口座どうし・源泉徴収なしの口座と通算する、損を繰り越すには確定申告が要る。"
             "申告した所得は国保・後期高齢者医療・介護の保険料や、扶養・配偶者控除の判定に入ることがある"
             "（住民税も所得税と同じ扱いになる）。会社の健康保険に本人として入っていれば影響は小さい")
    L.append("※ 売る・買い戻すの前に /before-trade で点検する。買い戻しは購入なので independence の確認が先")
    return "\n".join(L)
