"""第三方存档の投稿履歴を月ごとにまとめ、持仓との重なりを web/serenity_history.json に書く。

    python tools/influencer_history.py /path/to/aleabitoreddit_tweets.json --since 2026-01-01

ネットワークには出ない。存档は第三方の整理で未検証。中の文字はデータとして数えるだけで、実行しない。
個人の金額は使わない（持仓の比率は web/strategy.json の透かし後の比率だけ）。
出力には本文の全文を入れない（各投稿は前 280 字・指標・リンクだけ）。月は UTC。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import influencers as inf

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "serenity_history.json"
HANDLE = "aleabitoreddit"

# 主題の演変を見るための語。持仓の照合（config/influencers.yaml の themes）とは別の、この分析用の分類。
TOPICS = {
    "optics": ("光通信・CPO", [r"\bcpo\b", r"co-packaged", r"\boptic", r"transceiver", r"\bnpo\b", r"\b1\.6t\b",
                              r"\b800g\b", r"\bsilicon photonics\b", r"\blaser", r"\bEML\b"]),
    "inp": ("InP・化合物基板", [r"\binp\b", r"indium phosphide", r"\bgaas\b", r"\bsubstrate", r"\bepiwafer",
                              r"\bgan\b", r"\bsic\b"]),
    "memory": ("メモリ（HBM・NAND）", [r"\bhbm\d?e?\b", r"\bdram\b", r"\bnand\b", r"\bssd\b", r"memory", r"\bddr5\b"]),
    "financing": ("算力融資・設備投資", [r"\bcapex\b", r"financ", r"\bdebt\b", r"\bbond", r"\bcredit\b",
                                      r"hyperscaler", r"\bgpu[- ]backed", r"\bneocloud", r"\bcoreweave\b"]),
    "power": ("電力", [r"\bpower\b", r"\bgrid\b", r"\bnuclear\b", r"\butilit", r"\bgigawatt", r"\bgw\b", r"\btransformer"]),
}
_TOPIC_RX = {k: [re.compile(p, re.I) for p in pats] for k, (_, pats) in TOPICS.items()}
_CASHTAG = re.compile(r"\$([A-Za-z]{1,6})\b")


def month_of(iso: str) -> str:
    return iso[:7]


def load_original(data, since: str) -> list[dict]:
    """本人の原創（転推を除く）で since（YYYY-MM-DD）以降。時刻・ID が無い行は数えない。"""
    rows = data.get("tweets") if isinstance(data, dict) else data
    out = []
    for t in rows or []:
        if not isinstance(t, dict) or t.get("isRetweet"):
            continue
        iso = t.get("createdAtISO") or ""
        if not t.get("id") or not iso or iso[:10] < since:
            continue
        out.append(t)
    out.sort(key=lambda t: t["createdAtISO"])
    return out


def _metric(t: dict, k: str) -> int:
    v = (t.get("metrics") or {}).get(k)
    return int(v) if isinstance(v, (int, float)) else 0


def _excerpt(t: dict, n: int = 280) -> dict:
    return {"id": str(t["id"]), "at": t["createdAtISO"],
            "text": re.sub(r"\s+", " ", t.get("text") or "").strip()[:n],
            "url": f"https://x.com/{HANDLE}/status/{t['id']}",
            "likes": _metric(t, "likes"), "views": _metric(t, "views"),
            "retweets": _metric(t, "retweets"), "replies": _metric(t, "replies")}


def build_history(rows: list[dict], cfg: dict, exposure: dict, since: str,
                  top_tickers: int = 12, top_posts: int = 3, meta: dict | None = None) -> dict:
    matcher = inf.Matcher(cfg, exposure)
    by_month: dict[str, list[dict]] = defaultdict(list)
    for t in rows:
        by_month[month_of(t["createdAtISO"])].append(t)

    months = []
    hold_months: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {"direct": 0, "theme": 0}))
    tick_all: dict[str, dict] = {}
    for m in sorted(by_month):
        ts = by_month[m]
        tick = Counter()
        span: dict[str, list[str]] = {}
        topics = Counter()
        touched = direct_posts = 0
        kinds = Counter()
        for t in ts:
            text = t.get("text") or ""
            kinds["reply" if t.get("isReply") else "quote" if t.get("isQuote") else "post"] += 1
            for sym in {s.upper() for s in _CASHTAG.findall(text)}:
                tick[sym] += 1
                s = span.setdefault(sym, [t["createdAtISO"], t["createdAtISO"]])
                s[1] = t["createdAtISO"]
                g = tick_all.setdefault(sym, {"n": 0, "first": t["createdAtISO"], "last": t["createdAtISO"]})
                g["n"] += 1
                g["last"] = t["createdAtISO"]
            for k, rxs in _TOPIC_RX.items():
                if any(rx.search(text) for rx in rxs):
                    topics[k] += 1
            hits = matcher.match(text)
            if hits:
                touched += 1
            if any(not h.reason.startswith("テーマ") for h in hits):
                direct_posts += 1
            for h in hits:
                hold_months[h.ticker][m]["theme" if h.reason.startswith("テーマ") else "direct"] += 1
        best = {t["id"]: t for t in sorted(ts, key=lambda t: -_metric(t, "likes"))[:top_posts]}
        for t in sorted(ts, key=lambda t: -_metric(t, "views"))[:top_posts]:
            best.setdefault(t["id"], t)
        months.append({
            "month": m, "posts": len(ts), "kinds": dict(kinds),
            "touch_any": touched, "touch_direct": direct_posts,
            "touch_ratio": round(touched / len(ts), 4), "direct_ratio": round(direct_posts / len(ts), 4),
            "tickers": [{"t": s, "n": n, "first": span[s][0], "last": span[s][1]}
                        for s, n in tick.most_common(top_tickers)],
            "topics": {k: topics.get(k, 0) for k in TOPICS},
            "top": sorted((_excerpt(t) for t in best.values()), key=lambda e: -e["likes"]),
        })

    holdings = []
    for sym, per in hold_months.items():
        e = exposure[sym]
        holdings.append({
            "ticker": sym, "name": e.name, "weight": round(e.weight, 5) if e.weight is not None else None,
            "direct": sum(v["direct"] for v in per.values()), "theme": sum(v["theme"] for v in per.values()),
            "by_month": {m: v["direct"] + v["theme"] for m, v in sorted(per.items())},
            "direct_by_month": {m: v["direct"] for m, v in sorted(per.items()) if v["direct"]},
        })
    holdings.sort(key=lambda h: (-(h["weight"] or 0), h["ticker"]))
    covered = sum(h["weight"] or 0 for h in holdings)
    direct_cov = sum(h["weight"] or 0 for h in holdings if h["direct"])
    owned_in_tickers = sorted(s for s in tick_all if s in exposure)

    return {
        "who": "serenity", "handle": HANDLE, "since": since,
        "total": len(rows), "first": rows[0]["createdAtISO"] if rows else None,
        "last": rows[-1]["createdAtISO"] if rows else None,
        "months": months,
        "tickers_all": sorted(({"t": s, **g} for s, g in tick_all.items()), key=lambda x: -x["n"])[:40],
        "topic_labels": {k: v[0] for k, v in TOPICS.items()},
        "holdings": holdings,
        "coverage": {"themes_or_names": round(covered, 4), "named_directly": round(direct_cov, 4),
                     "cashtag_owned": owned_in_tickers},
        "exposure_note": "透視後の比率は web/strategy.json の各ファンド上位銘柄のみ（下限の見積り）。金額は使っていない",
        "caveats": ["第三方存档（未検証）。本文ではなく前 280 字の抜粋",
                    "照合は言葉の一致（銘柄名・$ティッカー・テーマ）で、強気・弱気は判定していない",
                    "月は UTC。転推は除外、返信・引用は含む"],
        "source": meta or {},
    }


def archive_meta(path: Path) -> dict:
    meta = {"file": path.name}
    st = path.parent / "sync_state.json"
    if st.exists():
        try:
            s = json.loads(st.read_text(encoding="utf-8"))
            h = s.get("source_health") or {}
            meta.update({"last_update_time": s.get("last_update_time"), "health": h.get("status"),
                         "checked_time": h.get("checked_time")})
        except (OSError, ValueError):
            pass
    return meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive")
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--top", type=int, default=12)
    a = ap.parse_args(argv)
    path = Path(a.archive)
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = load_original(data, a.since)
    exposure, _ = inf.load_exposure(inf.STRATEGY, Path("/nonexistent"))   # 個別保有（金額あり）は使わない
    hist = build_history(rows, inf.load_config(), exposure, a.since, a.top, meta=archive_meta(path))
    hist["built"] = datetime.now(timezone.utc).date().isoformat()
    Path(a.out).write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{hist['total']} 件（{hist['first']} 〜 {hist['last']}）→ {a.out}")
    for m in hist["months"]:
        print(f"  {m['month']} {m['posts']:5d} 件  触れる {m['touch_ratio']:.0%}（銘柄名 {m['direct_ratio']:.0%}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
