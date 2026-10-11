"""証券会社 CSV の取り込み。合成した CSV だけを使い、ネットワークには出ない。"""

import pytest
import yaml

from src import importers
from src.cli import main
from src.transactions import load as load_tx

SBI_JP = """\
約定履歴照会
口座番号,123-4567890
"約定日","銘柄","銘柄コード","市場","取引","期限","預り","課税","約定数量","約定単価","手数料/諸経費等","税額","受渡日","受渡金額/決済損益"
"2026/04/02","トヨタ自動車","7203","東証","株式現物買","当日","特定","申告","100","2,800","110","--","2026/04/04","280,110"
"2026/05/10","トヨタ自動車","7203","東証","株式現物売","当日","特定","申告","40","3,100","110","--","2026/05/12","123,890"
"2026/05/11","ソニーグループ","6758","東証","信用新規買","当日","特定","申告","100","3,000","0","--","2026/05/13","300,000"
"2026/06/01","ソニーグループ","6758","東証","株式現物買","当日","NISA(成長投資枠)","非課税","10","3,200","0","--","2026/06/03","32,000"
"""

RAKUTEN_US = """\
約定日,受渡日,ティッカー,銘柄名,口座,取引区分,売買区分,信用区分,弁済期限,決済通貨,数量［株］,単価［USドル］,約定代金［USドル］,為替レート,手数料［USドル］,税金［USドル］,受渡金額［USドル］,受渡金額［円］
2026/04/02,2026/04/04,VT,VANGUARD TOTAL WORLD,NISA成長投資枠,現物,買付,,,円,10,120.50,1205.00,150.20,0.00,0.00,1205.00,180991
2026/07/01,2026/07/03,NVDA,NVIDIA CORP,特定,現物,買付,,,米ドル,5,180.00,900.00,,2.00,0.20,902.20,
2026/08/01,2026/08/05,AAPL,APPLE INC,特定,現物,買付,,,米ドル,3,200.00,600.00,155.00,1.00,0.10,601.10,
"""

RAKUTEN_FUND = """\
約定日,受渡日,ファンド名,分配金,口座,取引,買付方法,数量［口］,単価,経費,為替レート,受渡金額/(ポイント利用)[円]
2026/04/10,2026/04/11,ｅＭＡＸＩＳ　Ｓｌｉｍ　全世界株式（オール・カントリー）,再投資,NISAつみたて投資枠,買付,カード,"66,667","30,000",0,,"200,000"
2026/05/10,2026/05/11,ｅＭＡＸＩＳ　Ｓｌｉｍ　全世界株式（オール・カントリー）,再投資,NISAつみたて投資枠,再投資,,"100","31,000",0,,"310"
2026/06/10,2026/06/11,謎の投信,再投資,特定,解約,,"10,000","15,000",0,,"15,000"
"""


def test_sbi_detects_header_after_preamble_and_skips_margin():
    res = importers.convert(SBI_JP)
    assert res.preset.key == "sbi_jp"
    assert [(t["type"], t["symbol"], t["account"]) for t in res.transactions] == [
        ("buy", "7203.T", "特定口座"),
        ("sell", "7203.T", "特定口座"),
        ("buy", "6758.T", "NISA"),
    ]
    first = res.transactions[0]
    assert first["quantity"] == 100 and first["price"] == 2800 and first["fee"] == 110
    assert first["asset_class"] == "jp_equity"
    assert "fx_rate" not in first
    # 信用取引は取り込まず、件数を報告する（黙って捨てない）
    assert any("信用新規買 1 件" in w for w in res.warnings)
    assert any("資産クラスを既定値" in w for w in res.warnings)


def test_rakuten_us_requires_fx_and_converts_fee_to_yen():
    res = importers.convert(RAKUTEN_US)
    assert res.preset.key == "rakuten_us"
    syms = [t["symbol"] for t in res.transactions]
    assert syms == ["VT", "AAPL"]      # NVDA は為替レートが空なので飛ばす
    assert any("為替レートが空" in w for w in res.warnings)
    aapl = res.transactions[1]
    assert aapl["currency"] == "USD" and aapl["fx_rate"] == 155.0
    assert aapl["fee"] == pytest.approx(155.0)   # 1 USD × 155
    # 決済通貨が「円」でも、約定単価は USD のまま。為替は必須
    vt = res.transactions[0]
    assert vt["currency"] == "USD" and vt["fx_rate"] == 150.2 and vt["account"] == "NISA"


def test_rakuten_fund_uses_map_and_per_unit_price():
    mapping = {"funds": {"eMAXIS Slim 全世界株式(オール・カントリー)":
                         {"symbol": "FUND-ALLCOUNTRY", "asset_class": "developed_equity"}}}
    res = importers.convert(RAKUTEN_FUND, mapping=mapping)
    assert res.preset.key == "rakuten_fund"
    assert len(res.transactions) == 2     # 再投資の行は取り込まない
    buy, sell = res.transactions
    assert buy["symbol"] == "FUND-ALLCOUNTRY" and buy["asset_class"] == "developed_equity"
    assert buy["price"] == pytest.approx(3.0)   # 30,000 円 / 万口 → 1 口 3 円
    assert sell["type"] == "sell" and sell["symbol"].startswith("FUND-")
    assert sell["asset_class"] == ""
    assert any("import_map に無い投信" in w for w in res.warnings)
    assert any("再投資 1 件" in w for w in res.warnings)


def test_unknown_format_names_missing_columns():
    res = importers.convert("a,b,c\n1,2,3\n")
    assert res.preset is None and res.transactions == []
    res = importers.convert("約定日,銘柄コード,数量［株］\n", preset_key="rakuten_jp")
    assert res.preset is None
    assert "売買区分" in res.warnings[0] and "単価[円]" in res.warnings[0]


def test_drop_existing_counts_duplicates(tmp_path):
    existing = tmp_path / "tx.yaml"
    existing.write_text(yaml.safe_dump({"transactions": [
        {"date": "2026-04-02", "type": "buy", "symbol": "7203.T", "account": "特定口座",
         "quantity": 100, "price": 2800},
    ]}, allow_unicode=True), encoding="utf-8")
    t = {"date": "2026-04-02", "type": "buy", "symbol": "7203.T", "account": "特定口座",
         "quantity": 100.0, "price": 2800.0}
    # 同じ約定が CSV に 2 件 → 既存 1 件ぶんだけ除く
    new, dropped = importers.drop_existing([dict(t), dict(t)], existing)
    assert dropped == 1 and len(new) == 1


def test_cli_end_to_end_cp932_and_output_loads(tmp_path, capsys):
    csv_path = tmp_path / "sbi.csv"
    csv_path.write_bytes(SBI_JP.encode("cp932"))
    out = tmp_path / "imported.yaml"
    main(["--transactions", str(tmp_path / "none.yaml"), "import-csv", str(csv_path),
          "--map", str(tmp_path / "nomap.yaml"), "--out", str(out)])
    text = capsys.readouterr().out
    assert "SBI 証券" in text and "3 件" in text
    assert "280" not in text.replace("2026", "")   # 金額は画面に出さない
    led = load_tx(out)
    assert led.warnings == []
    lot = led.lots[("7203.T", "特定口座")]
    assert lot.quantity == 60


def test_overlapping_exports_are_counted_once():
    t = {"date": "2026-09-02", "type": "buy", "symbol": "7203.T", "account": "特定口座",
         "quantity": 100.0, "price": 2800.0}
    u = dict(t, date="2026-09-03")
    # 1 つ目の出力に同じ約定が 2 件（本当に 2 回約定）、2 つ目の出力にも同じ約定が 1 件
    merged = importers.merge_files([[dict(t), dict(t)], [dict(t), dict(u)]])
    assert len(merged) == 3


def test_buyback_request_is_not_a_buy():
    assert importers.side_of("買取請求") is None
    assert importers.side_of("単元未満株買取") is None
    assert importers.side_of("株式現物売") == "sell"
