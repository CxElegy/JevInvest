import json

import pytest

import fetch_13f
import sectors as sec
from export_13f_json import _change, build, next_deadline

CMAP = {
    "AAA": {"ticker": "AAPL", "name": "APPLE INC", "security_type": "Common Stock"},
    "BBB": {"ticker": "KO", "name": "COCA-COLA CO/THE", "security_type": "Common Stock"},
    "CCC": {"ticker": "GPN 1.5 03/01/31", "name": "GLOBAL PAYMENTS INC", "security_type": "Corp"},
    "DDD": {"ticker": "NEW", "name": "NEWCO INC", "security_type": "Common Stock"},
}
SIC = {"AAPL": {"sic": 3571}, "KO": {"sic": 2080}, "GPN": {"sic": 7389}}
ROSTER = [{"name": "Fund A", "person": "投資家A"}, {"name": "Fund B", "person": "投資家B"},
          {"name": "Old Fund", "person": "昔の人"}]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_13f, "DB_PATH", tmp_path / "e.sqlite")
    con = fetch_13f.init_db()

    def filer(cik, rd, name, n, total):
        con.execute("INSERT INTO filers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (cik, rd, name, name, "value", rd, "acc", total, n, "dollars", 0.0))

    def pos(cik, rd, cusip, value, shares):
        con.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?)",
                    (cik, rd, cusip, "ISSUER " + cusip, "COM", value, shares, "SH"))

    filer("1", "2026-06-30", "Fund A", 3, 1000.0)
    pos("1", "2026-06-30", "AAA", 600.0, 200.0)     # 100 → 200 株: 買い増し
    pos("1", "2026-06-30", "CCC", 100.0, 10.0)      # 社債
    pos("1", "2026-06-30", "DDD", 300.0, 30.0)      # 新規
    filer("1", "2026-03-31", "Fund A", 2, 800.0)
    pos("1", "2026-03-31", "AAA", 500.0, 100.0)
    pos("1", "2026-03-31", "BBB", 300.0, 50.0)      # 全売却
    pos("1", "2026-03-31", "CCC", 0.0, 10.0)

    filer("2", "2026-06-30", "Fund B", 1, 1000.0)
    pos("2", "2026-06-30", "AAA", 1000.0, 100.0)
    filer("2", "2026-03-31", "Fund B", 1, 50.0)       # 前期が小さすぎる → 前期比なし
    pos("2", "2026-03-31", "AAA", 50.0, 5.0)

    filer("3", "2023-12-31", "Old Fund", 1, 100.0)    # 最新四半期の提出なし
    pos("3", "2023-12-31", "AAA", 100.0, 1.0)
    con.commit()
    yield con
    con.close()


def test_build_per_investor_changes_sectors_and_labels(db):
    d = build(db, ROSTER, CMAP, SIC, generated="t")
    json.dumps(d, allow_nan=False)                       # ブラウザで読める JSON

    assert d["report_date"] == "2026-06-30" and d["next_deadline"] == "2026-11-14"
    assert [e["person"] for e in d["excluded"]] == ["昔の人"]

    a = next(f for f in d["filers"] if f["name"] == "Fund A")
    top = {r["t"]: r for r in a["top"]}
    assert top["AAPL"]["chg"] == "add" and top["AAPL"]["chg_pct"] == 100.0
    assert top["NEW"]["chg"] == "new" and top["NEW"]["s"] == "その他・不明"
    assert top["GPN"]["c"].endswith("（社債・優先株など）") and top["GPN"]["s"] == "決済・ビジネスサービス"
    assert [r["t"] for r in a["exits"]] == ["KO"] and [r["t"] for r in a["new"]] == ["NEW"]
    sw = {s["s"]: s for s in a["sectors"]}
    assert sw["テクノロジー（ハード・半導体）"]["w"] == pytest.approx(60.0)

    b = next(f for f in d["filers"] if f["name"] == "Fund B")
    assert not b["comparable"] and b["top"][0]["chg"] is None

    g = d["group"]
    assert g["n"] == 1 and g["names"] == ["投資家A"]
    assert [e["t"] for e in g["sells"]] == ["KO"]


def test_change_thresholds():
    assert _change(10, 0) == ("new", None)
    assert _change(0, 10)[0] == "exit"
    assert _change(105, 100)[0] == "hold"
    assert _change(111, 100)[0] == "add" and _change(89, 100)[0] == "trim"


def test_sector_mapping_and_ticker_normalisation():
    assert sec.sector_for(3674) == "テクノロジー（ハード・半導体）"
    assert sec.sector_for(7370) == "ソフトウェア・インターネット"
    assert sec.sector_for(6331) == "金融"
    assert sec.sector_for(2834) == "ヘルスケア"
    assert sec.sector_for(None) == "その他・不明"
    assert sec.sector_for(3571, "ETP") == "ETF・ファンド"
    assert sec.sec_ticker("BRK/B") == "BRK-B" and sec.sec_ticker("GOOGL 6.25 05/15/29 A") == "GOOGL"
    assert sec.sec_ticker("EA*") == "EA"
    assert next_deadline("2026-12-31") == "2027-05-15"
