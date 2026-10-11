"""税の試算（2026 年分）。期待値は src/taxplan.py の docstring の出典から手で計算したもの。

社会保険料は明示して渡す（既定の 15% 概算に依存させない）。
"""


from src.cli import main
from src.taxplan import (
    Household,
    basic_deduction_income_tax,
    compute,
    furusato_limit,
    ideco_room,
    ideco_saving,
    salary_income,
)


def test_salary_income_follows_2026_table():
    # 国税庁 No.1410（令和 8 年分）と 年末調整のしかた 204.pdf の算出表
    assert salary_income(2_000_000) == 2_000_000 - 740_000          # 最低保障 74 万
    # 204.pdf の計算例: 531 万 → 年調給与額 530.8 万 → ×80% − 44 万
    assert salary_income(5_310_000) == 5_308_000 * 8 // 10 - 440_000 == 3_806_400
    assert salary_income(8_000_000) == 8_000_000 * 9 // 10 - 1_100_000 == 6_100_000
    assert salary_income(12_000_000) == 12_000_000 - 1_950_000      # 上限 195 万


def test_basic_deduction_uses_2026_amounts_not_2025():
    # 令和 8・9 年分: 489 万以下 104 万 / 655 万以下 67 万 / 2,350 万以下 62 万（国税庁 あらまし）
    assert basic_deduction_income_tax(3_560_000) == 1_040_000
    assert basic_deduction_income_tax(4_890_000) == 1_040_000
    assert basic_deduction_income_tax(4_890_001) == 670_000
    assert basic_deduction_income_tax(6_550_001) == 620_000
    assert basic_deduction_income_tax(25_000_001) == 0


def test_salary_5m():
    # 給与所得 = 500 万 × 80% − 44 万 = 356 万（4,000 円単位の端数なし）
    # 所得税: 356 万 − 社保 75 万 − 基礎 104 万 = 177 万 → 5% = 88,500 × 1.021 = 90,358 → 90,300
    # 住民税: 356 万 − 75 万 − 43 万 = 238 万 → 238,000 − 調整控除 2,500（(5 万 − 38 万) が負 → 最低額）
    r = compute(5_000_000, social_insurance=750_000)
    assert r.salary_income == 3_560_000
    assert r.taxable_it == 1_770_000
    assert r.income_tax == 90_300
    assert r.marginal_rate == 0.05
    assert r.taxable_rt == 2_380_000
    assert r.resident_shotokuwari == 235_500
    # ふるさと: 238 万 − (5 万 + (104 万 − 48 万)) = 177 万 → 5%
    # 235,500 × 0.2 ÷ (0.9 − 0.05105) + 2,000 = 47,100 ÷ 0.84895 + 2,000 = 57,480 → 57,400
    assert r.furusato_rate == 0.05
    assert furusato_limit(r) == 57_400


def test_salary_8m():
    # 給与所得 610 万、基礎 67 万（合計所得 489 万超 655 万以下）
    # 所得税: 610 − 120 − 67 = 423 万 → 20%: 846,000 − 427,500 = 418,500 × 1.021 = 427,288 → 427,200
    # 住民税: 610 − 120 − 43 = 447 万 → 447,000 − 2,500 = 444,500
    # ふるさと: 447 万 − (5 万 + 19 万) = 423 万 → 20%
    # 444,500 × 0.2 ÷ (0.9 − 0.2042) + 2,000 = 88,900 ÷ 0.6958 + 2,000 = 129,766 → 129,700
    r = compute(8_000_000, social_insurance=1_200_000)
    assert r.basic_it == 670_000
    assert r.taxable_it == 4_230_000
    assert r.income_tax == 427_200
    assert r.resident_shotokuwari == 444_500
    assert r.furusato_rate == 0.20
    assert furusato_limit(r) == 129_700

    # iDeCo 月 23,000（年 27.6 万）: 同じ 20% の段の中
    # 所得税: 395.4 万 → 790,800 − 427,500 = 363,300 × 1.021 = 370,929 → 370,900（差 56,300）
    # 住民税: 419.4 万 → 419,400 − 2,500 = 416,900（差 27,600）
    d_it, d_rt, d = ideco_saving(8_000_000, 276_000, social_insurance=1_200_000)
    assert (d_it, d_rt, d) == (56_300, 27_600, 83_900)
    # ふるさと（iDeCo あり）: 416,900 × 0.2 ÷ 0.6958 + 2,000 = 119,833 + 2,000 = 121,833 → 121,800
    assert furusato_limit(compute(8_000_000, 1_200_000, 276_000)) == 121_800


def test_salary_12m():
    # 給与所得 1,005 万、基礎 62 万
    # 所得税: 1,005 − 180 − 62 = 763 万 → 23%: 1,754,900 − 636,000 = 1,118,900 × 1.021
    #        = 1,142,396 → 1,142,300
    # 住民税: 1,005 − 180 − 43 = 782 万 → 782,000 − 2,500 = 779,500
    # ふるさと: 782 万 − (5 万 + 14 万) = 763 万 → 23%
    # 779,500 × 0.2 ÷ (0.9 − 0.23483) + 2,000 = 155,900 ÷ 0.66517 + 2,000 = 236,376 → 236,300
    r = compute(12_000_000, social_insurance=1_800_000)
    assert r.basic_it == 620_000
    assert r.taxable_it == 7_630_000
    assert r.income_tax == 1_142_300
    assert r.marginal_rate == 0.23
    assert r.resident_shotokuwari == 779_500
    assert furusato_limit(r) == 236_300
    # iDeCo 27.6 万: 735.4 万 → 1,691,420 − 636,000 = 1,055,420 × 1.021 = 1,077,583 → 1,077,500
    assert ideco_saving(12_000_000, 276_000, social_insurance=1_800_000) == (64_800, 27_600, 92_400)


def test_ideco_crossing_bracket_is_recomputed_not_marginal():
    # 年収 700 万、社保 105 万: 給与所得 520 万、基礎 67 万
    # 所得税の課税所得 348 万（20%）→ iDeCo 27.6 万で 320.4 万（10%）。330 万の段をまたぐ
    #   なし: 696,000 − 427,500 = 268,500 × 1.021 = 274,138 → 274,100
    #   あり: 320,400 −  97,500 = 222,900 × 1.021 = 227,580 → 227,500   差 46,600
    #   検算: (18 万 × 20% + 9.6 万 × 10%) × 1.021 = 46,557
    # 単純計算 27.6 万 × 20.42% = 56,359 は過大
    # 住民税: 372 万 → 344.4 万。369,500 − 341,900 = 27,600
    d_it, d_rt, d = ideco_saving(7_000_000, 276_000, social_insurance=1_050_000)
    assert (d_it, d_rt, d) == (46_600, 27_600, 74_200)
    assert d < 276_000 * (0.20 * 1.021 + 0.10)

    # ふるさとの特例分の税率も 20% → 10% に下がる
    #   なし: 369,500 × 0.2 ÷ 0.6958 + 2,000 = 108,208 → 108,200
    #   あり: 341,900 × 0.2 ÷ 0.7979 + 2,000 = 87,699 → 87,600
    a = compute(7_000_000, 1_050_000)
    b = compute(7_000_000, 1_050_000, 276_000)
    assert (a.furusato_rate, b.furusato_rate) == (0.20, 0.10)
    assert furusato_limit(a) == 108_200
    assert furusato_limit(b) == 87_600


def test_household_deductions():
    # 年収 800 万・社保 120 万・配偶者 + 特定扶養 1 人
    # 所得税: 610 − 120 − 67 − 38 − 63 = 322 万 → 10%: 322,000 − 97,500 = 224,500 × 1.021 = 229,214 → 229,200
    # 住民税: 610 − 120 − 43 − 33 − 45 = 369 万 → 369,000。人的控除差 5 + 5 + 18 = 28 万
    #   調整控除 (28 万 − 169 万) が負 → 2,500 → 366,500
    # ふるさと: 369 万 − (28 万 + 19 万) = 322 万 → 10%（所得税の課税所得と一致）
    r = compute(8_000_000, 1_200_000, household=Household(spouse=True, specific_dependents=1))
    assert r.taxable_it == 3_220_000
    assert r.income_tax == 229_200
    assert r.resident_shotokuwari == 366_500
    assert r.furusato_rate == 0.10


def test_ideco_room_with_and_without_corporate_pension():
    none = ideco_room(None)
    assert (none.cap_now, none.cap_new) == (23_000, 62_000)
    # 企業年金等 3 万: 現在 min(2 万, 5.5 万 − 3 万) = 2 万、新 6.2 万 − 3 万 = 3.2 万
    r = ideco_room(30_000)
    assert (r.cap_now, r.cap_new) == (20_000, 32_000)
    r = ideco_room(40_000)
    assert (r.cap_now, r.cap_new) == (15_000, 22_000)
    assert ideco_room(70_000).cap_new == 0


def test_default_social_insurance_is_labeled_estimate():
    r = compute(8_000_000)
    assert r.social_insurance == 1_200_000 and r.social_insurance_estimated


def test_cli_prints_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["taxplan", "--salary", "8000000", "--ideco-monthly", "23000"])
    out = capsys.readouterr().out
    assert "概算" in out and "ワンストップ" in out and "未確認" in out
    assert "60 歳まで引き出せない" in out
    assert list(tmp_path.iterdir()) == []
