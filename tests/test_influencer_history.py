"""第三方存档の履歴まとめと import-archive。ネットワークには出ない。"""

from __future__ import annotations

import json

import pytest

import influencers as inf
import influencer_history as hist

STRATEGY = {
    "as_of": "2026-10-02", "weights": {"sp": 1.0},
    "funds": {"sp": {"name": "S&P500"}},
    "stocks": {"sp": {"rows": [{"ticker": "NVDA", "name": "NVIDIA", "w": 0.1},
                               {"ticker": "MU", "name": "Micron", "w": 0.05}]}},
}


def tw(i, iso, text, likes=0, views=0, **kw):
    d = {"id": str(i), "text": text, "createdAtISO": iso, "isRetweet": False,
         "metrics": {"likes": likes, "views": views}}
    d.update(kw)
    return d


ARCHIVE = [
    tw(1, "2025-12-31T23:00:00Z", "old $NVDA"),
    tw(2, "2026-01-05T10:00:00Z", "$NVDA and $LITE CPO is the bottleneck", likes=50, views=1000),
    tw(3, "2026-01-06T10:00:00Z", "HBM supply, Micron wins", likes=5, views=9000),
    tw(4, "2026-01-07T10:00:00Z", "RT something $NVDA", isRetweet=True),
    tw(5, "2026-02-01T10:00:00Z", "nothing relevant here", isReply=True),
    {"id": "6", "text": "no time"},
    {"id": "7", "createdAtISO": "2026-02-02T00:00:00Z"},      # 本文なし・指標なし
    tw(8, "2026-02-03T00:00:00Z", "nuclear power for the grid", isQuote=True),
]


@pytest.fixture
def exposure(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(STRATEGY), encoding="utf-8")
    return inf.load_exposure(p, tmp_path / "none.yaml")[0]


def test_load_original_filters(exposure):
    rows = hist.load_original(ARCHIVE, "2026-01-01")
    assert [t["id"] for t in rows] == ["2", "3", "5", "7", "8"]


def test_build_history(exposure):
    rows = hist.load_original(ARCHIVE, "2026-01-01")
    h = hist.build_history(rows, inf.load_config(), exposure, "2026-01-01")
    assert h["total"] == 5
    jan, feb = h["months"]
    assert (jan["month"], jan["posts"], feb["posts"]) == ("2026-01", 2, 3)
    assert {t["t"]: t["n"] for t in jan["tickers"]} == {"NVDA": 1, "LITE": 1}
    assert jan["topics"]["optics"] == 1 and jan["topics"]["memory"] == 1
    assert feb["topics"]["power"] == 1 and feb["kinds"] == {"reply": 1, "post": 1, "quote": 1}
    assert jan["touch_any"] == 2 and jan["touch_ratio"] == 1.0
    assert jan["direct_ratio"] == 1.0
    assert feb["touch_any"] == 0
    # 点赞・浏览の上位は両方から。全文は持たず 280 字まで
    assert {p["id"] for p in jan["top"]} == {"2", "3"}
    assert all(len(p["text"]) <= 280 and p["url"].startswith("https://x.com/aleabitoreddit/status/")
               for m in h["months"] for p in m["top"])
    nv = next(x for x in h["holdings"] if x["ticker"] == "NVDA")
    assert nv["weight"] == 0.1 and nv["by_month"] == {"2026-01": 2} and nv["direct"] == 1 and nv["theme"] == 1
    assert h["coverage"]["cashtag_owned"] == ["NVDA"]
    assert "金額" in h["exposure_note"]


def test_excerpt_truncates():
    e = hist._excerpt(tw(9, "2026-03-01T00:00:00Z", "x" * 500))
    assert len(e["text"]) == 280


def test_empty(exposure):
    h = hist.build_history([], inf.load_config(), exposure, "2026-01-01")
    assert h["total"] == 0 and h["months"] == [] and h["first"] is None


def test_cli(tmp_path, exposure, monkeypatch):
    a = tmp_path / "a.json"
    a.write_text(json.dumps(ARCHIVE), encoding="utf-8")
    (tmp_path / "sync_state.json").write_text(json.dumps(
        {"last_update_time": "2026-09-17T23:56:29Z", "source_health": {"status": "stale_unverified"}}))
    out = tmp_path / "o.json"
    assert hist.main([str(a), "--out", str(out)]) == 0
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["total"] == 5 and d["source"]["health"] == "stale_unverified"


def test_parse_archive_and_import(tmp_path):
    posts = inf.parse_archive("serenity", "aleabitoreddit", ARCHIVE, "2026-01-01")
    assert [p.id for p in posts] == ["2", "3", "5", "7", "8"]
    assert {p.id: p.kind for p in posts} == {"2": "post", "3": "post", "5": "reply", "7": "post", "8": "quote"}
    assert posts[0].source == "archive_unverified" and posts[0].url.endswith("/status/2")
    con = inf.init_db(tmp_path / "t.sqlite")
    assert len(inf.save(con, posts)) == 5
    assert len(inf.save(con, posts)) == 0           # 二度入れても重複しない


def test_import_archive_cli(tmp_path, monkeypatch, capsys):
    a = tmp_path / "a.json"
    a.write_text(json.dumps(ARCHIVE), encoding="utf-8")
    monkeypatch.setattr(inf, "DB_PATH", tmp_path / "t.sqlite")
    assert inf.main(["import-archive", str(a), "--who", "serenity", "--since", "2026-01-01"]) == 0
    out = capsys.readouterr().out
    assert "5 件" in out and "未検証" in out
    con = inf.init_db()
    assert con.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 5
