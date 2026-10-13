"""tools/backtest_13f_copy.py のテスト。ネットワークには出ない（合成の価格と 13F）。"""

import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

import backtest_13f_copy as bt
import fetch_13f

TS = pd.Timestamp


def holiday_calendar() -> pd.DatetimeIndex:
    """2024 年の平日から、祝日 2 日（2/19 Presidents Day, 5/27 Memorial Day）を抜いたもの。"""
    days = pd.bdate_range("2024-01-02", "2024-12-31")
    return days.drop([TS("2024-02-19"), TS("2024-05-27")])


def short_calendar(n: int = 31) -> pd.DatetimeIndex:
    return pd.bdate_range("2024-03-04", periods=n)


def piecewise(cal: pd.DatetimeIndex, steps: list[tuple[int, float]]) -> pd.Series:
    """steps = [(開始位置, 値), ...] の階段状の価格。"""
    v = np.full(len(cal), np.nan)
    for start, val in steps:
        v[start:] = val
    return pd.Series(v, index=cal)


def pos(sym, value, cusip=None, issuer=None, issue=None):
    return {"cusip": cusip or f"{sym}0000000", "issuer": issuer or f"{sym} INC", "class": "COM",
            "value": float(value), "shares": 1.0, "share_type": "SH",
            "symbol": sym if issue is None else None, "issue": issue}


def filing(rd, filed, positions, options=None, accepted=None):
    return {"report_date": str(TS(rd).date()), "filed_date": str(TS(filed).date()),
            "accepted_et": accepted, "positions": positions, "options": options or []}


# ------------------------------------------------------------ エントリー日

def test_entry_next_moves_friday_filing_to_monday_and_skips_holidays():
    cal = holiday_calendar()
    fri = filing("2024-03-31", "2024-05-10", [])                  # 金曜 → 月曜
    assert bt.entry_date(cal, fri, "next") == TS("2024-05-13")
    assert bt.entry_date(cal, fri, "same") == TS("2024-05-10")

    fri_before_holiday = filing("2023-12-31", "2024-02-16", [])   # 金曜、翌月曜は祝日
    assert bt.entry_date(cal, fri_before_holiday, "next") == TS("2024-02-20")

    on_holiday = filing("2024-03-31", "2024-05-27", [])           # 祝日に提出
    assert bt.entry_date(cal, on_holiday, "next") == TS("2024-05-28")
    assert bt.entry_date(cal, on_holiday, "same") == TS("2024-05-28")

    # 報告日（日曜）は直前の営業日の終値（参考値専用）
    assert bt.entry_date(cal, filing("2024-06-30", "2024-08-14", []), "report") == TS("2024-06-28")


def test_entry_accepted_uses_market_close_cutoff_and_falls_back_to_next():
    cal = holiday_calendar()
    intraday = filing("2024-03-31", "2024-05-10", [], accepted="2024-05-10T11:12:04-04:00")
    after_close = filing("2024-03-31", "2024-05-10", [], accepted="2024-05-10T20:19:53-04:00")
    unknown = filing("2024-03-31", "2024-05-10", [])
    assert bt.entry_date(cal, intraday, "accepted") == TS("2024-05-10")
    assert bt.entry_date(cal, after_close, "accepted") == TS("2024-05-13")
    assert bt.entry_date(cal, unknown, "accepted") == TS("2024-05-13")


def test_acceptance_time_comes_from_edgar_header_in_eastern_time():
    # EDGAR のヘッダーの ACCEPTANCE-DATETIME は米東部の現地時刻（UTC ではない）
    hdr = "<SEC-HEADER>\n<ACCEPTANCE-DATETIME>20250214061204\nACCESSION NUMBER: 0001045810-25-000013"
    assert bt.parse_acceptance_header(hdr) == "2025-02-14T06:12:04-05:00"
    assert bt.parse_acceptance_header("&lt;ACCEPTANCE-DATETIME>20240814160704") == "2024-08-14T16:07:04-04:00"
    assert bt.parse_acceptance_header("no header here") is None


def test_submissions_json_acceptance_is_only_a_sanity_checked_fallback():
    # 実際の NVIDIA の JSON の値。ET にすると 21:12 だが提出日は同じ日 → 17:30 以降なら翌営業日付けのはず → 捨てる
    assert bt.accepted_from_json("2024-02-15T02:12:45.000Z", "2024-02-14") is None
    # 22:05 ET は EDGAR の受付時間（〜22:00）の外 → 捨てる
    assert bt.accepted_from_json("2025-11-15T03:05:18.000Z", "2025-11-14") is None
    # つじつまが合うものだけ残す
    assert bt.accepted_from_json("2025-02-14T16:12:04.000Z", "2025-02-14") == "2025-02-14T11:12:04-05:00"
    assert bt.accepted_from_json("2024-05-14T22:00:00.000Z", "2024-05-15") == "2024-05-14T18:00:00-04:00"
    assert bt.accepted_from_json(None, "2024-05-15") is None


class FakeResp:
    def __init__(self, status, text="", payload=None):
        self.status_code, self.text, self._payload = status, text, payload

    def json(self):
        return self._payload


class FakeEdgar:
    """fetch_13f.Edgar の代わり。URL ごとに用意した応答を返し、呼ばれた URL を記録する。"""

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, url):
        self.calls.append(url)
        r = self.responses.get(url, FakeResp(404))
        if isinstance(r, Exception):
            raise r
        return r


def test_fetch_filing_meta_prefers_header_falls_back_to_json_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_13f, "DB_PATH", tmp_path / "e.sqlite")
    db = fetch_13f.init_db()
    cik10 = "0001045810"
    sub = {"tickers": ["NVDA"], "filings": {"files": [{"name": "x"}], "recent": {
        "accessionNumber": ["0001045810-25-000013", "0001045810-24-000021", "0001045810-24-000999"],
        "form": ["13F-HR", "13F-HR", "10-K"],
        "filingDate": ["2025-02-14", "2024-02-14", "2024-02-21"],
        "acceptanceDateTime": ["2025-02-14T16:12:04.000Z", "2024-02-15T02:12:45.000Z",
                               "2024-02-21T21:00:00.000Z"],
    }}}
    base = "https://www.sec.gov/Archives/edgar/data/1045810/"
    ok_url = base + "000104581025000013/0001045810-25-000013-index-headers.html"
    ed = FakeEdgar({
        f"https://data.sec.gov/submissions/CIK{cik10}.json": FakeResp(200, payload=sub),
        ok_url: FakeResp(200, "<ACCEPTANCE-DATETIME>20250214061204"),
        # 2024-02-14 のヘッダーは 404 → JSON の値はつじつまが合わない（21:12 ET で同日付け）ので不明
    })
    notes = bt.fetch_filing_meta(ed, db, cik10)
    rows = dict(((a, (t, src)) for a, t, src in db.execute(
        "SELECT accession, accepted_et, source FROM filing_acceptance")))
    assert rows["0001045810-25-000013"] == ("2025-02-14T06:12:04-05:00", "header")
    assert rows["0001045810-24-000021"] == (None, None)
    assert "0001045810-24-000999" not in rows                       # 13F 以外は見ない
    assert notes and "0001045810-24-000021" in notes[0]
    assert bt.coverage(db, cik10)["tickers"] == ["NVDA"]

    # ヘッダーから取れた受理時刻は取り直さない
    ed2 = FakeEdgar({f"https://data.sec.gov/submissions/CIK{cik10}.json": FakeResp(200, payload=sub)})
    bt.fetch_filing_meta(ed2, db, cik10)
    assert ok_url not in ed2.calls
    db.close()


def test_entry_is_none_when_no_close_after_filing_yet():
    cal = short_calendar(5)
    f = filing("2024-01-31", cal[-1], [])
    assert bt.entry_date(cal, f, "next") is None
    periods, notes = bt.build_periods([f], cal, "next")
    assert periods == [] and "まだ" in notes[0]


def test_unsettled_today_bar_is_dropped_and_reported():
    cal = pd.bdate_range("2026-10-06", "2026-10-13")
    during = datetime(2026, 10, 13, 13, 45, tzinfo=bt.NY)
    after = datetime(2026, 10, 13, 17, 0, tzinfo=bt.NY)
    kept, note = bt.drop_unsettled_close(cal, fetched=during)
    assert kept[-1] == TS("2026-10-12") and "2026-10-13" in note
    kept, note = bt.drop_unsettled_close(cal, fetched=after)
    assert kept[-1] == TS("2026-10-13") and note is None


def test_unsettled_bar_is_judged_by_fetch_time_not_run_time():
    # 13:47 ET に取ってキャッシュした 10-13 の足を、引け後（17:00）や翌朝に再実行しても終値として使わない
    cal = pd.bdate_range("2026-10-06", "2026-10-13")
    fetched = datetime(2026, 10, 13, 13, 47, tzinfo=bt.NY)
    for now in [datetime(2026, 10, 13, 17, 0, tzinfo=bt.NY), datetime(2026, 10, 14, 0, 30, tzinfo=bt.NY)]:
        kept, note = bt.drop_unsettled_close(cal, fetched=fetched, now=now)
        assert kept[-1] == TS("2026-10-12") and "取得時点" in note and "13:47" in note
    # 取得時刻が無いときだけ実行時刻で判定する
    kept, _ = bt.drop_unsettled_close(cal, now=datetime(2026, 10, 13, 17, 0, tzinfo=bt.NY))
    assert kept[-1] == TS("2026-10-13")

    # 最終日に足がある銘柄のうち、いちばん早く取ったものの時刻を使う（足の無い銘柄は見ない）
    adj = pd.DataFrame({"SPY": 1.0, "X": 1.0, "OLD": 1.0}, index=cal)
    adj.loc[cal[-1], "OLD"] = np.nan
    ft = {"SPY": datetime(2026, 10, 13, 18, 0, tzinfo=bt.NY), "X": fetched,
          "OLD": datetime(2026, 10, 9, 12, 0, tzinfo=bt.NY)}
    assert bt.last_bar_fetch_time(adj, cal, ft) == fetched


# ------------------------------------------------------------ 組み替えとコスト

@pytest.fixture
def two_filings():
    cal = short_calendar(21)
    adj = pd.DataFrame({
        "A": piecewise(cal, [(0, 10.0), (8, 20.0)]),          # 1 期目に 2 倍
        "B": piecewise(cal, [(0, 50.0)]),
        "C": piecewise(cal, [(0, 40.0), (16, 44.0)]),         # 2 期目に +10%
        "SPY": piecewise(cal, [(0, 100.0), (12, 110.0)]),
    })
    f1 = filing("2024-02-29", cal[1], [pos("A", 100), pos("B", 100)])    # エントリー cal[2]
    f2 = filing("2024-03-15", cal[11], [pos("B", 300), pos("C", 100)])   # エントリー cal[12]
    return cal, adj, [f1, f2]


def test_rebalance_charges_turnover_cost_including_initial_buy(two_filings):
    cal, adj, filings = two_filings
    res = bt.run_backtest(filings, adj, cal, fx=None, bench=["SPY"], weight_modes=["value"],
                          costs=[0.0, 100.0], mode="next")
    s = next(x for x in res["strategies"] if x["cost_bps"] == 100.0)

    # 1 期目: 1.0 を全額購入 → コスト 0.01。A が 2 倍 → 0.99 + 0.495 = 1.485
    # 2 期目: A を 0.99 売り、B を 0.495 → 1.11375、C を 0.37125 買う。売買 1.98 → コスト 0.0198
    nav_post = 1.485 - 0.0198
    final = nav_post * (0.75 + 0.25 * 1.1)
    assert s["metrics"]["total_return_usd"] == pytest.approx(final - 1.0)
    assert s["cost_total"] == pytest.approx(0.01 + 0.0198)
    assert s["turnover"] == pytest.approx([1.0, 1.98 / 1.485])
    assert s["period_returns"] == pytest.approx([0.485, final / 1.485 - 1.0])
    # 寄与の合計 − コスト = 総リターン
    assert sum(s["contributions"].values()) - s["cost_total"] == pytest.approx(final - 1.0)

    gross = next(x for x in res["strategies"] if x["cost_bps"] == 0.0)
    assert gross["metrics"]["total_return_usd"] == pytest.approx(1.5 * 1.025 - 1.0)
    assert gross["cost_total"] == 0.0


def test_equal_weights_and_drift_within_period(two_filings):
    cal, adj, filings = two_filings
    res = bt.run_backtest(filings, adj, cal, fx=None, bench=["SPY"], weight_modes=["equal"],
                          costs=[0.0], mode="next")
    p2 = res["periods"][1]
    assert p2["priced"][0]["weight_equal"] == pytest.approx(0.5)
    # 2 期目は B と C を半分ずつ。C だけ +10% → +5%
    assert p2["returns"]["equal"] == pytest.approx(0.05)
    # 1 期目は A が 2 倍に、B は横ばい → 期中に比率は 2:1 にずれる（戻さない）
    assert res["periods"][0]["returns"]["equal"] == pytest.approx(0.5)


# ------------------------------------------------------------ 評価不能

def test_unpriced_positions_are_excluded_renormalized_and_reported():
    cal = short_calendar(21)
    adj = pd.DataFrame({
        "A": piecewise(cal, [(0, 10.0)]),
        "LATE": piecewise(cal, [(10, 5.0)]),                  # エントリーの後に上場
        "SPY": piecewise(cal, [(0, 100.0)]),
    })
    f = filing("2024-02-29", cal[1], [
        pos("A", 100),
        pos("PRIV", 50),                                     # 価格データが無い（非上場など）
        pos("LATE", 25),
        pos(None, 25, cusip="UNRES0001", issuer="MYSTERY CO",
            issue="CUSIP からティッカーを解決できない（OpenFIGI に無い・取得失敗）"),
    ])
    res = bt.run_backtest([f], adj, cal, fx=None, bench=["SPY"], weight_modes=["value", "equal"],
                          costs=[0.0], mode="next")
    p = res["periods"][0]
    assert [r["symbol"] for r in p["priced"]] == ["A"]
    assert p["priced"][0]["weight_value"] == pytest.approx(1.0)
    un = {u["key"]: u for u in p["unpriced"]}
    assert set(un) == {"PRIV", "LATE", "CUSIP:UNRES0001"}
    assert un["PRIV"]["reported_weight"] == pytest.approx(0.25)
    assert "価格データが無い" in un["PRIV"]["reason"]
    assert "上場前" in un["LATE"]["reason"]
    assert "解決できない" in un["CUSIP:UNRES0001"]["reason"]
    assert p["unpriced_weight"] == pytest.approx(0.5)

    md = bt.render_markdown(res, {"name": "Test", "cik": "1", "generated_at": "now"}, [])
    assert "⚠ **評価不能・写せない銘柄 3 件**" in md
    assert "MYSTERY CO" in md and "PRIV" in md and "25.0%" in md


def test_otc_names_are_excluded_unless_requested():
    cal = short_calendar(10)
    adj = pd.DataFrame({"A": piecewise(cal, [(0, 10.0)]), "OTCX": piecewise(cal, [(0, 1.0)]),
                        "SPY": piecewise(cal, [(0, 100.0)])})
    f = filing("2024-02-29", cal[1], [pos("A", 100), pos("OTCX", 100)])
    ex = {"A": "NMS", "OTCX": "PNK"}
    res = bt.run_backtest([f], adj, cal, fx=None, bench=["SPY"], weight_modes=["value"],
                          costs=[0.0], exchanges=ex)
    assert [u["symbol"] for u in res["periods"][0]["unpriced"]] == ["OTCX"]
    assert "店頭" in res["periods"][0]["unpriced"][0]["reason"]
    res2 = bt.run_backtest([f], adj, cal, fx=None, bench=["SPY"], weight_modes=["value"],
                           costs=[0.0], exchanges=ex, include_otc=True)
    assert res2["periods"][0]["n_priced"] == 2


def test_name_that_stops_trading_mid_period_is_frozen_and_reported():
    cal = short_calendar(21)
    adj = pd.DataFrame({
        "A": piecewise(cal, [(0, 10.0)]),
        "D": piecewise(cal, [(0, 10.0), (5, 8.0)]),
        "SPY": piecewise(cal, [(0, 100.0)]),
    })
    adj.loc[cal[8]:, "D"] = np.nan                            # cal[7] を最後に途絶
    f = filing("2024-02-29", cal[1], [pos("A", 100), pos("D", 100)])
    res = bt.run_backtest([f], adj, cal, fx=None, bench=["SPY"], weight_modes=["value"],
                          costs=[0.0])
    gaps = res["periods"][0]["trading_gaps"]
    assert gaps and gaps[0]["symbol"] == "D" and gaps[0]["kind"] == "stopped"
    assert gaps[0]["last_date"] == str(cal[7].date())
    # 最後の終値 8 で据え置き: 0.5 × 1 + 0.5 × 0.8 = 0.9
    assert res["strategies"][0]["metrics"]["total_return_usd"] == pytest.approx(-0.1)
    md = bt.render_markdown(res, {"name": "T", "cik": "1", "generated_at": "now"}, [])
    assert "途絶" in md and "据え置" in md


# ------------------------------------------------------------ 最大下落

def test_max_drawdown_dates_and_recovery():
    idx = pd.bdate_range("2024-01-01", periods=7)
    s = pd.Series([1.0, 1.2, 0.9, 1.0, 1.3, 0.65, 1.4], index=idx)
    dd = bt.max_drawdown(s)
    assert dd["mdd"] == pytest.approx(-0.5)
    assert dd["peak"] == str(idx[4].date()) and dd["trough"] == str(idx[5].date())
    assert dd["recovery"] == str(idx[6].date())

    s2 = pd.Series([1.0, 2.0, 1.0, 1.5, 1.2], index=idx[:5])
    dd2 = bt.max_drawdown(s2)
    assert dd2["mdd"] == pytest.approx(-0.5) and dd2["peak"] == str(idx[1].date())
    assert dd2["recovery"] is None

    assert bt.max_drawdown(pd.Series([1.0, 1.1, 1.2], index=idx[:3]))["mdd"] == 0.0


# ------------------------------------------------------------ 取れなかった値動き

def test_missed_pre_disclosure_move_and_disclosure_pop_for_new_names_only():
    cal = short_calendar(21)
    adj = pd.DataFrame({
        "A": piecewise(cal, [(0, 10.0), (2, 11.0), (4, 12.0)]),
        "B": piecewise(cal, [(0, 20.0)]),
        "C": piecewise(cal, [(0, 50.0), (9, 55.0), (12, 66.0)]),
        "SPY": piecewise(cal, [(0, 100.0)]),
    })
    f1 = filing(cal[0], cal[3], [pos("A", 300), pos("B", 100)])     # 提出前日 cal[2]、エントリー cal[4]
    f2 = filing(cal[8], cal[12], [pos("A", 100), pos("C", 100)])    # 提出前日 cal[11]、エントリー cal[13]
    res = bt.run_backtest([f1, f2], adj, cal, fx=None, bench=["SPY"], weight_modes=["value"],
                          costs=[0.0], initial=True)
    m1, m2 = res["missed"]
    # 報告日 → エントリー: A +20%、B 0% を 3:1 で加重
    assert m1["pre_disclosure"]["value"] == pytest.approx(0.75 * 0.2)
    assert m1["pre_disclosure"]["equal"] == pytest.approx(0.1)
    assert m1["new_names"] == ["A", "B"] and "初回" in m1["new_label"]
    assert m1["pop"]["value"] == pytest.approx(0.75 * (12 / 11 - 1))
    # 2 回目: A は前回もあったので新規ではない。C だけが新規
    assert m2["new_names"] == ["C"]
    assert m2["pre_disclosure"]["value"] == pytest.approx(0.5 * 0.0 + 0.5 * (66 / 50 - 1))
    assert m2["pop"]["value"] == pytest.approx(66 / 55 - 1)
    assert m2["pop_bench"] == pytest.approx(0.0)
    c = next(r for r in res["new_names"] if r["symbol"] == "C")
    assert c["pop"] == pytest.approx(0.2) and c["after_entry_to_exit"] == pytest.approx(0.0)


def test_pop_baseline_is_last_close_before_acceptance():
    cal = short_calendar(15)
    # X: 提出日（cal[5]）の場中に 10 → 12（開示前の値動き）、エントリー（cal[6]）で 15
    adj = pd.DataFrame({
        "X": piecewise(cal, [(0, 10.0), (5, 12.0), (6, 15.0)]),
        "SPY": piecewise(cal, [(0, 100.0), (5, 101.0), (6, 103.0)]),
    })
    d = cal[5].date()
    cases = {
        f"{d}T16:12:45-04:00": (cal[5], 15 / 12 - 1, 103 / 101 - 1),   # 引け後 → 提出日の終値が起点
        f"{d}T06:12:04-04:00": (cal[4], 15 / 10 - 1, 103 / 100 - 1),   # 寄り付き前 → 前営業日の終値
        f"{d}T11:12:04-04:00": (cal[4], 15 / 10 - 1, 103 / 100 - 1),   # 場中 → 前営業日の終値
        None: (cal[4], 15 / 10 - 1, 103 / 100 - 1),                    # 不明 → 提出日の前営業日
    }
    for acc, (base, pop, spy) in cases.items():
        f = filing(cal[0], cal[5], [pos("X", 100)], accepted=acc)
        res = bt.run_backtest([f], adj, cal, fx=None, bench=["SPY"], weight_modes=["value"],
                              costs=[0.0], mode="next", initial=True)
        m = res["missed"][0]
        assert m["pre_filing_close"] == str(base.date()), acc
        assert m["pop"]["value"] == pytest.approx(pop), acc
        assert m["pop_bench"] == pytest.approx(spy), acc
        assert res["new_names"][0]["pop"] == pytest.approx(pop)
        assert res["periods"][0]["entry"] == str(cal[6].date())       # エントリーは変わらない

    # 受理が提出日の前日の夜（17:30 以降 → 翌営業日付けの提出）なら、その日の終値が起点
    f = filing(cal[0], cal[5], [pos("X", 100)], accepted=f"{cal[4].date()}T18:00:00-04:00")
    i, rule = bt.pre_disclosure_close(bt.Prices(adj, cal), f)
    assert i == 4 and "引け後" in rule


def test_otc_names_are_left_out_of_follower_pre_disclosure_and_top_pops():
    cal = short_calendar(15)
    adj = pd.DataFrame({
        "A": piecewise(cal, [(0, 10.0), (4, 11.0)]),
        "OTCX": piecewise(cal, [(0, 10.0), (4, 30.0)]),        # 店頭: 大きく上がるが写せない
        "SPY": piecewise(cal, [(0, 100.0)]),
    })
    f = filing(cal[0], cal[3], [pos("A", 100), pos("OTCX", 100)], accepted=f"{cal[3].date()}T16:30:00-04:00")
    res = bt.run_backtest([f], adj, cal, fx=None, bench=["SPY"], weight_modes=["value"],
                          costs=[0.0], exchanges={"A": "NMS", "OTCX": "PNK"}, initial=True)
    m = res["missed"][0]
    assert m["pre_disclosure"]["value"] == pytest.approx(0.1)               # 写せる A だけ
    assert m["pre_disclosure_all_holdings"]["value"] == pytest.approx(0.5 * 0.1 + 0.5 * 2.0)
    assert m["pop"]["value"] == pytest.approx(0.1)
    otc = next(r for r in res["new_names"] if r["symbol"] == "OTCX")
    assert otc["copyable"] is False and otc["pop"] == pytest.approx(2.0)   # 参考として値は出す
    head = " ".join(bt._headline(res, "T"))
    assert "OTCX" not in head and "A +10.0%" in head


def test_first_filing_is_not_called_new_when_history_is_unknown():
    cal = short_calendar(10)
    adj = pd.DataFrame({"A": piecewise(cal, [(0, 10.0)]), "SPY": piecewise(cal, [(0, 1.0)])})
    f = filing(cal[0], cal[2], [pos("A", 100)])
    res = bt.run_backtest([f], adj, cal, fx=None, bench=["SPY"], weight_modes=["value"],
                          costs=[0.0], initial=None)
    assert res["missed"][0]["new_names"] == [] and "判断できない" in res["missed"][0]["new_label"]

    assert bt.first_is_initial([f], {"recent_from": "2020-09-14", "older_pages": 1}) is True
    assert bt.first_is_initial([f], {"recent_from": "2024-02-01", "older_pages": 1}) is False
    assert bt.first_is_initial([f], {"recent_from": "2024-02-01", "older_pages": 0}) is True
    assert bt.first_is_initial([f], {}) is None


# ------------------------------------------------------------ ベンチマーク

def test_benchmarks_cover_identical_dates_and_period_boundaries(two_filings):
    cal, adj, filings = two_filings
    adj = adj.assign(NEWB=piecewise(cal, [(5, 1.0)]))       # 開始日に価格が無い
    fx = pd.Series(np.linspace(150.0, 160.0, len(cal)), index=cal).iloc[::2]   # 為替は飛び飛び
    res = bt.run_backtest(filings, adj, cal, fx=fx, bench=["SPY", "NEWB"],
                          weight_modes=["value"], costs=[0.0])
    strat = res["strategies"][0]["metrics"]
    spy = next(b for b in res["benchmarks"] if b["symbol"] == "SPY")
    assert (spy["metrics"]["start"], spy["metrics"]["end"]) == (strat["start"], strat["end"])
    assert len(res["nav"]["series"]["SPY_0bp"]) == len(res["nav"]["series"]["value_0bp"]) \
        == len(res["nav"]["dates"])
    # 期の境界も同じ: SPY は cal[12] に 100 → 110
    assert spy["period_returns"] == pytest.approx([0.1, 0.0])
    assert res["periods"][0]["bench_returns"]["SPY"] == pytest.approx(0.1)
    assert spy["metrics"]["total_return_usd"] == pytest.approx(0.1)
    # 円建て: 欠けた日の為替は直前の値で埋める
    assert spy["metrics"]["total_return_jpy"] is not None
    assert [b["symbol"] for b in res["bench_unavailable"]] == ["NEWB"]
    assert "上場前" in res["bench_unavailable"][0]["reason"]


def test_calendar_prefers_benchmark_trading_days():
    idx = pd.bdate_range("2024-01-01", periods=5)
    px = pd.DataFrame({"SPY": [1, 1, np.nan, 1, 1], "X": [1, 1, 1, 1, 1]}, index=idx, dtype=float)
    cal, ref = bt.trading_calendar(px, ["SPY"])
    assert ref == "SPY" and len(cal) == 4


# ------------------------------------------------------------ 読み込み・出力

def test_load_filings_keeps_options_separate_and_attach_symbols_flags_non_stock(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_13f, "DB_PATH", tmp_path / "e.sqlite")
    db = fetch_13f.init_db()
    cik = "0000000042"
    db.execute("INSERT INTO filers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (cik, "2024-12-31", "X", "X CORP", None, "2025-02-14", "acc-1", 300.0, 3,
                "dollars", 0.0))
    for cusip, cls, value, st in [("AAA", "COM", 100.0, "SH"), ("BND", "NOTE", 100.0, "PRN"),
                                  ("UNK", "COM", 50.0, "SH"), ("PRF", "PFD", 50.0, "SH")]:
        db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?)",
                   (cik, "2024-12-31", cusip, "ISSUER " + cusip, cls, value, 1.0, st))
    db.execute("INSERT INTO options VALUES (?,?,?,?,?,?,?)",
               (cik, "2024-12-31", "AAA", "ISSUER AAA", "Put", 999.0, 10.0))
    bt.init_meta_tables(db)
    db.execute("INSERT INTO filing_acceptance VALUES (?,?,?,?,?)",
               (cik, "acc-1", "2025-02-14T06:12:04-05:00", "header", 0.0))
    db.commit()

    fs = bt.load_filings(db, cik)
    assert len(fs) == 1 and fs[0]["accepted_et"] == "2025-02-14T06:12:04-05:00"
    assert fs[0]["accepted_source"] == "header"
    assert [o["put_call"] for o in fs[0]["options"]] == ["Put"]
    assert {p["cusip"] for p in fs[0]["positions"]} == {"AAA", "BND", "UNK", "PRF"}

    cmap = {"AAA": {"ticker": "BRK/B"}, "BND": {"ticker": "X 5 01/01/30"},
            "PRF": {"ticker": "X 6.5 PFD"}}
    bt.attach_symbols(fs, cmap)
    by = {p["cusip"]: p for p in fs[0]["positions"]}
    assert by["AAA"]["symbol"] == "BRK-B"
    assert by["BND"]["symbol"] is None and "PRN" in by["BND"]["issue"]
    assert by["UNK"]["symbol"] is None and "解決できない" in by["UNK"]["issue"]
    assert by["PRF"]["symbol"] is None and "普通株以外" in by["PRF"]["issue"]
    db.close()


def test_report_and_json_are_complete(two_filings):
    cal, adj, filings = two_filings
    fx = pd.Series(150.0, index=cal)
    res = bt.run_backtest(filings, adj, cal, fx=fx, bench=["SPY"], weight_modes=["value", "equal"],
                          costs=[0.0, 50.0])
    sens = [bt._sensitivity_row(res, "next", 0.0)]
    md = bt.render_markdown(res, {"name": "Test", "cik": "1", "generated_at": "now",
                                  "calendar_ref": "SPY"}, sens)
    for heading in ["## 結論", "## 写した場合とベンチマーク", "### 片道コスト 50bp",
                    "## 期ごとの結果", "## フォロワーが取れなかった値動き",
                    "## 銘柄ごと", "## 評価不能の銘柄", "## この計算の限界"]:
        assert heading in md
    assert "評価不能の銘柄はなかった" in md
    payload = json.dumps(bt._clean({"result": res, "sensitivity": sens}), allow_nan=False)
    back = json.loads(payload)
    assert back["result"]["strategies"][0]["metrics"]["total_return_jpy"] == pytest.approx(
        back["result"]["strategies"][0]["metrics"]["total_return_usd"])


# ------------------------------------------------------------ 指標の数値

def test_jpy_return_direction_and_drawdown():
    # ドル建てで横ばい、円安（150 → 165 円）なら円建ては +10%、途中の下落は無い
    idx = pd.bdate_range("2024-01-01", periods=11)
    nav = pd.Series(1.0, index=idx)
    fx = pd.Series(np.linspace(150.0, 165.0, len(idx)), index=idx)
    m = bt.summarize(nav, fx, [0.0], ["x"])
    assert m["total_return_usd"] == pytest.approx(0.0)
    assert m["total_return_jpy"] == pytest.approx(0.10)
    assert m["max_drawdown_jpy"]["mdd"] == 0.0
    # 円高（165 → 150）なら円建てはマイナス
    m2 = bt.summarize(nav, fx[::-1].set_axis(idx), [0.0], ["x"])
    assert m2["total_return_jpy"] == pytest.approx(150 / 165 - 1)


def test_vol_is_annualised_with_252_days_and_cagr_uses_calendar_days():
    idx = pd.bdate_range("2024-01-01", periods=41)
    r = np.array([0.01 if k % 2 else -0.01 for k in range(40)])
    nav = pd.Series(np.r_[1.0, np.cumprod(1 + r)], index=idx)
    m = bt.summarize(nav, None, [], [])
    assert m["vol_ann"] == pytest.approx(np.std(r, ddof=1) * np.sqrt(252))

    two_years = pd.Series([1.0, 1.5, 2.0], index=pd.to_datetime(["2024-01-01", "2024-12-31", "2025-12-31"]))
    m = bt.summarize(two_years, None, [], [])
    assert (pd.Timestamp("2025-12-31") - pd.Timestamp("2024-01-01")).days == 730
    assert m["cagr_usd"] == pytest.approx(2 ** (365.25 / 730) - 1)
    assert m["ret_per_vol"] == pytest.approx(m["cagr_usd"] / m["vol_ann"])
    assert m["calmar"] is None                                     # 下落が無ければ出さない


def test_strategy_drawdown_and_risk_adjusted_ratios(two_filings):
    cal, adj, filings = two_filings
    adj = adj.copy()
    adj.loc[cal[5]:cal[6], "A"] = 8.0                             # 1 期目の途中で A が 10 → 8
    res = bt.run_backtest(filings, adj, cal, fx=None, bench=["SPY"], weight_modes=["value"],
                          costs=[0.0])
    m = res["strategies"][0]["metrics"]
    dd = m["max_drawdown_usd"]
    # 資産 1.0（A 0.5 + B 0.5）→ cal[5] に A が 0.8 倍で 0.9 → cal[7] に 1.0 へ戻る → cal[8] に 1.5
    assert dd["mdd"] == pytest.approx(-0.1)
    assert (dd["peak"], dd["trough"], dd["recovery"]) == (str(cal[4].date()), str(cal[5].date()),
                                                         str(cal[7].date()))
    assert m["calmar"] == pytest.approx(m["cagr_usd"] / 0.1)
    assert m["losing_periods"] == 0 and m["n_periods"] == 2


def test_raw_return_lead_is_only_called_a_win_when_it_survives_costs(two_filings):
    cal, adj, filings = two_filings
    # 写した戦略は 0bp で +53.75%、100bp で +50.18%。SPY +10%（勝ち）、HIGH +100%（負け）、
    # QQQ2 +52%（差 1.75pt → ほぼ同じ）、FLIP +50.5%（0bp では 3.25pt 上回るが、100bp では
    # 戦略のコストが重く差が 1.2pt に縮む → ほぼ同じ）
    adj = adj.assign(QQQ2=piecewise(cal, [(0, 100.0), (20, 152.0)]),
                     HIGH=piecewise(cal, [(0, 100.0), (20, 200.0)]),
                     FLIP=piecewise(cal, [(0, 100.0), (20, 150.5)]))
    res = bt.run_backtest(filings, adj, cal, fx=None, bench=["SPY", "QQQ2", "HIGH", "FLIP"],
                          weight_modes=["value"], costs=[0.0, 100.0])
    cmp_ = bt.compare_to_benchmarks(res, "value")
    assert cmp_["beats"] == ["SPY"]
    assert set(cmp_["roughly_equal"]) == {"QQQ2", "FLIP"}
    assert cmp_["loses"] == ["HIGH"]
    s0 = bt._strategy(res, "value", 0.0)
    assert s0["periods_beating_first_bench"] == 2                  # 1 期目 +50% 対 +10%、2 期目 +2.5% 対 0%
    assert s0["best_period"]["report_date"] == filings[0]["report_date"]
    assert s0["total_without_best_period"] == pytest.approx(0.025)
    assert s0["first_bench_without_best_period"] == pytest.approx(0.0)
    head = "\n".join(bt._headline(res, "T"))
    assert "上回った SPY" in head and "ほぼ同じ" in head and "リスク当たり" in head
    assert head.index("リスク当たり") < head.index("総リターンだけで比べると")


def test_shares_weights_drift_13f_values_to_entry_prices(two_filings):
    cal, adj, filings = two_filings
    adj = adj.copy()
    f = filing(cal[0], cal[4], [pos("A", 100), pos("B", 100)])      # 報告日 cal[0]、エントリー cal[5]
    adj.loc[cal[3]:, "A"] = 30.0                                     # 報告日からエントリーまでに A が 3 倍
    res = bt.run_backtest([f], adj, cal, fx=None, bench=["SPY"], weight_modes=["value", "shares", "equal"],
                          costs=[0.0])
    pr = res["periods"][0]["priced"]
    w = {r["symbol"]: r for r in pr}
    assert w["A"]["weight_value"] == pytest.approx(0.5)
    assert w["A"]["weight_shares"] == pytest.approx(0.75)           # 300 : 100
    assert w["A"]["weight_equal"] == pytest.approx(0.5)
    assert res["periods"][0]["shares_without_report_price"] == []


def test_nisa_quota_buys_by_year_count_purchases_in_yen(two_filings):
    cal, adj, filings = two_filings
    fx = pd.Series(150.0, index=cal)
    fx.loc[cal[12]:] = 165.0                                         # 2 回目の組み替えの時点で円安 10%
    res = bt.run_backtest(filings, adj, cal, fx=fx, bench=["SPY"], weight_modes=["value"], costs=[0.0])
    s = res["strategies"][0]
    # 初回 1.0 + 2 回目の買い（資産 1.5 → B 0.5 → 1.125 で +0.625、C 0 → 0.375 で +0.375。
    # 合計 1.0 ドル相当を 1 割高い円で買う → 1.1）。売り（A 1.0）は枠を戻さない
    assert s["buys_by_year"] == {"2024": pytest.approx(1.0 + 1.0 * 1.1)}
    assert s["buys_in_jpy"] is True
    md = bt.render_markdown(res, {"name": "T", "cik": "1", "generated_at": "now"}, [])
    assert "NISA 成長投資枠" in md and "2.10 倍" in md and "約 114 万円" in md


def test_zero_volume_bars_are_not_used_as_tradable_prices():
    cal = short_calendar(15)
    adj = pd.DataFrame({"A": piecewise(cal, [(0, 10.0)]), "Z": piecewise(cal, [(0, 5.0)]),
                        "SPY": piecewise(cal, [(0, 100.0)])})
    vol = pd.DataFrame(1000.0, index=cal, columns=adj.columns)
    vol.loc[cal[0]:, "Z"] = 0.0                                      # Z は 1 度も売買されていない
    vol.loc[cal[3], "SPY"] = 0.0                                     # ベンチマークは触らない
    f = filing(cal[0], cal[1], [pos("A", 100), pos("Z", 100)])
    res = bt.run_backtest([f], adj, cal, fx=None, bench=["SPY"], weight_modes=["value"],
                          costs=[0.0], volume=vol)
    assert [u["symbol"] for u in res["periods"][0]["unpriced"]] == ["Z"]
    assert res["zero_volume_days"] == {"Z": 15}
    assert any("出来高 0" in n for n in res["notes"])


def test_sensitivity_table_uses_benchmarks_present_not_a_fixed_ticker(two_filings):
    cal, adj, filings = two_filings
    res = bt.run_backtest(filings, adj, cal, fx=None, bench=["SPY"], weight_modes=["value", "equal"],
                          costs=[0.0])
    row = bt._sensitivity_row(res, "next", 0.0)
    assert row["benches"] == {"SPY": pytest.approx(0.1)}
    variants = [{"weights": "shares", "include_otc": False, "metrics": res["strategies"][0]["metrics"]}]
    md = bt.render_markdown(res, {"name": "T", "cik": "1", "generated_at": "now"}, [row], variants)
    assert "NVDA" not in md
    assert "| エントリー | 期間 | 時価加重 | 等金額 | SPY |" in md
    assert "## 組み入れ比率・店頭銘柄の扱いを変えると" in md and "株数×エントリー日株価" in md
    assert "年率÷ボラ" in md and "13F の比率は無視" in md


def test_otc_hindsight_warning_depends_on_zero_volume_before_entry():
    res = {"periods": [{"entry": "2024-02-15", "unpriced": [
        {"symbol": "OLD", "reason": "店頭（OTC: PNK）銘柄"}, {"symbol": "LATE", "reason": "店頭（OTC: PNK）銘柄"}]}]}
    idx = pd.bdate_range("2024-02-01", periods=30)
    vol = pd.DataFrame(1000.0, index=idx, columns=["OLD", "LATE"])
    vol.loc["2024-02-07", "OLD"] = 0.0                              # エントリー前に出来高 0 → 当時既に店頭
    vol.loc["2024-03-01", "LATE"] = 0.0                             # エントリー後 → 後知恵で除外の疑い
    notes = bt.otc_hindsight_notes(res, vol, {"OLD": "PNK", "LATE": "PNK"})
    old = next(n for n in notes if n.startswith("OLD"))
    late = next(n for n in notes if "LATE" in n)
    assert "2024-02-07" in old and "⚠" not in old
    assert late.startswith("⚠") and "--include-otc" in late
