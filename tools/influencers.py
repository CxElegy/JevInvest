"""X（旧 Twitter）の注目アカウントの投稿を取り込み、自分の持仓と照合する。

    python tools/influencers.py fetch                 # 新しい投稿を取り込む（全員）
    python tools/influencers.py fetch --only jukan
    python tools/influencers.py import posts.txt --who serenity   # 貼り付けた投稿を取り込む
    python tools/influencers.py import-archive a.json --who serenity --since 2026-01-01   # 第三方存档の JSON を取り込む
    python tools/influencers.py report --days 7       # reports/influencers.md を作る
    python tools/influencers.py watch --interval 900  # 15 分ごとに取り込み、持仓に触れる投稿を通知

計算と照合はここ（ローカル）で行い、「その人の視点で持仓をどう読むか」の判断は
Claude Code（/influencers）が reports/influencers.md を読んで行う。LLM はここから呼ばない。

投稿の取得元（上から順に使えるものを使う）:
  1. X API v2         環境変数 X_BEARER_TOKEN。読み取りは有料プランが要る
  1b. X のログイン cookie  環境変数 X_AUTH_TOKEN と X_CT0（x-timeline-archive の bird-search を使う。
                      bash tools/install_x_timeline.sh で vendor/ に入れる）。X の規約上グレーなので捨てアカウントで
  2. RSS              環境変数 TWEETS_RSS_URL（例: https://rsshub.example/twitter/user/{handle}）
                      自前の RSSHub・Nitter など。{handle} が置き換わる
  3. 手で貼る          import サブコマンド。テキストか JSON
どれも無いときは、取れない理由を出して止まる（黙って 0 件にしない）。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "influencers.yaml"
HOLDINGS = ROOT / "config" / "holdings.yaml"
STRATEGY = ROOT / "web" / "strategy.json"
DB_PATH = ROOT / "data" / "tweets.sqlite"
REPORT = ROOT / "reports" / "influencers.md"

X_API = "https://api.x.com/2"
TIMEOUT = 30.0


class SourceUnavailable(RuntimeError):
    """取得元が使えない（未設定・認証失敗・レート制限）。理由をそのまま利用者に見せる。"""


# ---------------------------------------------------------------------------
# 設定と持仓
# ---------------------------------------------------------------------------

def load_config(path: Path = CONFIG) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg.setdefault("influencers", [])
    cfg["aliases"] = {str(k): v for k, v in (cfg.get("aliases") or {}).items()}
    cfg["themes"] = cfg.get("themes") or {}
    for t in cfg["themes"].values():
        t["affects"] = {str(k): v for k, v in (t.get("affects") or {}).items()}
    return cfg


@dataclass
class Exposure:
    ticker: str
    name: str
    weight: float | None             # 持仓全体に占める比率。直接保有で比率が出せないときは None
    via: list[str] = field(default_factory=list)


def load_exposure(strategy_path: Path = STRATEGY,
                  holdings_path: Path = HOLDINGS) -> tuple[dict[str, Exposure], str]:
    """持っている銘柄と比率。ファンドは中身まで透かす（ファンド比率 × ファンド内比率）。

    web/strategy.json はファンドごとの上位銘柄しか持たないので、透かした比率は下限の見積り。
    config/holdings.yaml にある個別株は直接保有として加える（比率は価格が要るので None）。
    """
    exp: dict[str, Exposure] = {}
    notes = []
    if strategy_path.exists():
        s = json.loads(strategy_path.read_text(encoding="utf-8"))
        for fk, f in s.get("funds", {}).items():
            fw = float(s.get("weights", {}).get(fk, f.get("weight", 0)) or 0)
            for r in s.get("stocks", {}).get(fk, {}).get("rows", []):
                e = exp.setdefault(r["ticker"], Exposure(r["ticker"], r.get("name") or r["ticker"], 0.0))
                e.weight = (e.weight or 0.0) + fw * float(r["w"])
                e.via.append(f"{f['name']} {r['w']:.1%}")
        notes.append(f"web/strategy.json（{s.get('as_of', '?')} 時点、目標比率 "
                     + "・".join(f"{s['funds'][k]['name']} {v:.0%}" for k, v in s.get("weights", {}).items())
                     + "、各ファンドの上位銘柄のみ）")
    if holdings_path.exists():
        h = yaml.safe_load(holdings_path.read_text(encoding="utf-8")) or {}
        n = 0
        for p in h.get("positions") or []:
            sym = str(p.get("symbol", ""))
            if not sym or sym.startswith(("CASH-", "FUND-")):
                continue
            e = exp.setdefault(sym, Exposure(sym, p.get("name") or sym, None))
            e.via.append(f"直接保有（{p.get('account', '?')}）")
            n += 1
        if n:
            notes.append(f"config/holdings.yaml の個別保有 {n} 件（比率は report で確認）")
    return exp, "、".join(notes) or "持仓の情報なし（web/strategy.json も config/holdings.yaml も無い）"


# ---------------------------------------------------------------------------
# 照合
# ---------------------------------------------------------------------------

@dataclass
class Hit:
    ticker: str
    reason: str          # 「名前が出た」「$NVDA」「テーマ HBM: HBM の供給者」など


_CASHTAG = re.compile(r"\$([A-Za-z]{1,6}(?:\.[A-Za-z])?)\b")


class Matcher:
    def __init__(self, cfg: dict, exposure: dict[str, Exposure]):
        self.exposure = exposure
        self.aliases = [(t, re.compile(p, re.I)) for t, ps in cfg["aliases"].items() for p in ps]
        self.themes = [(k, [re.compile(p, re.I) for p in t.get("patterns", [])], t["affects"])
                       for k, t in cfg["themes"].items()]
        # 東証の銘柄は「8035」のような 4 桁コードでも拾う
        self.codes = {t.split(".")[0]: t for t in exposure if re.fullmatch(r"\w{4}\.T", t)}

    def match(self, text: str) -> list[Hit]:
        hits: dict[str, Hit] = {}

        def add(t: str, reason: str):
            if t in self.exposure and t not in hits:
                hits[t] = Hit(t, reason)

        for tag in _CASHTAG.findall(text):
            add(tag.upper(), f"${tag.upper()}")
        for t, rx in self.aliases:
            m = rx.search(text)
            if m:
                add(t, f"「{m.group(0)}」")
        for code, t in self.codes.items():
            if re.search(rf"(?<![\d.]){code}(?![\d])", text):
                add(t, f"コード {code}")
        for key, pats, affects in self.themes:
            m = next((m for rx in pats if (m := rx.search(text))), None)
            if not m:
                continue
            for t, path in affects.items():
                add(t, f"テーマ {key}（「{m.group(0)}」）: {path}")
        return list(hits.values())

    def weight(self, hits: list[Hit]) -> float:
        return sum(self.exposure[h.ticker].weight or 0.0 for h in hits)


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------

@dataclass
class Post:
    key: str             # config の influencer key
    id: str
    created_at: str      # ISO8601 UTC
    text: str
    url: str
    source: str
    kind: str = "post"   # post / repost / reply / quote


def init_db(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            key TEXT, id TEXT, created_at TEXT, text TEXT, url TEXT, source TEXT, kind TEXT,
            fetched_at TEXT, PRIMARY KEY (key, id));
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, user_id TEXT, since_id TEXT);
    """)
    return con


def save(con: sqlite3.Connection, posts: list[Post]) -> list[Post]:
    """新しく入ったものだけを返す。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = []
    for p in posts:
        cur = con.execute("INSERT OR IGNORE INTO posts VALUES (?,?,?,?,?,?,?,?)",
                          (p.key, p.id, p.created_at, p.text, p.url, p.source, p.kind, now))
        if cur.rowcount:
            new.append(p)
    con.commit()
    return new


def load_posts(con: sqlite3.Connection, since: datetime, keys: list[str] | None = None) -> list[Post]:
    q = "SELECT key,id,created_at,text,url,source,kind FROM posts WHERE created_at >= ?"
    args: list = [since.astimezone(timezone.utc).isoformat(timespec="seconds")]
    if keys:
        q += f" AND key IN ({','.join('?' * len(keys))})"
        args += keys
    q += " ORDER BY created_at DESC"
    return [Post(*r) for r in con.execute(q, args)]


# ---------------------------------------------------------------------------
# 取得元
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _status_url(handle: str, pid: str) -> str:
    return f"https://x.com/{handle}/status/{pid}"


def snowflake_time(pid: str) -> str | None:
    """X の投稿 ID には投稿時刻が入っている（上位ビット = 2010-11-04 からのミリ秒）。"""
    if not re.fullmatch(r"\d{15,20}", str(pid)):
        return None
    ms = (int(pid) >> 22) + 1288834974657
    return _iso(datetime.fromtimestamp(ms / 1000, timezone.utc))


def parse_x_api(key: str, handle: str, payload: dict) -> list[Post]:
    out = []
    for t in payload.get("data") or []:
        # 280 字を超える投稿は note_tweet に全文が入る
        text = (t.get("note_tweet") or {}).get("text") or t.get("text", "")
        refs = {r.get("type") for r in t.get("referenced_tweets") or []}
        kind = ("repost" if "retweeted" in refs else "quote" if "quoted" in refs
                else "reply" if "replied_to" in refs else "post")
        out.append(Post(key, str(t["id"]), _iso(datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))),
                        html.unescape(text), _status_url(handle, t["id"]), "x_api", kind))
    return out


def fetch_x_api(client, inf: dict, state: dict) -> tuple[list[Post], dict]:
    token = os.getenv("X_BEARER_TOKEN")
    if not token:
        raise SourceUnavailable("X_BEARER_TOKEN が未設定")
    h = {"Authorization": f"Bearer {token}"}

    def get(url, params=None):
        r = client.get(url, params=params, headers=h, timeout=TIMEOUT)
        if r.status_code == 429:
            reset = r.headers.get("x-rate-limit-reset")
            when = datetime.fromtimestamp(int(reset)).strftime("%H:%M") if reset else "?"
            raise SourceUnavailable(f"X API のレート制限（{when} に解除）")
        if r.status_code in (401, 403):
            raise SourceUnavailable(f"X API が {r.status_code} を返した（トークン無効、または読み取りできないプラン）: "
                                    f"{r.text[:200]}")
        r.raise_for_status()
        return r.json()

    uid = state.get("user_id") or inf.get("user_id")
    if not uid:
        uid = get(f"{X_API}/users/by/username/{inf['handle']}")["data"]["id"]
    params = {"max_results": 100, "tweet.fields": "created_at,note_tweet,referenced_tweets"}
    if state.get("since_id"):
        params["since_id"] = state["since_id"]
    payload = get(f"{X_API}/users/{uid}/tweets", params)
    posts = parse_x_api(inf["key"], inf["handle"], payload)
    newest = (payload.get("meta") or {}).get("newest_id") or state.get("since_id")
    return posts, {"user_id": str(uid), "since_id": newest}


_TAG = re.compile(r"<[^>]+>")


BIRD_DEFAULT = ROOT / "vendor" / "x-timeline-archive" / "scripts" / "vendor" / "bird-search" / "bird-search.mjs"


def parse_bird(key: str, handle: str, payload) -> list[Post]:
    """bird-search（X の Web 版の検索を cookie で呼ぶ）の --json 出力。"""
    if isinstance(payload, dict):
        raise SourceUnavailable(f"bird-search: {payload.get('error') or payload}")
    out = []
    for t in payload:
        pid = str(t.get("id") or t.get("id_str") or "")
        if not pid:
            continue
        raw = t.get("createdAt") or t.get("created_at")
        try:
            created = _iso(parsedate_to_datetime(raw)) if raw else None
        except (TypeError, ValueError):
            created = None
        user = (t.get("author") or {}).get("username") or handle
        if user.lower() != handle.lower():
            continue          # 引用元など本人以外の投稿は入れない
        text = t.get("text") or ""
        kind = "reply" if t.get("inReplyToStatusId") else "quote" if t.get("quotedTweet") else "post"
        out.append(Post(key, pid, created or snowflake_time(pid) or _iso(datetime.now(timezone.utc)),
                        html.unescape(text), _status_url(handle, pid), "x_cookie", kind))
    return out


def fetch_bird(client, inf: dict, state: dict, run=subprocess.run) -> tuple[list[Post], dict]:
    token, ct0 = os.getenv("X_AUTH_TOKEN"), os.getenv("X_CT0")
    if not token or not ct0:
        raise SourceUnavailable("X_AUTH_TOKEN / X_CT0 が未設定")
    bird = Path(os.getenv("BIRD_SEARCH_PATH") or BIRD_DEFAULT)
    if not bird.exists():
        raise SourceUnavailable(f"bird-search が無い（bash tools/install_x_timeline.sh で入れる）: {bird}")
    since = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    env = {**os.environ, "AUTH_TOKEN": token, "CT0": ct0}
    proc = run(["node", str(bird), f"from:{inf['handle']} since:{since}", "--count", "40", "--json"],
               env=env, text=True, capture_output=True, timeout=120)
    try:
        payload = json.loads(proc.stdout or "null")
    except json.JSONDecodeError:
        payload = None
    if payload is None:
        raise SourceUnavailable(f"bird-search が失敗（終了コード {proc.returncode}）: {(proc.stderr or '')[:200]}")
    return parse_bird(inf["key"], inf["handle"], payload), state


def _clean_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    return html.unescape(_TAG.sub("", s)).strip()


def parse_rss(key: str, handle: str, xml_text: str) -> list[Post]:
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or "").strip()
        m = re.search(r"/status(?:es)?/(\d+)", link) or re.search(r"/status(?:es)?/(\d+)", guid)
        pid = m.group(1) if m else hashlib.sha1((guid or link).encode()).hexdigest()[:16]
        text = _clean_html(it.findtext("description") or it.findtext("title") or "")
        pub = it.findtext("pubDate")
        created = _iso(parsedate_to_datetime(pub)) if pub else _iso(datetime.now(timezone.utc))
        title = it.findtext("title") or ""
        kind = "repost" if title.startswith("RT ") or text.startswith("RT ") else "post"
        out.append(Post(key, pid, created, text, _status_url(handle, pid) if m else link, "rss", kind))
    return out


def fetch_rss(client, inf: dict, state: dict) -> tuple[list[Post], dict]:
    tmpl = os.getenv("TWEETS_RSS_URL")
    if not tmpl:
        raise SourceUnavailable("TWEETS_RSS_URL が未設定")
    url = tmpl.format(handle=inf["handle"])
    r = client.get(url, timeout=TIMEOUT, follow_redirects=True)
    if r.status_code != 200:
        raise SourceUnavailable(f"RSS が {r.status_code} を返した: {url}")
    return parse_rss(inf["key"], inf["handle"], r.text), state


SERENITYSTOCK = "https://serenitystock.com/"


def parse_serenitystock(key: str, handle: str, page: str) -> list[Post]:
    """serenitystock.com（Serenity の言及を第三方がまとめた記録）のトップページの最新言及カード。

    本文は投稿そのものではなく、記録者の要約と立場（bullish など）。投稿時刻はリンク先の投稿 ID から出す。
    robots.txt が /api/ を禁止しているので、公開ページだけを読む。
    """
    start = page.find("md:hidden")
    cards = page[start:].split('aria-label="Open ')[1:] if start >= 0 else []
    out, seen = [], set()
    for c in cards:
        tm = re.search(r"<span>\$(?:<!-- -->)?([A-Z0-9.\-]+)</span>", c)
        xm = re.search(r'href="https://x\.com/\w+/status/(\d+)"', c)
        if not tm or not xm:
            continue
        ticker, pid = tm.group(1), xm.group(1)
        if (ticker, pid) in seen:
            continue
        seen.add((ticker, pid))
        stance = re.search(r"uppercase tracking-normal[^\"]*\">([a-z ]+)</span>", c)
        summary = re.search(r'<p class="mt-3[^"]*">(.*?)</p>', c, re.S)
        text = f"${ticker} [{stance.group(1) if stance else '?'}] " + _clean_html(summary.group(1) if summary else "")
        created = snowflake_time(pid) or _iso(datetime.now(timezone.utc))
        out.append(Post(key, f"{pid}-{ticker}", created, text + "（serenitystock.com の要約）",
                        _status_url(handle, pid), "serenitystock"))
    return out


def fetch_serenitystock(client, inf: dict, state: dict) -> tuple[list[Post], dict]:
    r = client.get(SERENITYSTOCK, timeout=TIMEOUT, follow_redirects=True,
                   headers={"User-Agent": "Mozilla/5.0 (JevInvest personal research)"})
    if r.status_code != 200:
        raise SourceUnavailable(f"serenitystock.com が {r.status_code} を返した")
    posts = parse_serenitystock(inf["key"], inf["handle"], r.text)
    if not posts:
        raise SourceUnavailable("serenitystock.com の形式が変わり、言及を読めなかった")
    return posts, state


SOURCES = [("X API", fetch_x_api), ("X cookie", fetch_bird), ("RSS", fetch_rss)]
# 本人の投稿が取れないときの代わり。config の archive: で人ごとに指定する
ARCHIVES = {"serenitystock": ("serenitystock.com", fetch_serenitystock)}


def fetch(con: sqlite3.Connection, cfg: dict, only: list[str] | None = None,
          client=None) -> tuple[list[Post], list[str]]:
    """全員分を取り込む。戻り値は (新しい投稿, 取れなかった理由)。"""
    if client is None:
        import httpx
        client = httpx.Client(headers={"User-Agent": "JevInvest personal research"})
    new, errors = [], []
    for inf in cfg["influencers"]:
        if only and inf["key"] not in only:
            continue
        row = con.execute("SELECT user_id, since_id FROM state WHERE key=?", (inf["key"],)).fetchone()
        state = {"user_id": row[0], "since_id": row[1]} if row else {}
        reasons = []
        sources = SOURCES + [ARCHIVES[a] for a in inf.get("archives") or [] if a in ARCHIVES]
        for label, fn in sources:
            try:
                posts, state = fn(client, inf, state)
            except SourceUnavailable as e:
                reasons.append(f"{label}: {e}")
                continue
            except Exception as e:  # ネットワーク断・形式の変化。次の取得元を試す
                reasons.append(f"{label}: {type(e).__name__}: {e}")
                continue
            new += save(con, posts)
            con.execute("INSERT OR REPLACE INTO state VALUES (?,?,?)",
                        (inf["key"], state.get("user_id"), state.get("since_id")))
            con.commit()
            break
        else:
            errors.append(f"{inf['name']}(@{inf['handle']}): " + " / ".join(reasons))
    return new, errors


# ---------------------------------------------------------------------------
# 手で貼った投稿の取り込み
# ---------------------------------------------------------------------------

def parse_pasted(key: str, handle: str, text: str) -> list[Post]:
    """JSON（X API の形、または {id,text,created_at,url} の配列）か、プレーンテキスト。

    プレーンテキストは空行 2 つ以上か `---` の行で区切る。各ブロックに x.com/.../status/<id> の
    URL があればそれを ID にし、無ければ本文のハッシュを ID にする（同じものを二度入れても重複しない）。
    日付は `2026-10-04` や `2026-10-04 13:05` の行があれば使い、無ければ取り込んだ時刻。
    """
    s = text.strip()
    if s.startswith(("{", "[")):
        data = json.loads(s)
        if isinstance(data, dict) and "data" in data:
            return parse_x_api(key, handle, data)
        out = []
        for d in data:
            pid = str(d.get("id") or hashlib.sha1(d["text"].encode()).hexdigest()[:16])
            created = d.get("created_at") or _iso(datetime.now(timezone.utc))
            out.append(Post(key, pid, _iso(datetime.fromisoformat(created.replace("Z", "+00:00"))),
                            d["text"], d.get("url") or _status_url(handle, pid), "import"))
        return out
    out = []
    for block in re.split(r"\n[ \t]*(?:-{3,}[ \t]*\n|\n[ \t]*\n)", s):
        block = block.strip()
        if not block:
            continue
        m = re.search(r"https?://(?:x|twitter)\.com/\w+/status/(\d+)", block)
        pid = m.group(1) if m else hashlib.sha1(block.encode()).hexdigest()[:16]
        d = re.search(r"\b(20\d\d-\d\d-\d\d)(?:[ T](\d\d:\d\d))?", block)
        created = (_iso(datetime.fromisoformat(f"{d.group(1)}T{d.group(2) or '00:00'}+00:00")) if d
                   else _iso(datetime.now(timezone.utc)))
        if m and snowflake_time(m.group(1)):
            created = snowflake_time(m.group(1))   # URL があれば ID の時刻が一番正確
        out.append(Post(key, pid, created, block, _status_url(handle, pid) if m else "", "import"))
    return out


def parse_archive(key: str, handle: str, data, since: str | None = None) -> list[Post]:
    """第三方存档（tweets の JSON 配列）を Post にする。未検証のデータなので source に印を付ける。

    転推は入れない。欠けているフィールドは .get で扱い、時刻の無い行は捨てる（件数は呼び出し側で比べる）。
    存档の中の文字はデータとして保存するだけで、解釈も実行もしない。
    """
    rows = data.get("tweets") if isinstance(data, dict) else data
    cut = since or ""
    out = []
    for t in rows or []:
        if not isinstance(t, dict) or t.get("isRetweet"):
            continue
        pid = str(t.get("id") or "")
        created = t.get("createdAtISO") or ""
        if not pid or not created or created[:10] < cut:
            continue
        kind = "reply" if t.get("isReply") else "quote" if t.get("isQuote") else "post"
        out.append(Post(key, pid, _iso(datetime.fromisoformat(created.replace("Z", "+00:00"))),
                        t.get("text") or "", _status_url(handle, pid), "archive_unverified", kind))
    return out


# ---------------------------------------------------------------------------
# レポート
# ---------------------------------------------------------------------------

def _pct(w: float | None) -> str:
    return "比率不明" if w is None else f"{w:.2%}"


def _fmt_time(iso: str) -> str:
    jst = datetime.fromisoformat(iso).astimezone(timezone(timedelta(hours=9)))
    return jst.strftime("%Y-%m-%d %H:%M JST")


def build_report(posts: list[Post], cfg: dict, exposure: dict[str, Exposure], exp_note: str,
                 days: float, errors: list[str] | None = None, max_posts: int = 30) -> str:
    matcher = Matcher(cfg, exposure)
    now = datetime.now(timezone.utc)
    L = [f"# 注目アカウントの投稿と持仓の照合（{_fmt_time(_iso(now))}）", ""]
    L += [(f"- 対象期間: 直近 {days * 24:g} 時間" if days < 1 else f"- 対象期間: 直近 {days:g} 日"), f"- 持仓の出所: {exp_note}",
          "- 比率は「持仓全体に占める比率」。ファンドは上位銘柄だけを透かしているので下限の見積り",
          "- 照合は言葉の一致（銘柄名・$ティッカー・テーマ）。言及があっても強気か弱気かは判定していない", ""]
    if errors:
        L += ["## 取り込めなかったもの（先に確認）", ""] + [f"- {e}" for e in errors] + [""]
    big = sorted((e for e in exposure.values() if e.weight), key=lambda e: -e.weight)[:12]
    if big:
        L += ["## 照合に使った持仓（透かした後の上位）", "", "| 銘柄 | 比率 | 経由 |", "|---|---:|---|"]
        L += [f"| {e.ticker} {e.name} | {_pct(e.weight)} | {'、'.join(e.via)} |" for e in big]
        L += [""]

    for inf in cfg["influencers"]:
        mine = [p for p in posts if p.key == inf["key"]]
        lens = inf.get("lens") or {}
        L += [f"## {inf['name']}（@{inf['handle']}）", ""]
        if lens:
            L += [f"**見方（こちらの要約。本人の発言ではない）**: {lens.get('summary', '').strip()}", ""]
            if lens.get("cares_about"):
                L += ["気にすること: " + " / ".join(lens["cares_about"]), ""]
            if lens.get("blind_spots"):
                L += [f"注意: {lens['blind_spots'].strip()}", ""]
        if not mine:
            L += ["この期間の投稿は取り込まれていない。", ""]
            continue
        scored = [(p, matcher.match(p.text)) for p in mine]
        rel = [(p, h) for p, h in scored if h]
        rel.sort(key=lambda x: (matcher.weight(x[1]), x[0].created_at), reverse=True)
        L += [f"投稿 {len(mine)} 件のうち、持仓に触れるもの {len(rel)} 件。", ""]

        touched: dict[str, list[str]] = {}
        for p, hs in rel:
            for h in hs:
                touched.setdefault(h.ticker, []).append(p.id)
        if touched:
            L += ["| 触れた持ち株 | 比率 | 言及した投稿数 |", "|---|---:|---:|"]
            for t, ids in sorted(touched.items(), key=lambda x: -(exposure[x[0]].weight or 0)):
                L += [f"| {t} {exposure[t].name} | {_pct(exposure[t].weight)} | {len(ids)} |"]
            total = sum(exposure[t].weight or 0 for t in touched)
            L += ["", f"この人の話題に入る持ち株の合計: **{total:.1%}**（同じ銘柄は 1 回だけ数える）", ""]

        L += ["### 持仓に触れる投稿（持仓への比率が大きい順）", ""]
        for p, hs in rel[:max_posts]:
            tag = "" if p.kind == "post" else f"（{p.kind}）"
            L += [f"#### {_fmt_time(p.created_at)}{tag} — 触れる比率 {matcher.weight(hs):.2%}",
                  f"{p.url}" if p.url else "", "",
                  "> " + p.text.strip()[:1200].replace("\n", "\n> "), ""]
            L += [f"- {h.ticker}（{exposure[h.ticker].name}, {_pct(exposure[h.ticker].weight)}）: {h.reason}"
                  for h in hs]
            L += [""]
        if len(rel) > max_posts:
            L += [f"（ほか {len(rel) - max_posts} 件は省略）", ""]
        other = [p for p, h in scored if not h][:10]
        if other:
            L += ["### 持仓に直接は触れない投稿（最新 10 件。見方の文脈として）", ""]
            for p in other:
                one = re.sub(r"\s+", " ", p.text)[:200]
                L += [f"- {_fmt_time(p.created_at)} {one} {p.url}"]
            L += [""]

    L += ["## Claude Code への依頼（/influencers）", "",
          "1. 人ごとに、上の投稿だけを根拠にして「この人ならこの持仓をどう見るか」を書く。"
          "投稿に無い意見を本人のものとして作らない。推測は推測と書く。",
          "2. 触れた持ち株ごとに、投稿の内容がその銘柄（とそれを含むファンド）の何に効くかを、比率と合わせて示す。",
          "3. そのあと視点を戻し、CLAUDE.md の判断基準（税制・配分・売買を減らす）で、"
          "この見方を持仓の行動に移すべきかを判断する。短期の材料での売買は根拠が弱いと明記する。",
          ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve(cfg: dict, who: str) -> dict:
    for inf in cfg["influencers"]:
        if who in (inf["key"], inf["handle"], inf["handle"].lower(), inf["name"].lower()):
            return inf
    raise SystemExit(f"config/influencers.yaml に {who} がいない")


def write_report(con, cfg, days: float, errors=None, only=None) -> Path:
    exposure, note = load_exposure()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    posts = load_posts(con, since, only)
    text = build_report(posts, cfg, exposure, note, days, errors)
    matcher = Matcher(cfg, exposure)
    n = sum(1 for p in posts if matcher.match(p.text))
    print(f"期間内の投稿 {len(posts)} 件、持仓に触れるもの {n} 件")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text, encoding="utf-8")
    return REPORT


def alert_lines(new: list[Post], cfg: dict) -> list[str]:
    exposure, _ = load_exposure()
    matcher = Matcher(cfg, exposure)
    names = {i["key"]: i["name"] for i in cfg["influencers"]}
    out = []
    for p in sorted(new, key=lambda p: p.created_at):
        hs = matcher.match(p.text)
        if hs:
            ts = ", ".join(f"{h.ticker} {_pct(exposure[h.ticker].weight)}" for h in hs)
            one = re.sub(r"\s+", " ", p.text)[:160]
            out.append(f"[{_fmt_time(p.created_at)}] {names.get(p.key, p.key)}: {ts}\n    {one}\n    {p.url}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="新しい投稿を取り込んでレポートを作る")
    f.add_argument("--only", nargs="*")
    f.add_argument("--days", type=float, default=7)
    f.add_argument("--hours", type=float, help="--days の代わりに時間で指定（定期実行で新着だけ見る）")
    i = sub.add_parser("import", help="貼り付けた投稿を取り込む（ファイル、または - で標準入力）")
    i.add_argument("file")
    i.add_argument("--who", required=True)
    i.add_argument("--days", type=float, default=7)
    i.add_argument("--hours", type=float, help="--days の代わりに時間で指定（定期実行で新着だけ見る）")
    ia = sub.add_parser("import-archive", help="第三方存档の JSON（tweets の配列）を取り込む")
    ia.add_argument("file")
    ia.add_argument("--who", required=True)
    ia.add_argument("--since", default=None, help="この日（YYYY-MM-DD、UTC）以降の本人の投稿だけ")
    ia.add_argument("--days", type=float, default=7)
    ia.add_argument("--hours", type=float, help="--days の代わりに時間で指定")
    r = sub.add_parser("report", help="取り込み済みの投稿からレポートを作る")
    r.add_argument("--days", type=float, default=7)
    r.add_argument("--hours", type=float, help="--days の代わりに時間で指定（定期実行で新着だけ見る）")
    r.add_argument("--only", nargs="*")
    w = sub.add_parser("watch", help="一定間隔で取り込み、持仓に触れる新しい投稿を出す")
    w.add_argument("--interval", type=int, default=900, help="秒（既定 900。短くしすぎると X API の上限に当たる）")
    w.add_argument("--days", type=float, default=7)
    w.add_argument("--hours", type=float, help="--days の代わりに時間で指定（定期実行で新着だけ見る）")
    w.add_argument("--only", nargs="*")
    a = ap.parse_args(argv)

    if a.hours:
        a.days = a.hours / 24
    cfg = load_config()
    con = init_db()

    if a.cmd == "import":
        inf = _resolve(cfg, a.who)
        text = sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8")
        new = save(con, parse_pasted(inf["key"], inf["handle"], text))
        print(f"{inf['name']}: {len(new)} 件を取り込んだ")
        print("\n".join(alert_lines(new, cfg)))
        print(f"→ {write_report(con, cfg, a.days).relative_to(ROOT)}")
        return 0

    if a.cmd == "import-archive":
        inf = _resolve(cfg, a.who)
        data = json.loads(Path(a.file).read_text(encoding="utf-8"))
        posts = parse_archive(inf["key"], inf["handle"], data, a.since)
        new = save(con, posts)
        newest = max((p.created_at for p in posts), default="なし")
        print(f"{inf['name']}: 存档から {len(posts)} 件（転推を除く）、新規 {len(new)} 件を取り込んだ。最新 {newest}")
        print("注意: 第三方存档（未検証）。存档の最終時刻より後の投稿は含まれない")
        return 0

    if a.cmd == "report":
        print(f"→ {write_report(con, cfg, a.days, only=a.only).relative_to(ROOT)}")
        return 0

    if a.cmd == "fetch":
        new, errors = fetch(con, cfg, a.only)
        for e in errors:
            print(f"取得できない: {e}", file=sys.stderr)
        print(f"新しい投稿 {len(new)} 件")
        print("\n".join(alert_lines(new, cfg)))
        print(f"→ {write_report(con, cfg, a.days, errors, a.only).relative_to(ROOT)}")
        if errors and not new:
            print("\n取得元が無い場合: X_BEARER_TOKEN か TWEETS_RSS_URL を設定するか、"
                  "投稿を貼って `import` で取り込む（README の「注目アカウント」）。", file=sys.stderr)
            return 2
        return 0

    # watch
    print(f"{a.interval} 秒ごとに取り込む。止めるには Ctrl-C。")
    try:
        while True:
            new, errors = fetch(con, cfg, a.only)
            stamp = datetime.now().strftime("%H:%M")
            for e in errors:
                print(f"[{stamp}] 取得できない: {e}", file=sys.stderr)
            lines = alert_lines(new, cfg)
            if lines:
                print("\n".join(lines), flush=True)
                write_report(con, cfg, a.days, errors, a.only)
            else:
                print(f"[{stamp}] 新しい投稿 {len(new)} 件、持仓に触れるものなし", flush=True)
            time.sleep(a.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
