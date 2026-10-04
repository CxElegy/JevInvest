"""注目アカウントの取り込みと持仓照合。ネットワークには出ない。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import influencers as inf

STRATEGY = {
    "as_of": "2026-10-02",
    "weights": {"sp": 0.6, "semi": 0.4},
    "funds": {"sp": {"name": "S&P500"}, "semi": {"name": "日経半導体"}},
    "stocks": {
        "sp": {"rows": [{"ticker": "NVDA", "name": "NVIDIA", "w": 0.1},
                        {"ticker": "MU", "name": "Micron", "w": 0.05}]},
        "semi": {"rows": [{"ticker": "285A.T", "name": "Kioxia", "w": 0.25},
                          {"ticker": "8035.T", "name": "Tokyo Electron", "w": 0.15}]},
    },
}


@pytest.fixture
def cfg():
    return inf.load_config()


@pytest.fixture
def exposure(tmp_path):
    sp = tmp_path / "strategy.json"
    sp.write_text(json.dumps(STRATEGY), encoding="utf-8")
    hp = tmp_path / "holdings.yaml"
    hp.write_text("positions:\n  - {symbol: CASH-JPY, account: A}\n  - {symbol: TSLA, name: Tesla, account: NISA}\n",
                  encoding="utf-8")
    exp, note = inf.load_exposure(sp, hp)
    return exp


def test_config_has_jukan_and_serenity(cfg):
    handles = {i["handle"] for i in cfg["influencers"]}
    assert {"Jukanlosreve", "aleabitoreddit"} <= handles
    for i in cfg["influencers"]:
        assert i["lens"]["summary"]


def test_exposure_looks_through_funds(exposure):
    assert exposure["NVDA"].weight == pytest.approx(0.06)
    assert exposure["285A.T"].weight == pytest.approx(0.10)
    assert exposure["TSLA"].weight is None          # 直接保有は比率を推測しない
    assert "CASH-JPY" not in exposure


def test_match_alias_cashtag_code_and_theme(cfg, exposure):
    m = inf.Matcher(cfg, exposure)
    hits = {h.ticker: h.reason for h in m.match("Kioxia guides NAND up; $MU strong. 8035 orders.")}
    assert "kioxia" in hits["285A.T"].lower()
    assert hits["MU"] == "$MU"
    assert hits["8035.T"] == "コード 8035"
    # テーマ経由: 持っていない会社の話でも持ち株に結びつく
    hbm = {h.ticker for h in m.match("SK hynix HBM4 qualification passed")}
    assert {"MU", "NVDA"} <= hbm
    # 持っていない銘柄の cashtag は拾わない
    assert m.match("$AXTI to the moon") == []


def test_dram_news_does_not_hit_nand_only_holding(cfg, exposure):
    m = inf.Matcher(cfg, exposure)
    assert "285A.T" not in {h.ticker for h in m.match("DRAM contract prices up")}


def test_parse_x_api_uses_note_tweet_and_kind():
    payload = {"data": [
        {"id": "1", "text": "short…", "note_tweet": {"text": "full long text"},
         "created_at": "2026-10-03T01:02:03.000Z"},
        {"id": "2", "text": "RT @x: hi", "created_at": "2026-10-03T02:00:00.000Z",
         "referenced_tweets": [{"type": "retweeted", "id": "9"}]},
    ], "meta": {"newest_id": "2"}}
    posts = inf.parse_x_api("jukan", "Jukanlosreve", payload)
    assert posts[0].text == "full long text"
    assert posts[0].url == "https://x.com/Jukanlosreve/status/1"
    assert posts[0].created_at == "2026-10-03T01:02:03+00:00"
    assert posts[1].kind == "repost"


def test_parse_rss():
    xml = """<rss><channel>
      <item><title>t</title><description>&lt;p&gt;Micron &amp;amp; HBM&lt;br&gt;line2&lt;/p&gt;</description>
        <link>https://nitter.example/aleabitoreddit/status/123#m</link>
        <pubDate>Fri, 03 Oct 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""
    [p] = inf.parse_rss("serenity", "aleabitoreddit", xml)
    assert p.id == "123"
    assert p.url == "https://x.com/aleabitoreddit/status/123"
    assert p.text == "Micron & HBM\nline2"
    assert p.created_at == "2026-10-03T10:00:00+00:00"


def test_parse_pasted_blocks_and_dedupe(tmp_path):
    text = ("2026-10-03 09:10\nfirst post\n\nsame post, second paragraph\n"
            "https://x.com/Jukanlosreve/status/555\n\n\nsecond post\n---\nthird post")
    posts = inf.parse_pasted("jukan", "Jukanlosreve", text)
    assert [p.id for p in posts][0] == "555"
    assert len(posts) == 3                       # 空行 1 つでは分けない
    assert posts[0].created_at == "2026-10-03T09:10:00+00:00"
    con = inf.init_db(tmp_path / "t.sqlite")
    assert len(inf.save(con, posts)) == 3
    assert inf.save(con, inf.parse_pasted("jukan", "Jukanlosreve", text)) == []


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeClient:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, headers=None, **kw):
        self.calls.append((url, params))
        return self.routes(url, params)


def test_fetch_x_api_uses_since_id_and_falls_back(tmp_path, monkeypatch, cfg):
    monkeypatch.setenv("X_BEARER_TOKEN", "t")
    monkeypatch.delenv("TWEETS_RSS_URL", raising=False)

    def routes(url, params):
        if "/users/by/username/Jukanlosreve" in url:
            return FakeResp(200, {"data": {"id": "42"}})
        if url.endswith("/users/42/tweets"):
            return FakeResp(200, {"data": [{"id": "7", "text": "Micron HBM",
                                            "created_at": "2026-10-03T00:00:00Z"}],
                                  "meta": {"newest_id": "7"}})
        return FakeResp(403, {"title": "Forbidden"})

    con = inf.init_db(tmp_path / "t.sqlite")
    client = FakeClient(routes)
    new, errors = inf.fetch(con, cfg, None, client)
    assert [p.id for p in new] == ["7"]
    assert len(errors) == 1 and "Serenity" in errors[0] and "403" in errors[0]
    # 2 回目は since_id 付きで、user id を引き直さない
    client.calls.clear()
    inf.fetch(con, cfg, ["jukan"], client)
    assert client.calls == [("https://api.x.com/2/users/42/tweets",
                             {"max_results": 100, "tweet.fields": "created_at,note_tweet,referenced_tweets",
                              "since_id": "7"})]


def test_fetch_reports_missing_sources(tmp_path, monkeypatch, cfg):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("TWEETS_RSS_URL", raising=False)
    con = inf.init_db(tmp_path / "t.sqlite")
    new, errors = inf.fetch(con, cfg, None, FakeClient(lambda *a: FakeResp(500)))
    assert new == [] and len(errors) == len(cfg["influencers"])
    assert all("未設定" in e for e in errors)


def test_report_lists_touched_holdings(cfg, exposure):
    now = datetime.now(timezone.utc)
    posts = [inf.Post("jukan", "1", (now - timedelta(hours=1)).isoformat(timespec="seconds"),
                      "Kioxia NAND price hike", "https://x.com/Jukanlosreve/status/1", "import"),
             inf.Post("jukan", "2", now.isoformat(timespec="seconds"), "lunch", "", "import")]
    md = inf.build_report(posts, cfg, exposure, "test", 7, ["Serenity: 未設定"])
    assert "取り込めなかったもの" in md
    assert "285A.T Kioxia | 10.00% | 1" in md
    assert "投稿 2 件のうち、持仓に触れるもの 1 件" in md
    assert "## Serenity（@aleabitoreddit）" in md and "この期間の投稿は取り込まれていない" in md
