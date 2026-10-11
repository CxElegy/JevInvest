"""税の効き目の試算（給与所得者・2026 年分）。iDeCo の節税額とふるさと納税の上限の目安。

    python -m src.cli taxplan --salary 8000000 --ideco-monthly 23000

年収は個人の金額なので、ファイルには一切書かない（標準出力だけ）。結果は概算で、
正は源泉徴収票・住民税の決定通知・自治体のシミュレーター。

対象: 2026 年（令和 8 年）分の所得税と、それに基づく 2027 年度（令和 9 年度）の住民税。
収入は給与だけ、年末調整で完結する会社員を想定する。

ルールと出典（確認日はすべて 2026-10-11）:

  給与所得控除（令和 8・9 年分）と 660 万円未満の端数処理
    - 国税庁 No.1410 [令和8年4月1日現在法令等]
      https://www.nta.go.jp/taxes/shiraberu/taxanswer/shotoku/1410.htm
      220 万以下 74 万 / 360 万以下 30%+8 万 / 660 万以下 20%+44 万 / 850 万以下 10%+110 万 / 超 195 万
    - 国税庁「令和8年分 年末調整のしかた」の算出表（年調給与額は 220 万〜660 万未満で
      (総額 − 220 万) を 4,000 円で割った余りを切り捨て。Ａ×70%−8 万、Ａ×80%−44 万 ほか）
      https://www.nta.go.jp/publication/pamph/gensen/nencho2026/pdf/204.pdf
    - 住民税も 220 万円超は同じ（千葉市 令和9年度の改正点）
      https://www.city.chiba.jp/zaiseikyoku/zeimu/kazeikanri/zeiseikaiseijuminzei2027.html

  所得税の基礎控除（令和 8・9 年分。令和 8 年度改正で 2026-12-01 施行、令和 8 年分から適用）
    - 国税庁「源泉所得税の改正のあらまし（令和8年4月）」
      https://www.nta.go.jp/publication/pamph/gensen/2026kaisei.pdf
    - 財務省 令和8年度税制改正の大綱 https://www.mof.go.jp/tax_policy/tax_reform/outline/fy2026/08taikou_01.htm
      合計所得 489 万以下 104 万（本則 62 万 + 特例 42 万）/ 655 万以下 67 万（62 + 5）/
      2,350 万以下 62 万 / 2,400 万以下 48 万 / 2,450 万以下 32 万 / 2,500 万以下 16 万 / 超 0
      ※ 2025 年度改正の 95/88/68/63/58 万は令和 8 年分では使わない（依頼文の想定から変わっている）

  住民税の基礎控除 43 万円（改正なし）と調整控除
    - 千葉市（上記）: 基礎控除の見直しは所得税のみ
    - 練馬区 調整控除（更新 2026-02-01）
      https://www.city.nerima.tokyo.jp/kurashi/zei/jyuminzei/zeigakukojo/choseikojo.html
      人的控除差: 基礎 5 万（合計所得 2,400 万以下。実際の差額にかかわらず 5 万）、
      配偶者 5/4/2 万（本人の合計所得 900/950/1,000 万以下）、一般扶養 5 万、特定扶養 18 万。
      調整控除 = 課税所得 200 万以下: min(差, 課税所得)×5% / 超: (差 − (課税所得 − 200 万))×5%、
      最低 2,500 円。合計所得 2,500 万超は無し

  所得税の速算表・復興特別所得税
    - 国税庁 No.2260 [令和8年4月1日現在法令等]
      https://www.nta.go.jp/taxes/shiraberu/taxanswer/shotoku/2260.htm
      課税所得は千円未満切捨て。2026 年分は復興特別所得税 2.1%（2027 年以後は 1.1% + 防衛特別所得税 1%）

  配偶者控除・扶養控除（任意。単純なケースだけ）
    - 国税庁 年末調整のしかた（上記 204.pdf）: 一般扶養 38 万、特定扶養 63 万、配偶者控除 最高 38 万
    - 配偶者の合計所得の要件 62 万以下（令和 8 年分。上記あらまし）
    - 練馬区（上記）: 住民税 配偶者 33/22/11 万、一般扶養 33 万、特定扶養 45 万。所得税 38/26/13 万

  iDeCo の拠出限度額
    - 厚生労働省「私的年金制度、iDeCoの改正のポイント」https://www.mhlw.go.jp/content/10600000/001690086.pdf
      現在: 企業年金なし 2.3 万 / あり 2.0 万（企業年金等と合わせて 5.5 万）。
      2026 年 12 月分から: 企業年金なし 6.2 万 / あり 企業年金等と合わせて 6.2 万
    - 掛金は全額「小規模企業共済等掛金控除」（所得税・住民税とも）

  ふるさと納税
    - 総務省 税金の控除について
      https://www.soumu.go.jp/main_sosiki/jichi_zeisei/czaisei/czaisei_seido/furusato/mechanism/deduction.html
      特例分 = (寄附 − 2,000)×(90% − 所得税率×1.021)、上限は住民税所得割額の 2 割。
      → 上限の目安 = 所得割額 × 20% ÷ (90% − 所得税率×1.021) + 2,000
      所得税率は「住民税の課税総所得 − 人的控除差調整額」で決める
    - 人的控除差調整額 = 人的控除差の合計 + (所得税の基礎控除 − 48 万)（令和 7 年度改正。大磯町の資料）
      https://www.town.oiso.kanagawa.jp/material/files/group/10/zeiseikaisei08.pdf
      → 結果として、所得税の課税所得とほぼ同じ金額で税率が決まる
    - ワンストップ特例: 確定申告不要の給与所得者で 5 団体以内、翌年 1 月 10 日まで
      https://www.soumu.go.jp/main_content/000362730.pdf
    - 2027 年以後の寄附から特例控除に定額上限（193 万円）が入る。2026 年の寄附には関係しない
      https://www.yamada-partners.jp/hubfs/zeikai/2026/k07.pdf

未確認として出力に書くもの:
  - 社会保険料は既定で年収 × 15% の概算（料率の内訳と 2026 年度の料率は確認していない。
    標準報酬の上限があるため高年収ほど実際の比率は下がる）。源泉徴収票の「社会保険料等の金額」を入れる
  - 人的控除差調整額の (所得税の基礎控除 − 48 万) を、令和 8 年度改正後の金額（104/67/62 万）に
    そのまま当てはめてよいか（令和 9 年度住民税の資料で未確認）
  - iDeCo の 2026 年中の払込回数（個人払込は 12 月分が翌年 1 月引落のため、12 月分の新上限は
    2027 年の控除になる見込み。払込証明書で確認）
  - iDeCo の受け取り時の課税（退職所得控除・公的年金等控除）はモデル化しない

モデル化しないもの: 配偶者特別控除、70 歳以上・障害者などの控除、所得金額調整控除、
生命保険料控除、医療費控除、住宅ローン控除、均等割・森林環境税、住民税の非課税限度。
"""

from __future__ import annotations

from dataclasses import dataclass, field

TAX_YEAR = 2026
SOCIAL_INSURANCE_RATIO = 0.15      # 既定の社会保険料（年収比）。概算
FUKKO = 0.021                      # 復興特別所得税
RESIDENT_RATE = 0.10               # 住民税の所得割（標準税率。市町村 6% + 道府県 4%）

# iDeCo の月額上限（厚労省）
IDECO_CAP_NOW_NO_PENSION = 23_000
IDECO_CAP_NOW_WITH_PENSION = 20_000
IDECO_COMBINED_NOW_WITH_PENSION = 55_000
IDECO_CAP_NEW = 62_000             # 2026 年 12 月分から。企業年金ありは企業年金等と合算

# 所得税の速算表（上限, 税率, 控除額）。国税庁 No.2260
_BRACKETS = [
    (1_949_000, 0.05, 0),
    (3_299_000, 0.10, 97_500),
    (6_949_000, 0.20, 427_500),
    (8_999_000, 0.23, 636_000),
    (17_999_000, 0.33, 1_536_000),
    (39_999_000, 0.40, 2_796_000),
    (float("inf"), 0.45, 4_796_000),
]


# ── 控除 ──

def salary_income(salary: int) -> int:
    """給与所得（令和 8 年分）。660 万円未満は年末調整の算出表の 4,000 円単位に合わせる。"""
    s = int(salary)
    if s < 741_000:
        return 0
    if s < 2_191_000:
        return s - 740_000
    if s < 2_200_000:   # 特例の端数表（219.1 万〜220 万未満）
        return 1_451_000 if s < 2_193_000 else 1_453_000 if s < 2_196_000 else 1_456_000
    if s < 6_600_000:
        a = s - (s - 2_200_000) % 4_000
        return a * 7 // 10 - 80_000 if a < 3_600_000 else a * 8 // 10 - 440_000
    if s < 8_500_000:
        return s * 9 // 10 - 1_100_000
    return s - 1_950_000


def basic_deduction_income_tax(total_income: int) -> int:
    """所得税の基礎控除（令和 8・9 年分。本則 62 万 + 特例）。"""
    for limit, amount in ((4_890_000, 1_040_000), (6_550_000, 670_000),
                          (23_500_000, 620_000), (24_000_000, 480_000),
                          (24_500_000, 320_000), (25_000_000, 160_000)):
        if total_income <= limit:
            return amount
    return 0


def basic_deduction_resident(total_income: int) -> int:
    """住民税の基礎控除（改正なし）。"""
    for limit, amount in ((24_000_000, 430_000), (24_500_000, 290_000), (25_000_000, 150_000)):
        if total_income <= limit:
            return amount
    return 0


def _spouse_tier(total_income: int) -> int | None:
    """配偶者控除の段階（本人の合計所得）。0: 900 万以下、1: 950 万以下、2: 1,000 万以下。"""
    for i, limit in enumerate((9_000_000, 9_500_000, 10_000_000)):
        if total_income <= limit:
            return i
    return None


@dataclass
class Household:
    spouse: bool = False               # 一般の控除対象配偶者（70 歳未満、合計所得 62 万以下）
    dependents: int = 0                # 一般の控除対象扶養親族（16〜18 歳、23〜69 歳）
    specific_dependents: int = 0       # 特定扶養親族（19〜22 歳）

    def personal(self, total_income: int) -> tuple[int, int, int]:
        """(所得税の控除, 住民税の控除, 調整控除用の人的控除差) ※基礎控除を除く。"""
        it = 380_000 * self.dependents + 630_000 * self.specific_dependents
        rt = 330_000 * self.dependents + 450_000 * self.specific_dependents
        diff = 50_000 * self.dependents + 180_000 * self.specific_dependents
        tier = _spouse_tier(total_income) if self.spouse else None
        if tier is not None:
            it += (380_000, 260_000, 130_000)[tier]
            rt += (330_000, 220_000, 110_000)[tier]
            diff += (50_000, 40_000, 20_000)[tier]
        return it, rt, diff

    @property
    def empty(self) -> bool:
        return not (self.spouse or self.dependents or self.specific_dependents)


# ── 税額 ──

def bracket(taxable: int) -> tuple[float, int]:
    for upper, rate, ded in _BRACKETS:
        if taxable <= upper:
            return rate, ded
    raise AssertionError


def income_tax(taxable: int) -> int:
    """所得税 + 復興特別所得税（百円未満切捨て）。taxable は千円未満切捨て済み。"""
    if taxable <= 0:
        return 0
    rate, ded = bracket(taxable)
    base = taxable * round(rate * 100) // 100 - ded      # 浮動小数の誤差を避けて整数で
    return base * 1021 // 1000 // 100 * 100


def _floor1000(x: float) -> int:
    return max(0, int(x) // 1000 * 1000)


def adjustment_credit(taxable_resident: int, diff: int, total_income: int) -> int:
    """住民税の調整控除。"""
    if total_income > 25_000_000:
        return 0
    if taxable_resident <= 2_000_000:
        v = min(diff, taxable_resident) * 0.05
    else:
        v = (diff - (taxable_resident - 2_000_000)) * 0.05
    return int(max(v, 2_500))


@dataclass
class TaxResult:
    salary: int
    social_insurance: int
    social_insurance_estimated: bool
    ideco_annual: int
    household: Household
    salary_income: int = 0
    basic_it: int = 0
    basic_rt: int = 0
    personal_it: int = 0
    personal_rt: int = 0
    personal_diff: int = 0
    taxable_it: int = 0
    taxable_rt: int = 0
    income_tax: int = 0
    marginal_rate: float = 0.0
    adjustment: int = 0
    resident_shotokuwari: int = 0
    furusato_rate: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def total_tax(self) -> int:
        return self.income_tax + self.resident_shotokuwari


def compute(salary: int, social_insurance: int | None = None, ideco_annual: int = 0,
            household: Household | None = None) -> TaxResult:
    hh = household or Household()
    est = social_insurance is None
    si = int(salary * SOCIAL_INSURANCE_RATIO) if est else int(social_insurance)
    r = TaxResult(salary=int(salary), social_insurance=si, social_insurance_estimated=est,
                  ideco_annual=int(ideco_annual), household=hh)
    gi = salary_income(salary)
    r.salary_income = gi
    r.basic_it = basic_deduction_income_tax(gi)
    r.basic_rt = basic_deduction_resident(gi)
    r.personal_it, r.personal_rt, r.personal_diff = hh.personal(gi)
    common = si + r.ideco_annual       # 社会保険料控除 + 小規模企業共済等掛金控除
    r.taxable_it = _floor1000(gi - common - r.basic_it - r.personal_it)
    r.taxable_rt = _floor1000(gi - common - r.basic_rt - r.personal_rt)
    r.income_tax = income_tax(r.taxable_it)
    r.marginal_rate = bracket(r.taxable_it)[0] if r.taxable_it > 0 else 0.0

    # 住民税の所得割（調整控除後、百円未満切捨て）
    basic_diff = {430_000: 50_000, 290_000: 30_000, 150_000: 10_000}.get(r.basic_rt, 0)
    diff = basic_diff + r.personal_diff
    r.adjustment = adjustment_credit(r.taxable_rt, diff, gi) if r.taxable_rt > 0 else 0
    gross = r.taxable_rt // 10                     # 10%
    r.resident_shotokuwari = max(0, gross - r.adjustment) // 100 * 100

    # ふるさと納税の特例分の税率: 住民税の課税総所得 − 人的控除差調整額
    # 人的控除差調整額 = 人的控除差の合計 + (所得税の基礎控除 − 48 万)
    adj = diff + (r.basic_it - 480_000)
    base = r.taxable_rt - adj
    r.furusato_rate = bracket(base)[0] if base > 0 else 0.0
    return r


def furusato_limit(r: TaxResult) -> int:
    """自己負担 2,000 円で済む寄附額の目安（百円未満切捨て）。"""
    if r.resident_shotokuwari <= 0:
        return 0
    v = r.resident_shotokuwari * 0.2 / (0.9 - r.furusato_rate * (1 + FUKKO)) + 2_000
    return int(v) // 100 * 100


# ── iDeCo ──

@dataclass
class IdecoRoom:
    has_corporate_pension: bool
    company_monthly: int
    cap_now: int        # 2026 年中（11 月分まで）の月額上限
    cap_new: int        # 2026 年 12 月分からの月額上限


def ideco_room(company_dc_monthly: int | None) -> IdecoRoom:
    """company_dc_monthly が None なら企業年金なし。0 以上なら企業年金あり（DC 事業主掛金 + DB 等の相当額）。"""
    if company_dc_monthly is None:
        return IdecoRoom(False, 0, IDECO_CAP_NOW_NO_PENSION, IDECO_CAP_NEW)
    c = int(company_dc_monthly)
    now = max(0, min(IDECO_CAP_NOW_WITH_PENSION, IDECO_COMBINED_NOW_WITH_PENSION - c))
    return IdecoRoom(True, c, now, max(0, IDECO_CAP_NEW - c))


def ideco_saving(salary: int, ideco_annual: int, social_insurance: int | None = None,
                 household: Household | None = None) -> tuple[int, int, int]:
    """iDeCo の有無で税額を計算し直した差。(所得税, 住民税所得割, 合計)。税率の段差もそのまま反映。"""
    a = compute(salary, social_insurance, 0, household)
    b = compute(salary, social_insurance, ideco_annual, household)
    d_it = a.income_tax - b.income_tax
    d_rt = a.resident_shotokuwari - b.resident_shotokuwari
    return d_it, d_rt, d_it + d_rt


# ── 出力 ──

def _yen(v: float) -> str:
    return f"{v:,.0f} 円"


def render(salary: int, social_insurance: int | None = None, ideco_monthly: int = 0,
           company_dc_monthly: int | None = None, household: Household | None = None) -> str:
    hh = household or Household()
    ideco_annual = int(ideco_monthly) * 12
    base = compute(salary, social_insurance, 0, hh)
    withi = compute(salary, social_insurance, ideco_annual, hh)
    room = ideco_room(company_dc_monthly)
    L: list[str] = []
    w = L.append

    w(f"# 税の効き目の試算（{TAX_YEAR} 年分の所得税・{TAX_YEAR + 1} 年度の住民税、概算）")
    w("")
    w("年収は画面に出すだけで、ファイルには保存していない。正は源泉徴収票と自治体の計算。")
    w("")
    w("## 1. 課税所得と税率（iDeCo なし）")
    w("")
    si_label = (f"{_yen(base.social_insurance)}（概算: 年収 × {SOCIAL_INSURANCE_RATIO:.0%}。"
                "料率は未確認。源泉徴収票の値を --social-insurance で入れる）"
                if base.social_insurance_estimated else _yen(base.social_insurance))
    rows = [
        ("給与収入", _yen(base.salary)),
        ("給与所得（給与所得控除後）", _yen(base.salary_income)),
        ("社会保険料控除", si_label),
        ("基礎控除（所得税 / 住民税）", f"{_yen(base.basic_it)} / {_yen(base.basic_rt)}"),
    ]
    if not hh.empty:
        rows.append(("配偶者・扶養控除（所得税 / 住民税）",
                     f"{_yen(base.personal_it)} / {_yen(base.personal_rt)}"))
    rows += [
        ("課税所得（所得税 / 住民税）", f"{_yen(base.taxable_it)} / {_yen(base.taxable_rt)}"),
        ("所得税 + 復興特別所得税", _yen(base.income_tax)),
        ("住民税 所得割（調整控除後）", f"{_yen(base.resident_shotokuwari)}（調整控除 {_yen(base.adjustment)}）"),
        ("限界税率", f"所得税 {base.marginal_rate:.0%} × 1.021 + 住民税 10% = "
                   f"{base.marginal_rate * (1 + FUKKO) + RESIDENT_RATE:.2%}"),
    ]
    w("| 項目 | 金額 |")
    w("|---|---|")
    for k, v in rows:
        w(f"| {k} | {v} |")
    w("")
    if hh.empty:
        w("※ 配偶者控除・扶養控除などは入れていない（--spouse / --dependents / --specific-dependents で指定）。")
    w("※ 配偶者特別控除、70 歳以上・障害者の控除、所得金額調整控除、生命保険料控除、医療費控除、"
      "住宅ローン控除はモデル化していない。")
    if base.salary > 8_500_000 and (hh.dependents or hh.specific_dependents):
        w("※ 23 歳未満の扶養親族がいると所得金額調整控除（最大 15 万円）が使えるが、反映していない（税は少し高めに出る）。")
    w("")

    # iDeCo
    w("## 2. iDeCo（掛金は全額所得控除）")
    w("")
    pension = (f"企業年金あり（企業年金等の掛金 月 {_yen(room.company_monthly)}）"
               if room.has_corporate_pension else "企業年金なし")
    w(f"- 区分: {pension}")
    w(f"- 月額の上限: 2026 年 11 月分まで {_yen(room.cap_now)} → "
      f"**2026 年 12 月分から {_yen(room.cap_new)}**（厚労省。企業年金ありは企業年金等と合わせて 62,000 円）")
    if ideco_monthly:
        d_it, d_rt, d = ideco_saving(salary, ideco_annual, social_insurance, hh)
        w(f"- 入力: 月 {_yen(ideco_monthly)} × 12 = 年 {_yen(ideco_annual)}")
        if ideco_monthly > room.cap_now:
            w(f"  - ⚠ 2026 年中の払込の上限（月 {_yen(room.cap_now)}）を超えている。"
              "超えた分は拠出できないので、この試算はそのまま上限超えで計算している")
        w(f"- 節税額（{TAX_YEAR} 年分、税額を計算し直した差）: 所得税 {_yen(d_it)} + 住民税 {_yen(d_rt)} = "
          f"**{_yen(d)}**（掛金の {d / ideco_annual:.1%}）")
        simple = ideco_annual * (base.marginal_rate * (1 + FUKKO) + RESIDENT_RATE)
        if abs(simple - d) >= 1_000:
            w(f"  - 掛金 × 限界税率の単純計算（{_yen(simple)}）とずれるのは、控除で税率の段を"
              "またぐ・住民税の調整控除が変わる・端数処理のため")
    new_annual = room.cap_new * 12
    if new_annual:
        _, _, d_new = ideco_saving(salary, new_annual, social_insurance, hh)
        w(f"- 参考: 新上限 月 {_yen(room.cap_new)} を 12 か月拠出した場合（2027 年分の目安。"
          f"2026 年分と同じ年収・税制で計算）: 年 {_yen(new_annual)} → 節税 **{_yen(d_new)}**")
    w("- 2026 年中に控除されるのは 2026 年中に払い込んだ掛金。個人払込は 12 月分が翌年 1 月引落のため、"
      "12 月分の増額は 2027 年の控除になる見込み（未確認。払込証明書で確認）")
    w("- **60 歳まで引き出せない。** 受け取り時の課税（退職所得控除・公的年金等控除）はこの試算に入っていない。"
      "節税額は「入れるときの得」であって、受け取り時に一部が課税で戻りうる")
    w("- 先に生活防衛資金と、60 歳まで使わないと決められる額かを確かめる。NISA はいつでも引き出せる")
    w("")

    # ふるさと納税
    w("## 3. ふるさと納税の上限の目安（自己負担 2,000 円で済む額）")
    w("")
    w("目安 = 住民税所得割額 × 20% ÷ (90% − 所得税率 × 1.021) + 2,000 円（総務省の控除の仕組みから）")
    w("")
    w("| ケース | 住民税所得割 | 特例分の所得税率 | 上限の目安 |")
    w("|---|---:|---:|---:|")
    w(f"| iDeCo なし | {_yen(base.resident_shotokuwari)} | {base.furusato_rate:.0%} | "
      f"**{_yen(furusato_limit(base))}** |")
    if ideco_monthly:
        w(f"| iDeCo 月 {ideco_monthly:,} 円あり | {_yen(withi.resident_shotokuwari)} | "
          f"{withi.furusato_rate:.0%} | **{_yen(furusato_limit(withi))}** |")
    w("")
    w("- iDeCo を始める・増やすと上限は下がる。先に iDeCo を決めてから寄附額を決める")
    w("- **ワンストップ特例**: 確定申告が不要な給与所得者で、寄附先が 5 団体以内。"
      "寄附ごとに申請書を出し、変更届も含めて翌年 1 月 10 日まで（2026 年分は 2027-01-10）")
    w("- **確定申告**: 6 団体以上、医療費控除などで申告する年はこちら（申告するとワンストップの申請は無効）。"
      "申告期限は翌年 3 月 15 日")
    w("- 上限ぎりぎりは避け、1〜2 割の余裕を残す（年収・社会保険料・控除の見込みが変わるため）")
    w("- 2027 年以後の寄附からは特例分に定額上限（193 万円）が入る。2026 年の寄附には関係しない")
    w("- 最終確認は 12 月の源泉徴収票の数字で、自治体や総務省のポータルから辿れるシミュレーターを使う")
    w("")
    w("## 未確認・前提")
    w("")
    if base.social_insurance_estimated:
        w("- 社会保険料は年収 × 15% の概算（料率の内訳は未確認。標準報酬の上限があるため、"
          "高年収ほど実際の比率は下がり、税はこの試算より高くなる）")
    w("- 人的控除差調整額の「所得税の基礎控除 − 48 万円」に令和 8 年度改正後の額を当てはめた"
      "（令和 9 年度住民税の資料では未確認）")
    w("- 住民税は標準税率 10%。均等割・森林環境税、市と県の別々の端数処理は入れていない")
    w("- 収入が給与だけ・年末調整で完結する前提。副業・配当の申告がある年は数字が変わる")
    return "\n".join(L)
