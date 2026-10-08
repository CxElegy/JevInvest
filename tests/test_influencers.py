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
    assert {"jukan05", "aleabitoreddit"} <= handles
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
        if "/users/by/username/jukan05" in url:
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


def test_build_web_matches_report_and_keeps_no_opinion(cfg, exposure):
    now = datetime.now(timezone.utc)
    posts = [inf.Post("jukan", "1", now.isoformat(timespec="seconds"),
                      "Kioxia NAND price hike\n$MU too", "https://x.com/jukan05/status/1", "fxtwitter"),
             inf.Post("jukan", "2", now.isoformat(timespec="seconds"), "lunch", "", "fxtwitter")]
    d = inf.build_web(posts, cfg, exposure, "test", 7, now=now)
    jukan = next(p for p in d["people"] if p["key"] == "jukan")
    serenity = next(p for p in d["people"] if p["key"] == "serenity")
    assert (jukan["n_posts"], jukan["n_touch"]) == (2, 1)
    assert {t["ticker"] for t in jukan["touched"]} == {"285A.T", "MU"}
    assert jukan["covered_weight"] == pytest.approx(0.10 + 0.03)
    assert jukan["posts"][0]["text"] == "Kioxia NAND price hike $MU too"   # 改行は 1 行にまとめる
    assert jukan["others"][0]["text"] == "lunch"
    assert serenity["n_posts"] == 0 and serenity["posts"] == []


def test_snowflake_time():
    # 2105943565918703696 は 2026-10-02 08:50 UTC の投稿
    assert inf.snowflake_time("2105943565918703696").startswith("2026-10-02T08:50")
    assert inf.snowflake_time("abc") is None


def test_parse_pasted_prefers_status_id_time():
    [p] = inf.parse_pasted("jukan", "Jukanlosreve", "hi https://x.com/Jukanlosreve/status/2105943565918703696")
    assert p.created_at.startswith("2026-10-02T08:50")


SS_CARD = ('<div class="grid gap-3 p-3 md:hidden"><div><a class="absolute inset-0" aria-label="Open LITE detail" '
           'href="/stocks/NMS/LITE"></a><span><span>$<!-- -->LITE</span><span>NasdaqGS</span></span>'
           '<span class="inline-flex h-6 px-2 text-[11px] font-semibold uppercase tracking-normal border-zinc-600/40">'
           'neutral</span><p class="mt-3 break-words text-sm">CEO says CPO demand &amp; supply gap.</p>'
           '<a href="https://x.com/aleabitoreddit/status/2105943565918703696">View on X</a></div>')


def test_parse_serenitystock_cards(cfg, exposure):
    posts = inf.parse_serenitystock("serenity", "aleabitoreddit", SS_CARD + SS_CARD.replace("md:hidden", "x"))
    assert len(posts) == 1                       # 同じカードの重複は 1 件
    p = posts[0]
    assert p.text.startswith("$LITE [neutral] CEO says CPO demand & supply gap.")
    assert p.url == "https://x.com/aleabitoreddit/status/2105943565918703696"
    assert p.created_at.startswith("2026-10-02T08:50")
    assert "NVDA" in {h.ticker for h in inf.Matcher(cfg, exposure).match(p.text)}   # テーマ optics 経由


def test_archive_used_only_when_configured(tmp_path, monkeypatch, cfg):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("TWEETS_RSS_URL", raising=False)

    class R:
        status_code, text = 200, SS_CARD

    class C:
        def get(self, url, **kw):
            assert url == inf.SERENITYSTOCK
            return R()

    new, errors = inf.fetch(inf.init_db(tmp_path / "t.sqlite"), cfg, None, C())
    assert {p.source for p in new} == {"serenitystock"}
    assert len(errors) == 1 and errors[0].startswith("Jukan")


def test_parse_bird_keeps_only_own_posts():
    payload = [
        {"id": "2105943565918703696", "text": "Micron HBM &amp; DRAM", "createdAt": "Fri Oct 02 08:50:00 +0000 2026",
         "author": {"username": "Jukanlosreve"}},
        {"id": "1", "text": "quoted someone", "author": {"username": "someone_else"}},
        {"id": "2105943565918703697", "text": "reply", "inReplyToStatusId": "5",
         "author": {"username": "jukanlosreve"}},
    ]
    posts = inf.parse_bird("jukan", "Jukanlosreve", payload)
    assert [p.id for p in posts] == ["2105943565918703696", "2105943565918703697"]
    assert posts[0].text == "Micron HBM & DRAM" and posts[0].source == "x_cookie"
    assert posts[0].created_at == "2026-10-02T08:50:00+00:00"
    assert posts[1].kind == "reply"
    with pytest.raises(inf.SourceUnavailable):
        inf.parse_bird("jukan", "Jukanlosreve", {"error": "Unauthorized", "items": []})


def test_fetch_bird_passes_cookies_by_env(tmp_path, monkeypatch):
    bird = tmp_path / "bird-search.mjs"
    bird.write_text("")
    monkeypatch.setenv("BIRD_SEARCH_PATH", str(bird))
    monkeypatch.delenv("X_AUTH_TOKEN", raising=False)
    with pytest.raises(inf.SourceUnavailable, match="未設定"):
        inf.fetch_bird(None, {"key": "jukan", "handle": "Jukanlosreve"}, {})
    monkeypatch.setenv("X_AUTH_TOKEN", "a")
    monkeypatch.setenv("X_CT0", "c")
    seen = {}

    class P:
        returncode, stderr = 0, ""
        stdout = json.dumps([{"id": "2105943565918703696", "text": "hi", "author": {"username": "Jukanlosreve"}}])

    def run(cmd, env, **kw):
        seen.update(cmd=cmd, env=env)
        return P()

    posts, _ = inf.fetch_bird(None, {"key": "jukan", "handle": "Jukanlosreve"}, {}, run=run)
    assert len(posts) == 1
    assert seen["env"]["AUTH_TOKEN"] == "a" and seen["env"]["CT0"] == "c"
    assert seen["cmd"][2].startswith("from:Jukanlosreve since:")
    assert "a" not in seen["cmd"]                 # cookie はコマンドラインに出さない


FX_PAYLOAD = {"code": 200, "results": [
    {"id": "2106907946890457304", "created_timestamp": 1791160973, "text": "Micron HBM &amp; DRAM",
     "author": {"screen_name": "jukan05"}, "replying_to": None},
    {"id": "2106851949400469887", "created_timestamp": 1791147622, "text": "AI drug discovery",
     "url": "https://x.com/FredaDuan/status/2106851949400469887", "author": {"screen_name": "FredaDuan"}},
    {"id": "2106909462141542687", "created_timestamp": 1791161334, "text": "optical fiber too?",
     "author": {"screen_name": "jukan05"}, "quote": {"text": "SPCX 10GW", "author": {"screen_name": "elonmusk"}}},
    {"id": "2106149598532415787", "created_timestamp": 1790980169, "text": "link",
     "author": {"screen_name": "jukan05"}, "replying_to": {"status": "2106149596418470138"}},
]}


def test_parse_fxtwitter_kinds_and_text():
    posts = inf.parse_fxtwitter("jukan", "jukan05", FX_PAYLOAD)
    assert [p.kind for p in posts] == ["post", "repost", "quote", "reply"]
    assert posts[0].text == "Micron HBM & DRAM" and posts[0].source == "fxtwitter"
    assert posts[0].url == "https://x.com/jukan05/status/2106907946890457304"
    assert posts[0].created_at == "2026-10-05T00:42:53+00:00"
    assert posts[1].text.startswith("RT @FredaDuan: ") and "FredaDuan" in posts[1].url
    assert "[引用 @elonmusk] SPCX 10GW" in posts[2].text
    with pytest.raises(inf.SourceUnavailable):
        inf.parse_fxtwitter("jukan", "jukan05", {"code": 404, "message": "Not found"})


def test_fetch_falls_back_to_fxtwitter_without_credentials(tmp_path, monkeypatch, cfg):
    for k in ("X_BEARER_TOKEN", "X_AUTH_TOKEN", "X_CT0", "TWEETS_RSS_URL", "FXTWITTER_API"):
        monkeypatch.delenv(k, raising=False)

    def routes(url, params):
        if url == "https://api.fxtwitter.com/2/profile/jukan05/statuses":
            return FakeResp(200, FX_PAYLOAD)
        return FakeResp(404, {"code": 404})

    new, errors = inf.fetch(inf.init_db(tmp_path / "t.sqlite"), cfg, ["jukan"], FakeClient(routes))
    assert {p.source for p in new} == {"fxtwitter"} and len(new) == 4
    assert errors == []
    monkeypatch.setenv("FXTWITTER_API", "off")
    new, errors = inf.fetch(inf.init_db(tmp_path / "u.sqlite"), cfg, ["jukan"], FakeClient(routes))
    assert new == [] and "FxTwitter: FXTWITTER_API=off" in errors[0]
