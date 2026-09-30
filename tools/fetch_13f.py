"""SEC EDGAR から 13F-HR を取得して SQLite に貯める。

一次ソースは EDGAR。アグリゲーターサイトを経由しないので、
数字の出所が「SEC への提出書類そのもの」になる。

    python tools/fetch_13f.py                 # ロスター全件
    python tools/fetch_13f.py --limit 5       # 先頭 5 件で試す
    python tools/fetch_13f.py --only Berkshire Pershing
    python tools/fetch_13f.py --refresh       # キャッシュを無視して取り直す

SEC の fair access policy:
  - User-Agent に連絡先を明記すること（下の USER_AGENT を自分のものに変える）
  - 10 リクエスト/秒まで。本スクリプトは余裕を見て約 5/秒に抑えている
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "edgar13f.sqlite"
ROSTER = ROOT / "config" / "investors.yaml"

# SEC は User-Agent に連絡先の明記を求める。個人のメールアドレスをコードに書かず、
# 環境変数 SEC_USER_AGENT で渡す（例: "JevInvest research you@example.com"）。
USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "jev-portfolio personal research tool (contact: your-email@example.com)",
)

REQUEST_INTERVAL = 0.2   # 秒。約 5 req/sec
TIMEOUT = 60.0


class SecBlocked(RuntimeError):
    pass


class Edgar:
    def __init__(self):
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        self._last = 0.0

    def get(self, url: str, **kw) -> httpx.Response:
        wait = REQUEST_INTERVAL - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        r = self._client.get(url, **kw)
        self._last = time.time()
        # SEC は連絡先の無いアクセスを HTML の拒否ページ（403 や 200）で返す。
        # これを「見つからない」と扱うと、全員が未解決として静かに記録されてしまう。
        if r.status_code in (403, 429) or "Undeclared Automated Tool" in r.text[:2000]:
            raise SecBlocked(
                f"SEC にアクセスを拒否されました（HTTP {r.status_code}）。"
                "SEC_USER_AGENT に連絡先メールアドレスを含めて再実行してください"
            )
        return r

    def close(self) -> None:
        self._client.close()


def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS filers (
            cik          TEXT NOT NULL,
            report_date  TEXT NOT NULL,
            roster_name  TEXT,
            edgar_name   TEXT,
            style        TEXT,
            filed_date   TEXT,
            accession    TEXT,
            total_value  REAL,
            n_positions  INTEGER,
            value_unit   TEXT,      -- dollars / thousands
            fetched_at   REAL,
            PRIMARY KEY (cik, report_date)
        );
        CREATE TABLE IF NOT EXISTS positions (
            cik         TEXT NOT NULL,
            report_date TEXT NOT NULL,
            cusip       TEXT NOT NULL,
            issuer      TEXT,
            class       TEXT,
            value       REAL,       -- 常にドル単位に正規化して格納
            shares      REAL,
            share_type  TEXT,
            PRIMARY KEY (cik, report_date, cusip, class)
        );
        -- プット・コールは保有株とは別に持つ。value は原資産ベースの想定元本（ドル）。
        CREATE TABLE IF NOT EXISTS options (
            cik         TEXT NOT NULL,
            report_date TEXT NOT NULL,
            cusip       TEXT NOT NULL,
            issuer      TEXT,
            put_call    TEXT NOT NULL,   -- Put / Call
            value       REAL,
            shares      REAL,
            PRIMARY KEY (cik, report_date, cusip, put_call)
        );
        CREATE TABLE IF NOT EXISTS unresolved (
            roster_name TEXT PRIMARY KEY,
            reason      TEXT,
            checked_at  REAL
        );
        """
    )
    db.commit()
    return db


def resolve_cik(ed: Edgar, name: str) -> list[str]:
    """会社名から CIK 候補を引く。

    EDGAR の atom 出力は company-info の name が壊れている（ARRAY(0x..) が返る）
    既知のバグがあるため、名前の確認は submissions JSON 側で行う。
    """
    r = ed.get(
        "https://www.sec.gov/cgi-bin/browse-edgar",
        params={
            "action": "getcompany", "company": name,
            "type": "13F-HR", "output": "atom", "count": "20",
        },
    )
    if r.status_code != 200:
        return []
    return list(dict.fromkeys(re.findall(r"<cik>(\d{10})</cik>", r.text)))


def latest_13f(ed: Edgar, cik: str) -> dict | None:
    """最新の 13F-HR の accession と日付を返す。"""
    r = ed.get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    if r.status_code != 200:
        return None
    d = r.json()
    rec = d.get("filings", {}).get("recent", {})
    forms = rec.get("form", [])

    for i, f in enumerate(forms):
        # 13F-HR と 13F-HR/A（訂正）を対象にする。13F-NT（保有なし通知）は除く
        if f in ("13F-HR", "13F-HR/A"):
            return {
                "edgar_name": d.get("name"),
                "accession": rec["accessionNumber"][i],
                "filed_date": rec["filingDate"][i],
                "report_date": rec["reportDate"][i],
                "form": f,
            }
    return None


def recent_13f(ed: Edgar, cik: str, n: int) -> list[dict]:
    """直近 n 四半期分の 13F-HR を、報告日ごとに 1 件ずつ新しい順で返す。

    同じ報告日に原本（13F-HR）と訂正（13F-HR/A）がある場合は原本を採る。
    訂正は「追加分だけ」の提出があり、それを採ると保有が欠けて見えるため。
    原本が無い報告日だけ訂正を使う。
    """
    r = ed.get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    if r.status_code != 200:
        return []
    d = r.json()
    rec = d.get("filings", {}).get("recent", {})
    by_date: dict[str, dict] = {}
    for i, f in enumerate(rec.get("form", [])):
        if f not in ("13F-HR", "13F-HR/A"):
            continue
        rd = rec["reportDate"][i]
        cur = by_date.get(rd)
        if cur is None or (cur["form"] == "13F-HR/A" and f == "13F-HR"):
            by_date[rd] = {
                "edgar_name": d.get("name"),
                "accession": rec["accessionNumber"][i],
                "filed_date": rec["filingDate"][i],
                "report_date": rd,
                "form": f,
            }
    return [by_date[k] for k in sorted(by_date, reverse=True)[:n]]


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def fetch_info_table(ed: Edgar, cik: str, accession: str) -> list[dict]:
    """保有明細（information table）を取得してパースする。"""
    acc_nodash = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}"

    r = ed.get(f"{base}/index.json")
    if r.status_code != 200:
        raise RuntimeError(f"index.json が取れない (HTTP {r.status_code})")

    items = r.json().get("directory", {}).get("item", [])
    # primary_doc.xml はカバーページ。保有明細はもう一方の xml。
    xmls = [
        it["name"] for it in items
        if it["name"].lower().endswith(".xml") and "primary_doc" not in it["name"].lower()
    ]
    if not xmls:
        # 2013 年より前の提出はプレーンテキスト形式で、構造化されていない。
        # 最新の CIK を選んでいれば通常ここには来ない。
        raise RuntimeError(
            "information table の xml が無い（2013 年以前の旧形式の可能性）"
        )

    r = ed.get(f"{base}/{xmls[0]}")
    if r.status_code != 200:
        raise RuntimeError(f"information table が取れない (HTTP {r.status_code})")

    root = ET.fromstring(r.content)
    rows: list[dict] = []
    for node in root.iter():
        if _strip_ns(node.tag) != "infoTable":
            continue
        rec: dict = {}
        for child in node.iter():
            t = _strip_ns(child.tag)
            if t in ("nameOfIssuer", "titleOfClass", "cusip", "value",
                     "sshPrnamt", "sshPrnamtType", "putCall"):
                rec[t] = (child.text or "").strip()
        if rec.get("cusip"):
            rows.append(rec)
    return rows


def _num(s: str | None) -> float:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def aggregate_options(rows: list[dict], unit: str) -> list[dict]:
    """プット・コールの行を (CUSIP, 種別) でまとめる。単位は現物側の判定に合わせる。"""
    scale = 1000.0 if unit == "thousands" else 1.0
    agg: dict[tuple[str, str], dict] = {}
    for r in rows:
        pc = (r.get("putCall") or "").strip().capitalize()
        if pc not in ("Put", "Call"):
            continue
        a = agg.setdefault((r.get("cusip", ""), pc), {
            "cusip": r.get("cusip", ""), "issuer": r.get("nameOfIssuer", ""),
            "put_call": pc, "value": 0.0, "shares": 0.0})
        a["value"] += _num(r.get("value")) * scale
        a["shares"] += _num(r.get("sshPrnamt"))
    return list(agg.values())


def normalize_and_aggregate(rows: list[dict]) -> tuple[list[dict], str]:
    """単位を正規化し、同一銘柄の行をまとめる。

    2 つの落とし穴を処理する:

    1. value の単位が提出者によって違う。2023 年の規則改正で「千ドル」から
       「ドル」に変わったが、2026 年時点でも千ドル単位で出す運用会社がある
       （例: Baupost）。単位を誤ると保有額が 1000 倍ずれる。
       株数から逆算した 1 株単価が現実的かどうかで判定する。

       合計値での判定は、株数の極端に多い銘柄 1 つで歪む。行ごとに単価を
       出して中央値を見るほうが堅牢。

    2. 同一銘柄が複数行に分かれる。マネージャー別・議決権別に別行で出るため、
       CUSIP と クラスでまとめないと保有額が過小に見える。
    """
    # プット・コールの行は、原資産の時価で報告される「オプションの想定元本」であって
    # 保有株ではない。同じ CUSIP で現物と合算すると保有が水増しされるので除外する。
    rows = [r for r in rows if not (r.get("putCall") or "").strip()]

    implied = sorted(
        _num(r.get("value")) / _num(r.get("sshPrnamt"))
        for r in rows
        if r.get("sshPrnamtType") == "SH" and _num(r.get("sshPrnamt")) > 0
    )

    unit = "dollars"
    if implied:
        median = implied[len(implied) // 2]
        # 1 株 $1 未満が中央値になる株式ポートフォリオはまず無い → 千ドル単位
        if median < 1.0:
            unit = "thousands"

    scale = 1000.0 if unit == "thousands" else 1.0

    agg: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r.get("cusip", ""), r.get("titleOfClass", ""))
        a = agg.setdefault(
            key,
            {
                "cusip": r.get("cusip", ""),
                "class": r.get("titleOfClass", ""),
                "issuer": r.get("nameOfIssuer", ""),
                "value": 0.0,
                "shares": 0.0,
                "share_type": r.get("sshPrnamtType", ""),
            },
        )
        a["value"] += _num(r.get("value")) * scale
        a["shares"] += _num(r.get("sshPrnamt"))

    return list(agg.values()), unit


def process(ed: Edgar, db: sqlite3.Connection, entry: dict, refresh: bool,
            quarters: int = 2) -> str:
    name = entry["name"]
    style = entry.get("style")
    cik = entry.get("cik")

    if not cik:
        cands = resolve_cik(ed, name)
        if not cands:
            db.execute(
                "INSERT OR REPLACE INTO unresolved VALUES (?,?,?)",
                (name, "EDGAR の会社名検索で 13F 提出者として見つからない", time.time()),
            )
            db.commit()
            return "未解決"
        # 候補が複数ある場合は「最新の報告日を持つ CIK」を採用する。
        #
        # 「13F がある最初の候補」を採ってはいけない。運用会社は解散・改名・
        # 再登録で複数の CIK を持つことがあり、たとえば Baupost は
        #   0001054420 … 2002 年で提出が止まった旧法人
        #   0001061768 … 現行法人
        # の 2 つがある。前者を掴むと 20 年以上前のデータを静かに返してしまう。
        cik = None
        info = None
        best = ""
        for c in cands[:8]:
            i = latest_13f(ed, c)
            if i and i["report_date"] > best:
                best, cik, info = i["report_date"], c, i
        if cik is None:
            db.execute(
                "INSERT OR REPLACE INTO unresolved VALUES (?,?,?)",
                (name, f"候補 {len(cands)} 件すべてに 13F-HR がない", time.time()),
            )
            db.commit()
            return "13F なし"
    else:
        cik = cik.zfill(10)
        info = latest_13f(ed, cik)

    if not info:
        db.execute(
            "INSERT OR REPLACE INTO unresolved VALUES (?,?,?)",
            (name, "13F-HR の提出履歴が無い", time.time()),
        )
        db.commit()
        return "13F なし"

    filings = recent_13f(ed, cik, quarters) or [info]
    notes: list[str] = []
    for fi in filings:
        existing = db.execute(
            "SELECT n_positions, total_value, value_unit FROM filers"
            " WHERE cik=? AND report_date=?",
            (cik, fi["report_date"]),
        ).fetchone()
        if existing and not refresh:
            notes.append(f"{fi['report_date']} キャッシュ")
            continue

        rows = fetch_info_table(ed, cik, fi["accession"])
        positions, unit = normalize_and_aggregate(rows)
        options = aggregate_options(rows, unit)
        total = sum(p["value"] for p in positions)

        db.execute(
            "INSERT OR REPLACE INTO filers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                cik, fi["report_date"], name, fi["edgar_name"], style,
                fi["filed_date"], fi["accession"], total, len(positions),
                unit, time.time(),
            ),
        )
        db.execute("DELETE FROM positions WHERE cik=? AND report_date=?",
                   (cik, fi["report_date"]))
        db.executemany(
            "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?)",
            [
                (cik, fi["report_date"], p["cusip"], p["issuer"], p["class"],
                 p["value"], p["shares"], p["share_type"])
                for p in positions
            ],
        )
        db.execute("DELETE FROM options WHERE cik=? AND report_date=?",
                   (cik, fi["report_date"]))
        db.executemany(
            "INSERT OR REPLACE INTO options VALUES (?,?,?,?,?,?,?)",
            [(cik, fi["report_date"], o["cusip"], o["issuer"], o["put_call"],
              o["value"], o["shares"]) for o in options],
        )
        notes.append(
            f"{fi['report_date']} / {len(positions)} 銘柄 / ${total / 1e9:,.1f}B"
            + ("  ⚠千ドル単位" if unit == "thousands" else "")
        )
    db.execute("DELETE FROM unresolved WHERE roster_name=?", (name,))
    db.commit()
    return " ; ".join(notes)


def main() -> None:
    ap = argparse.ArgumentParser(description="SEC EDGAR から 13F-HR を取得する")
    ap.add_argument("--limit", type=int, help="先頭 N 件だけ処理する")
    ap.add_argument("--only", nargs="+", help="名前に含まれる文字列で絞る")
    ap.add_argument("--refresh", action="store_true", help="キャッシュを無視して取り直す")
    ap.add_argument("--quarters", type=int, default=2,
                    help="取得する四半期数（既定 2。前期比の売買を出すには 2 以上）")
    args = ap.parse_args()

    if "your-email@example.com" in USER_AGENT:
        print("⚠ USER_AGENT が既定値のままです。")
        print("  SEC は連絡先の明記を要求しています。環境変数 SEC_USER_AGENT に")
        print("  連絡先を含む文字列を設定してください。")
        print("  （このまま続行しますが、アクセスを制限される可能性があります）")
        print()

    roster = yaml.safe_load(ROSTER.read_text(encoding="utf-8"))["investors"]

    if args.only:
        needles = [s.lower() for s in args.only]
        roster = [e for e in roster
                  if any(n in e["name"].lower() for n in needles)]
    if args.limit:
        roster = roster[: args.limit]

    db = init_db()
    ed = Edgar()
    ok = failed = 0

    print(f"対象 {len(roster)} 件を EDGAR から取得します")
    print("=" * 74)
    try:
        for i, entry in enumerate(roster, 1):
            label = entry["name"][:38]
            try:
                result = process(ed, db, entry, args.refresh, args.quarters)
                if result in ("未解決", "13F なし"):
                    failed += 1
                    print(f"[{i:>3}/{len(roster)}] {label:<38} — {result}")
                else:
                    ok += 1
                    print(f"[{i:>3}/{len(roster)}] {label:<38} ✓ {result}")
            except SecBlocked as e:
                print(f"\n✗ {e}")
                print("  拒否された状態で続けても全件失敗するので中断します。")
                break
            except Exception as e:  # noqa: BLE001 — 1 件の失敗で全体を止めない
                failed += 1
                print(f"[{i:>3}/{len(roster)}] {label:<38} ✗ {type(e).__name__}: {e}")
    except KeyboardInterrupt:
        print("\n中断しました。取得済みのデータは保存されています。")
    finally:
        ed.close()

    print("=" * 74)
    print(f"成功 {ok} 件 / 失敗 {failed} 件")
    print(f"保存先: {DB_PATH}")

    un = db.execute("SELECT roster_name, reason FROM unresolved ORDER BY roster_name").fetchall()
    if un:
        print(f"\n未解決 {len(un)} 件（config/investors.yaml に cik を直接書けば解決します）:")
        for n, why in un:
            print(f"  - {n}: {why}")

    db.close()


if __name__ == "__main__":
    main()
