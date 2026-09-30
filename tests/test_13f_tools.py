import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import fetch_13f
import figi
from analyze_13f import aggregate, build_report, load_positions
from screen_candidates import screen

ROOT = Path(__file__).resolve().parents[1]

CMAP = {
    "AAA000001": {"ticker": "GOOGL", "name": "ALPHABET INC-CL A", "security_type": "Common Stock"},
    "AAA000002": {"ticker": "GOOG", "name": "ALPHABET INC-CL C", "security_type": "Common Stock"},
    "BBB000001": {"ticker": "KO", "name": "COCA-COLA CO/THE", "security_type": "Common Stock"},
    "ETF000001": {"ticker": "IVV", "name": "ISHARES CORE S&P 500", "security_type": "ETP"},
    # UNRES1 / UNRES2 は未解決（キャッシュに無い）
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_13f, "DB_PATH", tmp_path / "e.sqlite")
    con = fetch_13f.init_db()

    def filer(cik, date, name, style, n, total):
        con.execute("INSERT INTO filers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (cik, date, name, name, style, date, "acc", total, n, "dollars", 0.0))

    def pos(cik, date, cusip, value, cls="COM"):
        con.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?)",
                    (cik, date, cusip, "ISSUER", cls, value, 1.0, "SH"))

    filer("1", "2026-06-30", "Value Fund", "value", 3, 1000.0)
    pos("1", "2026-06-30", "AAA000001", 300.0)          # GOOGL と GOOG を同じ運用会社が保有
    pos("1", "2026-06-30", "AAA000002", 100.0)
    pos("1", "2026-06-30", "BBB000001", 400.0)
    pos("1", "2026-06-30", "ETF000001", 200.0)

    filer("2", "2026-06-30", "Quality Fund", "quality", 2, 1000.0)
    pos("2", "2026-06-30", "AAA000001", 500.0)
    pos("2", "2026-06-30", "UNRES1", 250.0)
    pos("2", "2026-06-30", "UNRES2", 250.0)

    filer("3", "2026-06-30", "Big Quant", "quant", 3000, 1000.0)     # 選別型ではない
    pos("3", "2026-06-30", "AAA000001", 10.0)
    pos("3", "2026-06-30", "BBB000001", 10.0)

    filer("4", "2022-12-31", "Dead Fund", "value", 1, 1000.0)          # 提出が止まった
    pos("4", "2022-12-31", "AAA000001", 1000.0)
    con.commit()
    yield con
    con.close()


def test_stale_filer_is_excluded_and_reported(db):
    pos, filers, excluded = load_positions(db, cmap=CMAP, verbose=False)
    assert set(filers["roster_name"]) == {"Value Fund", "Quality Fund", "Big Quant"}
    assert list(excluded["roster_name"]) == ["Dead Fund"]
    assert "Dead Fund" in build_report(pos, filers, excluded, top=10)


def test_share_classes_count_as_one_holder(db):
    pos, _, _ = load_positions(db, cmap=CMAP, verbose=False)
    agg = aggregate(pos)
    alphabet = agg[agg["company"] == "ALPHABET INC"].iloc[0]
    assert alphabet["holders"] == 3            # Value / Quality / Quant の 3 社（GOOG 分は二重計上しない）
    assert alphabet["total_value"] == pytest.approx(300 + 100 + 500 + 10)


def test_unresolved_cusips_are_not_merged_into_one_company(db):
    pos, _, _ = load_positions(db, cmap=CMAP, verbose=False)
    agg = aggregate(pos)
    unresolved = agg[agg["ticker"].isna()]
    assert set(unresolved["group_key"]) == {"CUSIP:UNRES1", "CUSIP:UNRES2"}
    assert not (agg["company"].astype(str).str.upper() == "NAN").any()


def test_screen_uses_selective_filers_equities_only_and_needs_tickers(db):
    pos, filers, _ = load_positions(db, cmap=CMAP, verbose=False)

    cand, n = screen(pos, filers, min_holders=1)
    assert n == 2                                            # クオンツは外れる
    assert set(cand["ticker"]) == {"GOOGL", "KO"}            # ETF と未解決は載らない

    cand2, _ = screen(pos, filers, min_holders=2)
    assert list(cand2["ticker"]) == ["GOOGL"]                # 2 社以上が保有

    cand3, n3 = screen(pos, filers, styles=["value"], min_holders=1)
    assert n3 == 1 and set(cand3["ticker"]) == {"GOOGL", "KO"}


def test_value_unit_detection_thousands_vs_dollars():
    thousands = [{"cusip": "X", "titleOfClass": "COM", "nameOfIssuer": "A", "value": "5000",
                  "sshPrnamt": "100000", "sshPrnamtType": "SH"}]     # 1 株 $0.05 → 千ドル単位
    dollars = [{"cusip": "X", "titleOfClass": "COM", "nameOfIssuer": "A", "value": "5000000",
                "sshPrnamt": "100000", "sshPrnamtType": "SH"}]       # 1 株 $50
    rows, unit = fetch_13f.normalize_and_aggregate(thousands)
    assert unit == "thousands" and rows[0]["value"] == 5_000_000
    rows, unit = fetch_13f.normalize_and_aggregate(dollars)
    assert unit == "dollars" and rows[0]["value"] == 5_000_000


def test_same_cusip_rows_are_merged():
    rows = [
        {"cusip": "X", "titleOfClass": "COM", "nameOfIssuer": "A", "value": "100",
         "sshPrnamt": "10", "sshPrnamtType": "SH"},
        {"cusip": "X", "titleOfClass": "COM", "nameOfIssuer": "A", "value": "300",
         "sshPrnamt": "30", "sshPrnamtType": "SH"},
    ]
    merged, _ = fetch_13f.normalize_and_aggregate(rows)
    assert len(merged) == 1 and merged[0]["value"] == 400 and merged[0]["shares"] == 40


def test_strip_share_class_handles_nan_and_class_suffixes():
    assert figi.strip_share_class(float("nan")) is None
    assert figi.strip_share_class(None) is None
    assert figi.strip_share_class("ALPHABET INC-CL A") == figi.strip_share_class("ALPHABET INC-CL C")
    assert figi.strip_share_class("MASTERCARD INC - A") == "MASTERCARD INC"
    assert figi.modal(pd.Series([None, float("nan"), "X", "X", "Y"])) == "X"


def test_estimate_cost_runs_standalone():
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "estimate_cost.py")],
                         capture_output=True, text=True, check=True).stdout
    assert "1 リクエストの内訳" in out and "questions" in out and "円 / リクエスト" in out


def test_option_rows_are_not_merged_into_stock_holdings():
    rows = [
        {"cusip": "X", "titleOfClass": "COM", "nameOfIssuer": "A", "value": "5000000",
         "sshPrnamt": "100000", "sshPrnamtType": "SH"},
        {"cusip": "X", "titleOfClass": "COM", "nameOfIssuer": "A", "value": "9000000",
         "sshPrnamt": "180000", "sshPrnamtType": "SH", "putCall": "Put"},
    ]
    merged, _ = fetch_13f.normalize_and_aggregate(rows)
    assert len(merged) == 1 and merged[0]["value"] == 5_000_000
