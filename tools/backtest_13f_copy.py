"""13F を「開示されてから個人が実際に写せたタイミング」で写したら、どうなっていたかの検証。

    SEC_USER_AGENT="名前 連絡先メール" python tools/backtest_13f_copy.py   # NVIDIA（CIK 1045810）
    python tools/backtest_13f_copy.py --cik 1067983 --name Berkshire
    python tools/backtest_13f_copy.py --cost-bps 0 50 100 --bench SPY SMH
    python tools/backtest_13f_copy.py --entry same      # 提出日の終値で買えた楽観ケース
    python tools/backtest_13f_copy.py --no-fetch        # SEC に行かず、キャッシュだけで計算
    python tools/backtest_13f_copy.py --no-fetch --web web/copycheck.json   # ページ用の要約も書く

出力: reports/13f_copy_<cik>.md（読む用）と data/13f_copy_<cik>.json（機械可読）。どちらも .gitignore 済み。
--web を付けると、web/ledger.html の「13F を写す」タブが読む公開用の要約（丸めた数字・週次の資産推移。
個人の金額・手元のパス・SEC への連絡先は入れない）も書く。

何を測るか:
  13F は四半期末の保有を、最大 45 日後に開示する。「〇〇の宿題を写せ」という話は、
  報告日から開示までの値動きや、開示直後の急騰（開示ポップ）まで含めて語られがち。
  ここでは、開示を見てから動く個人が実際に取れたリターンだけを測り、
  取れなかった分（開示前の値動き・開示ポップ）を別に出す。

先読みをしないルール:
  - エントリー: 提出日の「翌営業日」の終値（--entry next、既定）。提出は引け後のことが多く、
    提出日の終値では買えない。--entry same は提出日の終値で買えたとする楽観ケース、
    --entry accepted は EDGAR の受理時刻（各提出のヘッダーの ACCEPTANCE-DATETIME、米東部）が
    16:00 より前ならその日の終値、後なら翌営業日。
  - 出口: 次の提出のエントリー日。最後の期は取得できた最新の終値まで（場中の値は使わない。
    場中かどうかは実行時刻ではなく、価格を取得した時刻で判定する）
  - 開示ポップの起点は、開示前の最後の終値（受理が 16:00 以降なら受理日の終値、前なら前営業日の終値）
  - 各エントリーで、その提出の現物株ロングに組み替える（プット・コールは別に一覧するだけ）
  - 期中は持ちっぱなし（比率は値動きでずれるまま）
  - 片道コストは、組み替えで売買した金額に掛ける（最初の購入は全額が売買）

黙って直さない（CLAUDE.md）:
  - エントリー日に価格の無い銘柄（非上場・上場前・上場廃止・ティッカー未解決）は、
    0 円扱いにも黙った削除にもしない。除外して残りで按分し、期ごとに 13F 上の比率つきで一覧する
  - 店頭（OTC）銘柄は日本のネット証券では通常買えないので、既定では写せない銘柄として除外し一覧する
  - 期中に取引が途絶えた銘柄は、最後の終値で据え置き（その値で現金化したのと同じ）、明記する

リターンの見方（CLAUDE.md の「リターン」は税・手数料後の、リスク調整後リターン）:
  - 総リターンだけでなく、年率÷ボラ・年率÷最大下落、マイナスの期の数、最も良かった 1 期を除いた結果、
    NISA 成長投資枠の消費（年ごとの買付額）を出す。総リターンの差が小さい・コスト次第で逆転する
    ベンチマークは「ほぼ同じ」と書き、「上回った」とは書かない
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_13f  # noqa: E402
import figi  # noqa: E402
import sectors as sec  # noqa: E402
from src.marketdata import MarketData  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CIK = "1045810"                       # NVIDIA CORP
DEFAULT_BENCH = ["SPY", "QQQ", "SMH", "NVDA", "ACWI"]
DEFAULT_COSTS = [0.0, 50.0]
FX_SYMBOL = "JPY=X"                           # 1 ドル = 何円
ALL_QUARTERS = 400                            # recent_13f に渡す件数。「全部」の意味

MAX_STALE = 5            # 営業日。これより古い価格しか無ければ「その日は売買できない」とみなす
TRADING_DAYS = 252
NY = ZoneInfo("America/New_York")
MARKET_CLOSE = dtime(16, 0)
CLOSE_SETTLED = dtime(16, 30)                 # この時刻（米東部）までは当日の足を未確定とみなす
EXCHANGE_TTL_SEC = 7 * 86400

# Yahoo の exchange 表記のうち店頭（OTC）市場のもの
OTC_EXCHANGES = {"PNK", "OTC", "OQX", "OQB", "OBB", "OEM", "OGM"}

MODE_JP = {
    "next": "提出日の翌営業日の終値（既定）",
    "accepted": "EDGAR 受理時刻の直後の終値",
    "same": "提出日の終値（楽観）",
    "report": "報告日（四半期末）の終値（実行不可能な参考値）",
}
WEIGHT_JP = {"value": "時価加重", "shares": "株数×エントリー日株価", "equal": "等金額"}
WEIGHT_DESC = {
    "value": "時価加重（13F の報告日の金額の比）",
    "shares": "株数×エントリー日株価（13F の株数をエントリー日の株価で評価した比）",
    "equal": "等金額（13F の比率は無視。1/銘柄数）",
}
ROUGHLY_EQUAL = 0.03     # 総リターンの差がこれ未満なら「ほぼ同じ」（運・コストの仮定で入れ替わる差）
NISA_GROWTH_MANYEN = 240  # NISA 成長投資枠の年間上限（万円）。買付額で減り、売っても年内は戻らない


# ------------------------------------------------------------ 13F の読み込み

def _iso(d) -> str | None:
    if d is None:
        return None
    return pd.Timestamp(d).date().isoformat()


def init_meta_tables(db: sqlite3.Connection) -> None:
    """受理時刻と、EDGAR の一覧がどこまで遡れているかを持つ表（fetch_13f の表には手を入れない）。"""
    db.executescript(
        """
        -- 旧版は submissions JSON の acceptanceDateTime を UTC として持っていたが、
        -- EDGAR の正式な受理時刻（米東部）と 4〜5 時間ずれていた。誤った値を使わないよう捨てる
        DROP TABLE IF EXISTS filing_accept;
        CREATE TABLE IF NOT EXISTS filing_acceptance (
            cik TEXT NOT NULL, accession TEXT NOT NULL, accepted_et TEXT, source TEXT,
            fetched_at REAL, PRIMARY KEY (cik, accession)
        );
        CREATE TABLE IF NOT EXISTS filer_coverage (
            cik TEXT PRIMARY KEY, recent_from TEXT, older_pages INTEGER, fetched_at REAL
        );
        CREATE TABLE IF NOT EXISTS listing_exchange (
            symbol TEXT PRIMARY KEY, exchange TEXT, fetched_at REAL
        );
        """
    )
    cols = {r[1] for r in db.execute("PRAGMA table_info(filer_coverage)")}
    if "tickers" not in cols:
        db.execute("ALTER TABLE filer_coverage ADD COLUMN tickers TEXT")
    db.commit()


EDGAR_OPEN, EDGAR_CLOSE = dtime(6, 0), dtime(22, 0)      # EDGAR の受付時間（米東部）
EDGAR_SAME_DAY_CUTOFF = dtime(17, 30)                     # これより後の受理は翌営業日付けの提出


def parse_acceptance_header(text: str) -> str | None:
    """EDGAR の -index-headers.html にある ACCEPTANCE-DATETIME（米東部の現地時刻）を ISO 文字列にする。"""
    m = re.search(r"ACCEPTANCE-DATETIME>\s*(\d{14})", text or "")
    if not m:
        return None
    t = pd.Timestamp(datetime.strptime(m.group(1), "%Y%m%d%H%M%S"))
    return t.tz_localize("America/New_York").isoformat()


def accepted_from_json(value: str | None, filed_date: str | None) -> str | None:
    """submissions JSON の acceptanceDateTime（末尾 Z）を米東部にしたもの。ヘッダーが取れない時の代わり。

    この値は EDGAR の正式な受理時刻より 4〜5 時間遅く出ることがある（実測）。
    EDGAR の受付時間（6:00〜22:00）の外や、17:30 以降なのに同じ日付けの提出（逆も）になる値は
    明らかにおかしいので捨てて None（accepted モードは翌営業日のエントリーになる）。
    """
    if not value:
        return None
    try:
        t = pd.Timestamp(value)
    except (ValueError, TypeError):
        return None
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    t = t.tz_convert("America/New_York")
    if not (EDGAR_OPEN <= t.time() <= EDGAR_CLOSE):
        return None
    if filed_date:
        fd = pd.Timestamp(filed_date).date()
        if t.time() > EDGAR_SAME_DAY_CUTOFF:
            if fd <= t.date():
                return None
        elif fd != t.date():
            return None
    return t.isoformat()


def fetch_filing_meta(ed: fetch_13f.Edgar, db: sqlite3.Connection, cik10: str) -> list[str]:
    """13F の受理時刻（米東部）と、EDGAR の一覧が遡れる範囲を取ってキャッシュする。注意書きを返す。

    受理時刻の正は、各提出の -index-headers.html にある ACCEPTANCE-DATETIME（米東部）。
    submissions JSON の acceptanceDateTime は 4〜5 時間ずれることがあるので、ヘッダーが取れない時だけ、
    つじつまが合う場合に限って使う。受理時刻は後から変わらないので、ヘッダーから取れた分は取り直さない。

    recent_13f は submissions の "recent" だけを見る。"recent" に入りきらない古い提出は
    別ページ（filings.files）にあるので、初回の 13F が本当に初回かどうかの判定に範囲を使う。
    """
    init_meta_tables(db)
    notes: list[str] = []
    r = ed.get(f"https://data.sec.gov/submissions/CIK{cik10}.json")
    if r.status_code != 200:
        return [f"EDGAR の提出一覧を取得できなかった（HTTP {r.status_code}）。受理時刻はキャッシュの値を使う"]
    d = r.json()
    rec = d.get("filings", {}).get("recent", {})
    dates = rec.get("filingDate") or []
    db.execute(
        "INSERT OR REPLACE INTO filer_coverage (cik, recent_from, older_pages, fetched_at, tickers)"
        " VALUES (?,?,?,?,?)",
        (cik10, min(dates) if dates else None, len(d.get("filings", {}).get("files") or []),
         time.time(), json.dumps(d.get("tickers") or [])),
    )
    db.commit()

    json_acc = rec.get("acceptanceDateTime") or []
    todo = [
        (rec["accessionNumber"][i], json_acc[i] if i < len(json_acc) else None,
         dates[i] if i < len(dates) else None)
        for i, f in enumerate(rec.get("form", []))
        if f in ("13F-HR", "13F-HR/A")
    ]
    cached = {a for (a,) in db.execute(
        "SELECT accession FROM filing_acceptance WHERE cik=? AND source='header'", (cik10,))}
    blocked = False
    fallback = []
    for acc, jv, fd in todo:
        if acc in cached:
            continue
        et = None
        if not blocked:
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/"
                   f"{acc.replace('-', '')}/{acc}-index-headers.html")
            try:
                h = ed.get(url)
                if h.status_code == 200:
                    et = parse_acceptance_header(h.text)
            except fetch_13f.SecBlocked:
                blocked = True
            except Exception:  # noqa: BLE001 — 取れなければ JSON の値で代える（注意書きを出す）
                et = None
        if et:
            db.execute("INSERT OR REPLACE INTO filing_acceptance VALUES (?,?,?,?,?)",
                       (cik10, acc, et, "header", time.time()))
        else:
            fb = accepted_from_json(jv, fd)
            fallback.append(acc)
            db.execute("INSERT OR REPLACE INTO filing_acceptance VALUES (?,?,?,?,?)",
                       (cik10, acc, fb, "submissions_json" if fb else None, time.time()))
    db.commit()
    if fallback:
        notes.append(
            f"受理時刻を EDGAR のヘッダーから取れなかった提出 {len(fallback)} 件は、submissions JSON の値"
            "（4〜5 時間遅く出ることがある）を、つじつまが合う場合だけ使った。合わないものは不明として"
            "翌営業日のエントリーにした: " + ", ".join(fallback))
    return notes


def load_filings(db: sqlite3.Connection, cik10: str) -> list[dict]:
    """キャッシュ済みの 13F を報告日の古い順に返す。プット・コールは options に分けてある。"""
    init_meta_tables(db)
    accepted = {a: (t, src) for a, t, src in db.execute(
        "SELECT accession, accepted_et, source FROM filing_acceptance WHERE cik=?", (cik10,))}
    out = []
    for rd, fd, acc, en, unit in db.execute(
        "SELECT report_date, filed_date, accession, edgar_name, value_unit FROM filers"
        " WHERE cik=? ORDER BY report_date", (cik10,)
    ).fetchall():
        pos = [
            {"cusip": c, "issuer": i, "class": cl, "value": float(v or 0.0),
             "shares": float(sh or 0.0), "share_type": st}
            for c, i, cl, v, sh, st in db.execute(
                "SELECT cusip, issuer, class, value, shares, share_type FROM positions"
                " WHERE cik=? AND report_date=? ORDER BY value DESC", (cik10, rd))
        ]
        opts = [
            {"cusip": c, "issuer": i, "put_call": pc, "value": float(v or 0.0),
             "shares": float(sh or 0.0)}
            for c, i, pc, v, sh in db.execute(
                "SELECT cusip, issuer, put_call, value, shares FROM options"
                " WHERE cik=? AND report_date=? ORDER BY value DESC", (cik10, rd))
        ]
        at, src = accepted.get(acc, (None, None))
        out.append({
            "report_date": rd, "filed_date": fd, "accession": acc, "edgar_name": en,
            "value_unit": unit, "accepted_et": at, "accepted_source": src if at else None,
            "positions": pos, "options": opts,
        })
    return out


def coverage(db: sqlite3.Connection, cik10: str) -> dict:
    init_meta_tables(db)
    row = db.execute("SELECT recent_from, older_pages, tickers FROM filer_coverage WHERE cik=?",
                     (cik10,)).fetchone()
    if not row:
        return {}
    try:
        tickers = json.loads(row[2]) if row[2] else []
    except ValueError:
        tickers = []
    return {"recent_from": row[0], "older_pages": row[1], "tickers": tickers}


def first_is_initial(filings: list[dict], cov: dict) -> bool | None:
    """最初の 13F が、この提出者にとって本当に初回の開示か。

    EDGAR の "recent" 一覧が、最初の 13F より 1 四半期以上前まで遡れていれば、
    それ以前の 13F は一覧に出ていたはずなので初回と言える。判断できなければ None。
    """
    if not filings or not cov:
        return None
    if not cov.get("older_pages"):
        return True                       # 古いページが無い = 全提出が一覧にある
    rf = cov.get("recent_from")
    if not rf:
        return None
    return pd.Timestamp(rf) <= pd.Timestamp(filings[0]["filed_date"]) - pd.Timedelta(days=100)


def attach_symbols(filings: list[dict], cmap: dict[str, dict]) -> None:
    """CUSIP → ティッカー（Yahoo の表記）を付ける。付けられない理由は issue に残す。"""
    for f in filings:
        for p in f["positions"]:
            m = cmap.get(p["cusip"]) or {}
            t = m.get("ticker")
            p["figi_name"] = m.get("name")
            p["symbol"] = None
            p["issue"] = None
            if p.get("share_type") and p["share_type"] != "SH":
                p["issue"] = "株数ではなく額面（PRN）で報告された債券など。株として写せない"
            elif not t or (isinstance(t, float) and math.isnan(t)):
                p["issue"] = "CUSIP からティッカーを解決できない（OpenFIGI に無い・取得失敗）"
            elif sec.is_non_common(t):
                p["issue"] = f"普通株以外（社債・優先株など: {t}）"
            else:
                p["symbol"] = sec.sec_ticker(t)          # BRK/B → BRK-B（Yahoo と同じ表記）


def listing_exchanges(db: sqlite3.Connection, symbols: list[str], refresh: bool = False,
                      fetch: bool = True) -> tuple[dict[str, str | None], list[str]]:
    """現在の上場市場（Yahoo の表記）。店頭銘柄の判定に使う。取れなければ None（判定しない）。"""
    init_meta_tables(db)
    now = time.time()
    out: dict[str, str | None] = {}
    todo = []
    for s in sorted(set(symbols)):
        row = db.execute("SELECT exchange, fetched_at FROM listing_exchange WHERE symbol=?",
                         (s,)).fetchone()
        if row and row[0] and not refresh and now - row[1] < EXCHANGE_TTL_SEC:
            out[s] = row[0]
        elif row and row[0] and not fetch:
            out[s] = row[0]
        else:
            todo.append(s)
    notes: list[str] = []
    if not todo:
        return out, notes
    if not fetch:
        notes.append(f"上場市場が未取得の銘柄 {len(todo)} 件は店頭かどうか判定していない: "
                     + ", ".join(todo))
        return out | {s: None for s in todo}, notes
    try:
        import yfinance as yf
    except ImportError:
        notes.append("yfinance が無いため上場市場（店頭かどうか）を判定していない")
        return out | {s: None for s in todo}, notes
    failed = []
    for s in todo:
        ex = None
        try:
            ex = yf.Ticker(s).fast_info["exchange"]
        except Exception:  # noqa: BLE001 — 取れなければ判定しないだけ
            ex = None
        out[s] = ex or None
        if ex:
            db.execute("INSERT OR REPLACE INTO listing_exchange VALUES (?,?,?)", (s, ex, now))
        else:
            failed.append(s)
        time.sleep(0.2)
    db.commit()
    if failed:
        notes.append("上場市場を取得できず、店頭かどうか判定していない銘柄: " + ", ".join(failed))
    return out, notes


# ------------------------------------------------------------ 価格・カレンダー

def price_frame(hist: dict[str, pd.DataFrame], field: str = "adj_close") -> pd.DataFrame:
    cols = {s: df[field].astype(float) for s, df in hist.items()
            if df is not None and not df.empty and field in df.columns}
    if not cols:
        return pd.DataFrame()
    return pd.DataFrame(cols).sort_index()


def trading_calendar(px: pd.DataFrame, prefer: list[str]) -> tuple[pd.DatetimeIndex, str]:
    """売買日のカレンダー。指数 ETF（先頭のベンチマーク）の取引日を使う。

    全銘柄の和集合にすると、1 銘柄だけに紛れた休日の足でエントリー日がずれる。
    """
    for s in prefer:
        if s in px.columns and px[s].notna().any():
            return pd.DatetimeIndex(px.index[px[s].notna()]), s
    return pd.DatetimeIndex(px.dropna(how="all").index), "全銘柄の取引日の和集合"


def drop_unsettled_close(cal: pd.DatetimeIndex, fetched: datetime | None = None,
                         now: datetime | None = None) -> tuple[pd.DatetimeIndex, str | None]:
    """取得した時点で終値が確定していなかった最後の足を落とす（落としたことは返して明記する）。

    判定は価格を取得した時刻（キャッシュの fetched_at）で行う。実行した時刻で判定すると、
    場中に取ってキャッシュした足を、引け後や翌朝の再実行で終値として使ってしまう。
    fetched が無い（キャッシュの記録が無い）ときだけ実行時刻 now で代用する。
    """
    if not len(cal):
        return cal, None
    ref = fetched or now or datetime.now(NY)
    ref = ref.astimezone(NY) if ref.tzinfo else ref.replace(tzinfo=NY)
    last = cal[-1].date()
    if last > ref.date() or (last == ref.date() and ref.time() < CLOSE_SETTLED):
        what = "取得時点" if fetched else "実行時点"
        return cal[:-1], (f"{last} の足は{what}（米東部 {ref:%Y-%m-%d %H:%M}）で終値が確定して"
                          f"いなかったため使わず、{cal[-2].date() if len(cal) > 1 else '—'} の終値までで計算")
    return cal, None


def last_bar_fetch_time(adj: pd.DataFrame, cal: pd.DatetimeIndex,
                        fetched_at: dict[str, datetime]) -> datetime | None:
    """カレンダー最終日に足がある銘柄のうち、最も早く取得されたものの取得時刻。

    1 銘柄でも場中に取った足が最終日に混ざっていれば、その日は終値として使わない。
    """
    if not len(cal) or adj.empty or cal[-1] not in adj.index:
        return None
    row = adj.loc[cal[-1]]
    ts = [fetched_at[s] for s in adj.columns if s in fetched_at and pd.notna(row[s])]
    return min(ts) if ts else None


class Prices:
    """カレンダーにそろえた調整後終値。raw は欠損のまま、ff は直前の値で埋めたもの。"""

    def __init__(self, adj: pd.DataFrame, cal: pd.DatetimeIndex):
        self.cal = pd.DatetimeIndex(cal)
        self.raw = adj.reindex(self.cal) if not adj.empty else pd.DataFrame(index=self.cal)
        self.ff = self.raw.ffill()
        self.valid = {s: np.flatnonzero(self.raw[s].notna().to_numpy()) for s in self.raw.columns}

    def date(self, i: int) -> str:
        return self.cal[i].date().isoformat()

    def pos(self, d) -> int:
        return int(self.cal.get_loc(pd.Timestamp(d)))

    def on_or_before(self, d) -> int | None:
        i = int(self.cal.searchsorted(pd.Timestamp(d), side="right")) - 1
        return i if i >= 0 else None

    def before(self, d) -> int | None:
        i = int(self.cal.searchsorted(pd.Timestamp(d), side="left")) - 1
        return i if i >= 0 else None

    def check(self, s: str, i: int) -> str | None:
        """位置 i で売買できる価格があるか。無ければ理由を返す（あれば None）。"""
        v = self.valid.get(s)
        if v is None or len(v) == 0:
            return "価格データが無い（Yahoo に未収録・非上場・上場廃止の可能性）"
        k = int(v.searchsorted(i, side="right")) - 1
        if k < 0:
            return f"{self.date(int(v[0]))} から取引開始。この日にはまだ価格が無い（上場前）"
        j = int(v[k])
        if i - j > MAX_STALE:
            return f"{self.date(j)} を最後に価格が無い（上場廃止・取引停止の可能性）"
        return None

    def px(self, s: str, i: int) -> float:
        return float(self.ff[s].iat[i])

    def gap(self, s: str, ie: int, ix: int) -> dict | None:
        """保有期間 (ie, ix] の取引途絶・長い欠けを調べる。"""
        v = self.valid.get(s, np.array([], dtype=int))
        k = int(v.searchsorted(ix, side="right")) - 1
        last = int(v[k]) if k >= 0 else None
        if last is not None and ix - last > MAX_STALE:
            return {"kind": "stopped", "last_date": self.date(last), "missing_days": ix - last,
                    "resumed_later": bool((v > ix).any())}
        inside = v[(v > ie) & (v <= ix)]
        pts = np.r_[ie, inside]
        if len(pts) >= 2:
            gaps = np.diff(pts) - 1
            g = int(gaps.argmax())
            if gaps[g] > MAX_STALE:
                return {"kind": "gap", "from": self.date(int(pts[g])),
                        "to": self.date(int(pts[g + 1])), "missing_days": int(gaps[g])}
        return None


def entry_date(cal: pd.DatetimeIndex, filing: dict, mode: str) -> pd.Timestamp | None:
    """この提出を見て組み替えられる最初の終値の日。

    next     … 提出日より後の最初の営業日（提出日が金曜なら月曜、休日なら次の営業日）
    same     … 提出日当日（休日なら次の営業日）。引け後の提出では実際には買えない楽観ケース
    accepted … 受理時刻が米東部 16:00 より前の営業日ならその日、そうでなければ次の営業日。
               受理時刻が無ければ next と同じ
    report   … 報告日（四半期末）以前の最後の営業日。開示前なので実行不可能。参考値専用
    """
    if mode == "report":
        i = int(cal.searchsorted(pd.Timestamp(filing["report_date"]), side="right")) - 1
        return cal[i] if i >= 0 else None
    fd = pd.Timestamp(filing["filed_date"])
    if mode == "same":
        i = int(cal.searchsorted(fd, side="left"))
    elif mode == "accepted" and filing.get("accepted_et"):
        t = pd.Timestamp(filing["accepted_et"])
        d = pd.Timestamp(t.date())
        before_close = t.time() < MARKET_CLOSE
        i = int(cal.searchsorted(d, side="left" if before_close else "right"))
    elif mode in ("next", "accepted"):
        i = int(cal.searchsorted(fd, side="right"))
    else:
        raise ValueError(f"unknown entry mode: {mode}")
    return cal[i] if i < len(cal) else None


def build_periods(filings: list[dict], cal: pd.DatetimeIndex, mode: str
                  ) -> tuple[list[dict], list[str]]:
    """提出ごとの保有期間 [エントリー, 次のエントリー]。最後は最新の終値まで。"""
    notes: list[str] = []
    cands = []
    for f in filings:
        e = entry_date(cal, f, mode)
        if e is None:
            notes.append(f"{f['report_date']} 期（{f['filed_date']} 提出）は、提出後の終値がまだ"
                         "無いため組み入れていない")
            continue
        cands.append((f, e))
    periods = []
    for k, (f, e) in enumerate(cands):
        x = cands[k + 1][1] if k + 1 < len(cands) else cal[-1]
        if x <= e:
            notes.append(f"{f['report_date']} 期はエントリー日 {e.date()} の後に価格が無い"
                         "（または次の提出と同じ日に組み替える）ため、保有期間が無い")
            continue
        periods.append({"filing": f, "entry": e, "exit": x})
    return periods, notes


# ------------------------------------------------------------ バスケット

def basket_rows(filing: dict) -> list[dict]:
    """提出の現物株を銘柄（ティッカー、無ければ CUSIP）単位にまとめ、13F 上の比率を付ける。"""
    rows: dict[str, dict] = {}
    for p in filing["positions"]:
        key = p.get("symbol") or f"CUSIP:{p['cusip']}"
        r = rows.setdefault(key, {
            "key": key, "symbol": p.get("symbol"), "issuer": p.get("issuer"),
            "cusips": [], "value": 0.0, "issue": p.get("issue"),
        })
        r["value"] += float(p.get("value") or 0.0)
        if p["cusip"] not in r["cusips"]:
            r["cusips"].append(p["cusip"])
    total = sum(r["value"] for r in rows.values())
    for r in rows.values():
        r["reported_weight"] = r["value"] / total if total > 0 else None
    return sorted(rows.values(), key=lambda r: -r["value"])


def otc_reason(symbol: str | None, exchanges: dict[str, str | None] | None,
               include_otc: bool) -> str | None:
    ex = (exchanges or {}).get(symbol) if symbol else None
    if ex in OTC_EXCHANGES and not include_otc:
        return (f"店頭（OTC: {ex}）銘柄。日本のネット証券では通常買えないため写せない銘柄"
                "として除外（市場は現在の Yahoo の表示で判定）")
    return None


def select_basket(filing: dict, prices: Prices, i_entry: int,
                  exchanges: dict[str, str | None] | None = None,
                  include_otc: bool = False, i_report: int | None = None) -> dict:
    """エントリー日に買える銘柄だけで組む。買えない銘柄は理由と 13F 上の比率つきで返す。

    比率は 3 通り:
      value  … 13F の金額（報告日の時価）の比
      shares … 13F の株数 × エントリー日の株価の比（= 報告日の金額を、エントリーまでの値動きでずらしたもの。
               分割にも強いよう調整後終値の比で動かす）。報告日の価格が無い銘柄は 13F の金額のまま
      equal  … 1/銘柄数（13F の比率は無視）
    """
    priced, unpriced = [], []
    for r in basket_rows(filing):
        reason = r.get("issue") or otc_reason(r["symbol"], exchanges, include_otc)
        if reason is None:
            reason = prices.check(r["symbol"], i_entry)
        (unpriced if reason else priced).append({**r, "reason": reason})
    pv = sum(r["value"] for r in priced)
    drifted, no_report_px = {}, []
    for r in priced:
        s = r["symbol"]
        if i_report is not None and i_report <= i_entry and not prices.check(s, i_report):
            drifted[s] = r["value"] * prices.px(s, i_entry) / prices.px(s, i_report)
        else:
            drifted[s] = r["value"]
            no_report_px.append(s)
    dv = sum(drifted.values())
    weights = {
        "value": {r["symbol"]: r["value"] / pv for r in priced} if pv > 0 else {},
        "shares": {s: v / dv for s, v in drifted.items()} if dv > 0 else {},
        "equal": {r["symbol"]: 1.0 / len(priced) for r in priced} if priced else {},
    }
    return {
        "priced": priced, "unpriced": unpriced, "weights": weights,
        "unpriced_weight": sum(r["reported_weight"] or 0.0 for r in unpriced),
        "shares_without_report_price": no_report_px,
    }


# ------------------------------------------------------------ シミュレーション

def simulate(periods: list[dict], baskets: list[dict], prices: Prices, weights: str,
             cost_bps: float) -> dict:
    """各エントリーで組み替え、期中は持ちっぱなし。資金 1.0 から始める。

    コスト = 片道コスト × Σ|目標の金額 − 組み替え前の金額|（組み替え前の資産額で目標を計算）。
    最初の購入は全額が売買になる。最後の売却コストは課さない（ベンチマークも同じ）。
    """
    c = cost_bps / 1e4
    units: dict[str, float] = {}
    cash = 0.0
    nav: dict[int, float] = {}
    pnl: dict[str, float] = defaultdict(float)
    per = []
    cost_total = 0.0
    for p, b in zip(periods, baskets, strict=True):
        ie, ix = p["ie"], p["ix"]
        if not nav:
            nav_pre, old = 1.0, {}
        else:
            old = {s: u * prices.px(s, ie) for s, u in units.items()}
            nav_pre = sum(old.values()) + cash
        w = b["weights"][weights]
        target = {s: wi * nav_pre for s, wi in w.items()}
        traded = sum(abs(target.get(s, 0.0) - old.get(s, 0.0)) for s in set(target) | set(old))
        buys = sum(max(target.get(s, 0.0) - old.get(s, 0.0), 0.0) for s in set(target) | set(old))
        cost = c * traded
        nav_post = nav_pre - cost
        units = {s: wi * nav_post / prices.px(s, ie) for s, wi in w.items()}
        cash = 0.0 if units else nav_post      # 買える銘柄が無い期は現金で持つ（報告に出る）
        nav[ie] = nav_post
        if units:
            syms = list(units)
            vals = prices.ff[syms].iloc[ie + 1: ix + 1].to_numpy() @ np.array([units[s] for s in syms])
        else:
            vals = np.zeros(ix - ie)
        for k, v in enumerate(vals, start=ie + 1):
            nav[k] = float(v) + cash
        for s, u in units.items():
            pnl[s] += u * (prices.px(s, ix) - prices.px(s, ie))
        cost_total += cost
        per.append({"return": nav[ix] / nav_pre - 1.0, "turnover": traded / nav_pre if nav_pre else 0.0,
                    "cost": cost, "buys": buys})
    series = pd.Series({prices.cal[i]: v for i, v in sorted(nav.items())}, dtype=float)
    return {"nav": series, "periods": per, "pnl": dict(pnl), "cost_total": cost_total}


def benchmark_nav(prices: Prices, sym: str, i0: int, i1: int, cost_bps: float
                  ) -> tuple[pd.Series | None, str | None]:
    """同じ日付の範囲で買って持ち続けた場合。最初の購入にだけ片道コストを掛ける。"""
    why = prices.check(sym, i0)
    if why:
        return None, why
    p = prices.ff[sym].iloc[i0: i1 + 1]
    return (1.0 - cost_bps / 1e4) * p / p.iloc[0], None


# ------------------------------------------------------------ 指標

def max_drawdown(s: pd.Series) -> dict:
    """最大下落率と、そのピーク・底・回復の日付。"""
    s = s.dropna()
    empty = {"mdd": 0.0, "peak": None, "trough": None, "recovery": None}
    if len(s) < 2:
        return empty
    dd = s / s.cummax() - 1.0
    t = dd.idxmin()
    m = float(dd.loc[t])
    if m >= 0:
        return empty
    before = s.loc[:t]
    pk = before[before == before.max()].index[-1]
    after = s.loc[t:]
    rec = after[after >= s.loc[pk]]
    return {"mdd": m, "peak": _iso(pk), "trough": _iso(t),
            "recovery": _iso(rec.index[0]) if len(rec) else None}


def _cagr(total: float, years: float) -> float | None:
    if years <= 0 or total <= -1:
        return None
    return (1.0 + total) ** (1.0 / years) - 1.0


def summarize(nav: pd.Series, fx: pd.Series | None, period_returns: list[float],
              labels: list[str]) -> dict:
    """資金 1.0 から始めた資産額の系列の指標。円建ては 1 ドル = fx 円で換算する。"""
    start, end = nav.index[0], nav.index[-1]
    years = (end - start).days / 365.25
    total = float(nav.iloc[-1]) - 1.0
    rets = nav.pct_change().dropna()
    out = {
        "start": _iso(start), "end": _iso(end), "years": years,
        "total_return_usd": total, "cagr_usd": _cagr(total, years),
        "vol_ann": float(rets.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(rets) > 1 else None,
        "max_drawdown_usd": max_drawdown(nav),
        "total_return_jpy": None, "cagr_jpy": None, "max_drawdown_jpy": None,
        "worst_period": None,
    }
    if fx is not None:
        f = fx.reindex(nav.index)
        f0, f1 = f.iloc[0], f.iloc[-1]
        if pd.notna(f0) and pd.notna(f1) and f0 > 0:
            tj = float(nav.iloc[-1]) * float(f1) / float(f0) - 1.0
            out["total_return_jpy"] = tj
            out["cagr_jpy"] = _cagr(tj, years)
            out["max_drawdown_jpy"] = max_drawdown(nav * f / f0)
    if period_returns:
        k = int(np.argmin(period_returns))
        out["worst_period"] = {"return": float(period_returns[k]), "report_date": labels[k]}
        out["losing_periods"] = sum(1 for x in period_returns if x < 0)
        out["n_periods"] = len(period_returns)
    # リスク当たりのリターン（CLAUDE.md の「リターン」はリスク調整後）。ボラ・最大下落 1 単位あたりの年率
    cg, vol, mdd = out["cagr_usd"], out["vol_ann"], out["max_drawdown_usd"]["mdd"]
    out["ret_per_vol"] = cg / vol if cg is not None and vol else None
    out["calmar"] = cg / abs(mdd) if cg is not None and mdd < 0 else None
    return out


# ------------------------------------------------------------ 開示前の値動き

def weighted_return(rows: list[dict], prices: Prices, i0: int | None, i1: int | None) -> dict:
    """rows（13F の金額つき）を i0 の終値で買い i1 の終値まで持った場合の、時価加重と等金額。"""
    out = {"value": None, "equal": None, "n": 0, "excluded": []}
    if i0 is None or i1 is None or i1 < i0:
        return out
    got = []
    for r in rows:
        s = r.get("symbol")
        why = r.get("issue") or (prices.check(s, i0) if s else "ティッカー無し")
        if not why and prices.check(s, i1):
            why = prices.check(s, i1)
        if why:
            out["excluded"].append({"key": r["key"], "issuer": r.get("issuer"), "reason": why})
            continue
        got.append((r["value"], prices.px(s, i1) / prices.px(s, i0) - 1.0))
    if not got:
        return out
    tv = sum(v for v, _ in got)
    out["value"] = sum(v * x for v, x in got) / tv if tv > 0 else None
    out["equal"] = sum(x for _, x in got) / len(got)
    out["n"] = len(got)
    return out


def pre_disclosure_close(prices: Prices, filing: dict) -> tuple[int | None, str]:
    """開示前の最後の終値の位置と、その決め方。

    受理が米東部 16:00 以降なら、その日の終値はまだ開示前なので、その日の終値。
    16:00 より前（寄り付き前・場中）なら、前の営業日の終値。受理日が休日なら直前の営業日。
    受理時刻が分からなければ提出日の前営業日の終値（17:30 以降の受理は翌営業日付けになるので、
    これより後に開示されたことは確実。ただし開示前の 1 日分を含みうる）。
    """
    acc = filing.get("accepted_et")
    if acc:
        t = pd.Timestamp(acc)
        d = pd.Timestamp(t.date())
        if t.time() >= MARKET_CLOSE:
            return prices.on_or_before(d), "受理が引け後: 受理日の終値"
        return prices.before(d), "受理が引け前: 受理日の前営業日の終値"
    return prices.before(filing["filed_date"]), "受理時刻不明: 提出日の前営業日の終値"


def missed_returns(filings: list[dict], prices: Prices, mode: str, bench: str | None,
                   periods: list[dict], initial: bool | None,
                   exchanges: dict[str, str | None] | None = None,
                   include_otc: bool = False) -> tuple[list[dict], list[dict]]:
    """フォロワーが取れなかった値動き。

    - 報告日の終値 → エントリーの終値: その 13F の保有（13F の金額で加重）の値動き。
      報告日の時点で持っていた人だけが取れた分。フォロワーが写せる銘柄（店頭などを除く。戦略と同じ）
      で出し、13F の全銘柄での値も別に持つ
    - 新規に開示された銘柄の、開示前の最後の終値 → エントリーの終値（開示ポップ）。
      開示前の終値は受理時刻で決める（pre_disclosure_close）
    """
    cal = prices.cal
    exit_by_rd = {p["filing"]["report_date"]: p for p in periods}
    rows, new_rows = [], []
    prev_keys: set[str] | None = None

    def not_copyable(r):
        return r.get("issue") or otc_reason(r.get("symbol"), exchanges, include_otc)

    for k, f in enumerate(filings):
        e = entry_date(cal, f, mode)
        ie = prices.pos(e) if e is not None else None
        i_rd = prices.on_or_before(f["report_date"])
        i_pre, pre_rule = pre_disclosure_close(prices, f)
        brs = basket_rows(f)
        keys = {r["key"] for r in brs}
        if k == 0:
            new = brs if initial else []
            new_label = ("初回の開示。前回の 13F なし" if initial
                         else "前回の 13F が一覧に無く、新規かどうか判断できない")
        else:
            new = [r for r in brs if r["key"] not in (prev_keys or set())]
            new_label = None
        copyable = [r for r in brs if not not_copyable(r)]
        new_copyable = [r for r in new if not not_copyable(r)]
        pre = weighted_return(copyable, prices, i_rd, ie)
        pre_all = weighted_return(brs, prices, i_rd, ie)
        pop = weighted_return(new_copyable, prices, i_pre, ie)

        def bret(i0, i1):
            if bench is None or i0 is None or i1 is None:
                return None
            if prices.check(bench, i0) or prices.check(bench, i1):
                return None
            return prices.px(bench, i1) / prices.px(bench, i0) - 1.0

        per = exit_by_rd.get(f["report_date"])
        rows.append({
            "report_date": f["report_date"], "filed_date": f["filed_date"],
            "accepted_et": f.get("accepted_et"),
            "report_close": prices.date(i_rd) if i_rd is not None else None,
            "pre_filing_close": prices.date(i_pre) if i_pre is not None else None,
            "pre_filing_rule": pre_rule,
            "entry": _iso(e),
            "pre_disclosure": pre, "pre_disclosure_all_holdings": pre_all,
            "pre_disclosure_bench": bret(i_rd, ie),
            "new_names": [r["symbol"] or r["key"] for r in new], "new_label": new_label,
            "pop": pop, "pop_bench": bret(i_pre, ie),
        })
        for r in new:
            s = r.get("symbol")
            pop_r = after = None
            why = r.get("issue")
            if s and not why and i_pre is not None and ie is not None:
                why = prices.check(s, i_pre) or prices.check(s, ie)
                if not why:
                    pop_r = prices.px(s, ie) / prices.px(s, i_pre) - 1.0
                    if per is not None:
                        after = prices.px(s, per["ix"]) / prices.px(s, ie) - 1.0
            nc = not_copyable(r)
            new_rows.append({
                "report_date": f["report_date"], "symbol": s, "issuer": r.get("issuer"),
                "reported_weight": r["reported_weight"], "pop": pop_r,
                "pre_filing_close": prices.date(i_pre) if i_pre is not None else None,
                "after_entry_to_exit": after, "exit": per and prices.date(per["ix"]),
                "excluded_reason": why, "copyable": not nc, "not_copyable_reason": nc,
            })
        prev_keys = keys
    return rows, new_rows


# ------------------------------------------------------------ 銘柄ごと

def holding_stats(periods: list[dict], baskets: list[dict], prices: Prices,
                  pnl_by_strategy: dict[str, dict[str, float]]) -> list[dict]:
    """写したポートフォリオが持っていた間の、銘柄ごとの最大下落と寄与。"""
    spans: dict[str, list[list[int]]] = defaultdict(list)
    issuer: dict[str, str] = {}
    held_rd: dict[str, list[str]] = defaultdict(list)
    for p, b in zip(periods, baskets, strict=True):
        for r in b["priced"]:
            s = r["symbol"]
            issuer.setdefault(s, r.get("issuer"))
            held_rd[s].append(p["filing"]["report_date"])
            if spans[s] and spans[s][-1][1] == p["ie"]:
                spans[s][-1][1] = p["ix"]                     # 続けて保有 → 1 つの保有期間
            else:
                spans[s].append([p["ie"], p["ix"]])
    out = []
    for s, sp in spans.items():
        worst = {"mdd": 0.0, "peak": None, "trough": None, "recovery": None}
        growth = 1.0
        for ie, ix in sp:
            seg = prices.ff[s].iloc[ie: ix + 1]
            growth *= float(seg.iloc[-1] / seg.iloc[0])
            dd = max_drawdown(seg)
            if dd["mdd"] < worst["mdd"]:
                worst = dd
        out.append({
            "symbol": s, "issuer": issuer.get(s), "periods_held": len(held_rd[s]),
            "first_report": held_rd[s][0], "last_report": held_rd[s][-1],
            "held_from": prices.date(sp[0][0]), "held_to": prices.date(sp[-1][1]),
            "return_while_held": growth - 1.0, "max_drawdown_while_held": worst,
            "contribution": {k: v.get(s, 0.0) for k, v in pnl_by_strategy.items()},
        })
    return sorted(out, key=lambda r: r["max_drawdown_while_held"]["mdd"])


# ------------------------------------------------------------ 本体（ネットワーク無し）

def mask_zero_volume(adj: pd.DataFrame, volume: pd.DataFrame | None,
                     keep: list[str] | None = None) -> tuple[pd.DataFrame, dict[str, int]]:
    """出来高 0 の日の終値は売買が成立していない（気配や前日値の持ち越し）ので、価格なしとして扱う。

    keep（ベンチマーク・カレンダー用）は触らない。落とした日数を銘柄ごとに返す。
    """
    if volume is None or volume.empty or adj.empty:
        return adj, {}
    vol = volume.reindex(index=adj.index, columns=adj.columns)
    zero = (vol == 0) & adj.notna()
    for k in keep or []:
        if k in zero.columns:
            zero[k] = False
    counts = {s: int(n) for s, n in zero.sum().items() if n}
    return (adj.mask(zero) if counts else adj), counts


def yearly_buys(sim_periods: list[dict], entries: list[pd.Timestamp], fx_al: pd.Series | None,
                prices: Prices, i0: int) -> dict[str, float]:
    """年ごとの買付額（初期資金に対する倍率）。NISA の成長投資枠は買付額で減り、売っても年内は戻らない。

    為替があれば円換算（枠は円の買付額で数える）。初回の購入（1.0 倍）を含む。
    """
    out: dict[str, float] = defaultdict(float)
    f0 = float(fx_al.iloc[i0]) if fx_al is not None and pd.notna(fx_al.iloc[i0]) else None
    for per, e in zip(sim_periods, entries, strict=True):
        k = 1.0
        if f0:
            fe = fx_al.loc[e]
            k = float(fe) / f0 if pd.notna(fe) else 1.0
        out[str(e.year)] += per["buys"] * k
    return dict(out)


def run_backtest(filings: list[dict], adj: pd.DataFrame, cal: pd.DatetimeIndex,
                 fx: pd.Series | None, bench: list[str], weight_modes: list[str],
                 costs: list[float], mode: str = "next",
                 exchanges: dict[str, str | None] | None = None, include_otc: bool = False,
                 initial: bool | None = True, volume: pd.DataFrame | None = None) -> dict:
    """13F の写し方の検証一式。価格とカレンダーを渡せばネットワークに出ない。"""
    adj, zero_vol = mask_zero_volume(adj, volume, keep=list(bench))
    prices = Prices(adj, cal)
    periods, notes = build_periods(filings, prices.cal, mode)
    if not periods:
        raise ValueError("保有期間を作れる 13F がない（提出後の価格が無い）")
    for p in periods:
        p["ie"], p["ix"] = prices.pos(p["entry"]), prices.pos(p["exit"])
    baskets = [select_basket(p["filing"], prices, p["ie"], exchanges, include_otc,
                             i_report=prices.on_or_before(p["filing"]["report_date"]))
               for p in periods]
    i0, i1 = periods[0]["ie"], periods[-1]["ix"]
    labels = [p["filing"]["report_date"] for p in periods]
    fx_al = None
    if fx is not None and not fx.dropna().empty:
        fx_al = fx.dropna().reindex(fx.dropna().index.union(prices.cal)).ffill().reindex(prices.cal)

    strategies, sims = [], {}
    for w in weight_modes:
        for c in costs:
            sim = simulate(periods, baskets, prices, w, c)
            sims[(w, c)] = sim
            rets = [x["return"] for x in sim["periods"]]
            strategies.append({
                "weights": w, "cost_bps": c, "metrics": summarize(sim["nav"], fx_al, rets, labels),
                "period_returns": rets, "turnover": [x["turnover"] for x in sim["periods"]],
                "cost_total": sim["cost_total"],
                "buys_by_year": yearly_buys(sim["periods"], [p["entry"] for p in periods],
                                            fx_al, prices, i0),
                "buys_in_jpy": fx_al is not None,
                "contributions": dict(sorted(sim["pnl"].items(), key=lambda kv: -kv[1])),
            })

    benchmarks, bench_missing, bench_navs = [], [], {}
    for b in bench:
        for c in costs:
            nav, why = benchmark_nav(prices, b, i0, i1, c)
            if nav is None:
                if c == costs[0]:
                    bench_missing.append({"symbol": b, "reason": why})
                continue
            rets = []
            for k, p in enumerate(periods):
                r = prices.px(b, p["ix"]) / prices.px(b, p["ie"]) - 1.0
                rets.append((1.0 + r) * (1.0 - c / 1e4) - 1.0 if k == 0 else r)
            benchmarks.append({"symbol": b, "cost_bps": c,
                               "metrics": summarize(nav, fx_al, rets, labels),
                               "period_returns": rets})
            bench_navs[(b, c)] = nav

    c0 = min(costs)
    first_bench = next((b for b in bench if (b, c0) in bench_navs), None)

    # 一時的な勝ちに頼っていないか: 先頭のベンチマークに勝った期の数と、最も良かった 1 期を除いた結果
    for st in strategies:
        fb = next((x for x in benchmarks if x["symbol"] == first_bench
                   and x["cost_bps"] == st["cost_bps"]), None)
        rets = st["period_returns"]
        k = int(np.argmax(rets))
        st["best_period"] = {"report_date": labels[k], "return": rets[k]}
        st["total_without_best_period"] = float(np.prod([1 + r for j, r in enumerate(rets) if j != k]) - 1)
        if fb:
            br = fb["period_returns"]
            st["periods_beating_first_bench"] = sum(1 for a, b2 in zip(rets, br) if a > b2)
            st["first_bench_without_best_period"] = float(
                np.prod([1 + r for j, r in enumerate(br) if j != k]) - 1)

    per_rows = []
    for k, (p, b) in enumerate(zip(periods, baskets, strict=True)):
        f = p["filing"]
        gaps = []
        for r in b["priced"]:
            g = prices.gap(r["symbol"], p["ie"], p["ix"])
            if g:
                gaps.append({"symbol": r["symbol"], "issuer": r.get("issuer"), **g})
        per_rows.append({
            "report_date": f["report_date"], "filed_date": f["filed_date"],
            "accepted_et": f.get("accepted_et"), "accepted_source": f.get("accepted_source"),
            "entry": prices.date(p["ie"]),
            "exit": prices.date(p["ix"]), "n_positions": len(b["priced"]) + len(b["unpriced"]),
            "n_priced": len(b["priced"]),
            "priced": [{"symbol": r["symbol"], "issuer": r.get("issuer"),
                        "reported_weight": r["reported_weight"],
                        "weight_value": b["weights"]["value"].get(r["symbol"]),
                        "weight_shares": b["weights"]["shares"].get(r["symbol"]),
                        "weight_equal": b["weights"]["equal"].get(r["symbol"])}
                       for r in b["priced"]],
            "unpriced": [{"key": r["key"], "symbol": r["symbol"], "issuer": r.get("issuer"),
                          "cusips": r["cusips"], "reported_weight": r["reported_weight"],
                          "reason": r["reason"]} for r in b["unpriced"]],
            "unpriced_weight": b["unpriced_weight"],
            "shares_without_report_price": b["shares_without_report_price"],
            "options": f.get("options", []),
            "returns": {w: sims[(w, c0)]["periods"][k]["return"] for w in weight_modes},
            "turnover": {w: sims[(w, c0)]["periods"][k]["turnover"] for w in weight_modes},
            "bench_returns": {bm["symbol"]: bm["period_returns"][k]
                              for bm in benchmarks if bm["cost_bps"] == c0},
            "trading_gaps": gaps,
        })
        if not b["priced"]:
            notes.append(f"{f['report_date']} 期は買える銘柄が無く、現金（0%）で持ったことにした")
    if zero_vol:
        notes.append("出来高 0 の日の終値は売買できない価格として使わなかった: "
                     + ", ".join(f"{s} {n} 日" for s, n in sorted(zero_vol.items())))

    missed, new_names = missed_returns(filings, prices, mode, first_bench, periods, initial,
                                       exchanges, include_otc)
    holdings = holding_stats(periods, baskets, prices,
                             {w: sims[(w, c0)]["pnl"] for w in weight_modes})

    nav_out = {"dates": [_iso(d) for d in sims[(weight_modes[0], c0)]["nav"].index]}
    series = {}
    for (w, c), sim in sims.items():
        series[f"{w}_{c:g}bp"] = [round(float(x), 6) for x in sim["nav"].to_numpy()]
    for (b, c), nav in bench_navs.items():
        series[f"{b}_{c:g}bp"] = [round(float(x), 6) for x in nav.to_numpy()]
    nav_out["series"] = series

    return {
        "mode": mode, "start": prices.date(i0), "end": prices.date(i1),
        "cost_bps": costs, "weights": weight_modes, "bench": bench,
        "first_bench": first_bench, "include_otc": include_otc,
        "fx_start": float(fx_al.iloc[i0]) if fx_al is not None and pd.notna(fx_al.iloc[i0]) else None,
        "fx_end": float(fx_al.iloc[i1]) if fx_al is not None and pd.notna(fx_al.iloc[i1]) else None,
        "periods": per_rows, "strategies": strategies, "benchmarks": benchmarks,
        "bench_unavailable": bench_missing, "missed": missed, "new_names": new_names,
        "holdings": holdings, "notes": notes, "nav": nav_out, "zero_volume_days": zero_vol,
    }


# ------------------------------------------------------------ 出力

def _pct(x, signed: bool = True, digits: int = 1) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "—"
    v = round(x * 100, digits) + 0.0          # -0.0 を出さない
    return f"{v:+.{digits}f}%" if signed else f"{v:.{digits}f}%"


def _w(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _et(s: str | None) -> str:
    if not s:
        return "—"
    t = pd.Timestamp(s)
    return f"{t:%m-%d %H:%M}"


def _strategy(res: dict, w: str, c: float) -> dict | None:
    return next((s for s in res["strategies"] if s["weights"] == w and s["cost_bps"] == c), None)


def _bench(res: dict, b: str, c: float) -> dict | None:
    return next((s for s in res["benchmarks"] if s["symbol"] == b and s["cost_bps"] == c), None)


def _clean(obj):
    """JSON に NaN・numpy 型・Timestamp を出さない。"""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return obj.isoformat()
    return obj


def _ratio(x) -> str:
    return "—" if x is None or not math.isfinite(x) else f"{x:.2f}"


def compare_to_benchmarks(res: dict, w: str) -> dict[str, list[str]]:
    """総リターンでの比較を、全てのコストで ROUGHLY_EQUAL 以上の差がある場合だけ「上回った・下回った」にする。

    差が小さい、またはコストの仮定で逆転するものは「ほぼ同じ」。リスク当たり（年率÷ボラ）の比較も返す。
    """
    out = {"beats": [], "roughly_equal": [], "loses": [], "beats_risk_adj": [], "loses_risk_adj": []}
    c0 = min(res["cost_bps"])
    for b in res["bench"]:
        margins = []
        for c in res["cost_bps"]:
            s, bm = _strategy(res, w, c), _bench(res, b, c)
            if s and bm:
                margins.append(s["metrics"]["total_return_usd"] - bm["metrics"]["total_return_usd"])
        if not margins:
            continue
        if all(m >= ROUGHLY_EQUAL for m in margins):
            out["beats"].append(b)
        elif all(m <= -ROUGHLY_EQUAL for m in margins):
            out["loses"].append(b)
        else:
            out["roughly_equal"].append(b)
        s, bm = _strategy(res, w, c0), _bench(res, b, c0)
        a, z = s["metrics"].get("ret_per_vol"), bm["metrics"].get("ret_per_vol")
        if a is not None and z is not None:
            (out["beats_risk_adj"] if a > z else out["loses_risk_adj"]).append(b)
    return out


def acceptance_counts(periods: list[dict]) -> dict:
    """受理時刻（米東部）の分布。引け後（16:00 以降）の受理は、提出日の終値では買えない。"""
    acc = [pd.Timestamp(p["accepted_et"]) for p in periods if p.get("accepted_et")]
    late = sum(1 for a in acc if a.time() >= MARKET_CLOSE)
    early = sum(1 for a in acc if a.time() < dtime(9, 30))
    return {"known": len(acc), "unknown": len(periods) - len(acc), "after_close": late,
            "pre_open": early, "intraday": len(acc) - late - early}


def _headline(res: dict, name: str, sensitivity: list[dict] | None = None) -> list[str]:
    c0 = min(res["cost_bps"])
    hi = max(res["cost_bps"])
    fb = res.get("first_bench")
    lines = []
    m = res["periods"]
    lines.append(
        f"対象は {name} の 13F {len(m)} 回分（報告日 {m[0]['report_date']}〜{m[-1]['report_date']}）。"
        f"開示を見てから写す人が持てたのは {res['start']}〜{res['end']}"
        f"（約 {(pd.Timestamp(res['end']) - pd.Timestamp(res['start'])).days / 365.25:.1f} 年）。"
    )
    parts = []
    for w in res["weights"]:
        s = _strategy(res, w, c0)
        if s:
            mm = s["metrics"]
            parts.append(f"{WEIGHT_JP[w]} {_pct(mm['total_return_usd'])}（年率 {_pct(mm['cagr_usd'])}）")
    bparts = []
    for b in res["bench"]:
        bm = _bench(res, b, c0)
        if bm:
            bparts.append(f"{b} {_pct(bm['metrics']['total_return_usd'])}")
    lines.append(f"{MODE_JP[res['mode']]}で組み替えた場合（片道コスト {c0:g}bp、ドル建て）: "
                 + "、".join(parts) + "。同じ期間を買って持つだけなら " + "、".join(bparts) + "。")

    # リスクとリスク当たりのリターンを、総リターンの比較より先に出す（CLAUDE.md の「リターン」の定義）
    for w in res["weights"]:
        s = _strategy(res, w, c0)
        if not s:
            continue
        mm = s["metrics"]
        dd = mm["max_drawdown_usd"]
        wp = mm.get("worst_period") or {}
        cmp_ = compare_to_benchmarks(res, w)
        lines.append(
            f"{WEIGHT_JP[w]}: 年率ボラ {_pct(mm['vol_ann'], signed=False)}、途中の最大下落 {_pct(dd['mdd'])}"
            f"（{dd['peak']}→{dd['trough']}）、{mm.get('n_periods', '—')} 期中 {mm.get('losing_periods', '—')} 期が"
            f"マイナス、最悪の期 {_pct(wp.get('return'))}（{wp.get('report_date', '—')} 期）。"
            f"リスク当たりのリターン（年率÷ボラ）は {_ratio(mm.get('ret_per_vol'))}"
            "（" + "、".join(f"{b} {_ratio(_bench(res, b, c0)['metrics'].get('ret_per_vol'))}"
                            for b in res["bench"] if _bench(res, b, c0)) + "）、"
            f"年率÷最大下落は {_ratio(mm.get('calmar'))}。"
            f"リスク当たりで上回ったベンチマーク: {', '.join(cmp_['beats_risk_adj']) or 'なし'}。"
        )
    for w in res["weights"]:
        s = _strategy(res, w, c0)
        if not s:
            continue
        cmp_ = compare_to_benchmarks(res, w)
        costs_txt = f"{c0:g}bp・{hi:g}bp の両方" if hi != c0 else f"{c0:g}bp"
        txt = (f"{WEIGHT_JP[w]}の総リターンだけで比べると（{costs_txt}で {ROUGHLY_EQUAL * 100:.0f}pt 以上の差）: "
               f"上回った {', '.join(cmp_['beats']) or 'なし'} / "
               f"ほぼ同じ（差が {ROUGHLY_EQUAL * 100:.0f}pt 未満かコスト次第で逆転） "
               f"{', '.join(cmp_['roughly_equal']) or 'なし'} / 下回った {', '.join(cmp_['loses']) or 'なし'}。")
        if fb and s.get("periods_beating_first_bench") is not None:
            bp = s["best_period"]
            txt += (f"{fb} に勝った期は {s['periods_beating_first_bench']}/{len(s['period_returns'])}。"
                    f"最も良かった 1 期（{bp['report_date']} 期 {_pct(bp['return'])}）を除くと "
                    f"{_pct(s['total_without_best_period'])}（{fb} は同じ期を除いて "
                    f"{_pct(s.get('first_bench_without_best_period'))}）。")
        lines.append(txt)
    if hi != c0:
        cs = [f"{WEIGHT_JP[w]} {_pct(_strategy(res, w, hi)['metrics']['total_return_usd'])}"
              for w in res["weights"] if _strategy(res, w, hi)]
        bs = [f"{b} {_pct(_bench(res, b, hi)['metrics']['total_return_usd'])}"
              for b in res["bench"] if _bench(res, b, hi)]
        lines.append(f"片道 {hi:g}bp のコストを入れると: " + "、".join(cs) + "（" + "、".join(bs) + "）。")
    if sensitivity:
        w0 = res["weights"][0]
        lines.append(
            f"エントリーの仮定別（{WEIGHT_JP[w0]}、{c0:g}bp）: "
            + " / ".join(f"{MODE_JP[r['mode']]} {_pct(r.get(w0))}" for r in sensitivity) + "。"
        )
    ac = acceptance_counts(res["periods"])
    if ac["known"]:
        lines.append(
            f"EDGAR の受理時刻（米東部）が分かる {ac['known']} 回のうち、{ac['after_close']} 回は 16:00 以降（引け後）、"
            f"{ac['pre_open']} 回は取引開始（9:30）前、{ac['intraday']} 回は場中。引け後の回は提出日の終値では買えず、"
            "「提出日の終値」は実行できない楽観値になる。"
        )
    pre = [r["pre_disclosure"]["value"] for r in res["missed"] if r["pre_disclosure"]["value"] is not None]
    if pre:
        lines.append(
            f"報告日から（フォロワーが買える）エントリーまでの、写せる保有銘柄の値動き（時価加重）: "
            f"{len(pre)} 回中 {sum(1 for x in pre if x > 0)} 回がプラス、中央値 {_pct(float(np.median(pre)))}、"
            f"最大 {_pct(max(pre))}。この分は報告日に持っていた本人だけが取れた。"
        )
    pops = [r for r in res["new_names"] if r["pop"] is not None and r.get("copyable", True)]
    if pops:
        top = sorted(pops, key=lambda r: -r["pop"])[:3]
        lines.append(
            "新規に開示された（写せる）銘柄の、開示前の最後の終値→エントリーの値動き（開示ポップ）の大きいもの: "
            + "、".join(f"{r['symbol']} {_pct(r['pop'])}（{r['report_date']} 期）" for r in top)
            + "。フォロワーはこの上がった後の値段で買うことになる。"
        )
    return lines


def _years(res: dict) -> float:
    return (pd.Timestamp(res["end"]) - pd.Timestamp(res["start"])).days / 365.25


def limitations(res: dict) -> list[tuple[str | None, str]]:
    """この計算の限界（見出し, 本文）。Markdown のレポートと Web 用の JSON で同じ文を使う。"""
    return [
        ("期間が短い。", (f"13F {len(res['periods'])} 回分・約 {_years(res):.1f} 年で、"
                        "1 つの相場（AI・半導体の上昇局面）しか含まない。数回の組み替えの結果は運の影響が大きく、"
                        "将来のリターンの見積もりにはならない")),
        ("戦略的な出資を含む。", ("事業会社の 13F は、提携・資金支援のための出資（相対取引・"
                              "発表前の条件での取得）が中心になりうる。13F には取得単価が出ないので、本人の損益とは別物で、"
                              "フォロワーは開示後の市場価格で買うしかない")),
        ("生存者バイアス。", ("「写せ」と話題になるのは、うまくいった提出者・期間だけ。"
                          "同じことをして失敗した提出者は話題にならない")),
        ("コスト・税・為替。", ("片道コストは手数料・為替手数料・売買の価格への影響をまとめた目安。"
                            "課税口座なら組み替えのたびに利益に約 20% の税、NISA でも組み替えのたびに成長投資枠を使う"
                            "（売っても枠はその年には戻らない）。円建ては為替の動きを含む")),
        ("リスク指標は仮定の計算。", ("最大下落・ボラは「この方法で持っていたら」の計算で、"
                                "実際に持っていた人の実績ではない。最大下落の途中で売ってしまうと、結果はこれより悪くなる")),
        (None, "13F は米国上場の株式ロングだけ。非上場の出資・現金・空売りは写らない。四半期の途中の売買も見えない"),
        (None, "店頭（OTC）かどうかは現在の上場市場で判定している。当時と市場が違う銘柄では判定がずれうる"),
    ]


def render_markdown(res: dict, meta: dict, sensitivity: list[dict],
                    variants: list[dict] | None = None) -> str:
    name = meta["name"]
    c0 = min(res["cost_bps"])
    L: list[str] = []
    L.append(f"# 13F を開示後に写した場合の検証: {name}（CIK {meta['cik']}）")
    L.append("")
    L.append(f"> 生成: {meta['generated_at']} / 13F: SEC EDGAR / 価格: Yahoo Finance の配当込み調整後終値")
    L.append("> **仮定の計算で、実績ではない。売買の推奨でもない。** 個別株を買う話になったら、"
             "独立性の確認（DESC・事前承認）が先。")
    L.append("")

    # ---- 結論
    L.append("## 結論（数字だけ）")
    L.append("")
    for line in _headline(res, name, sensitivity):
        L.append(f"- {line}")
    L.append("")

    # ---- 警告
    L.append("## 警告・データの問題")
    L.append("")
    unpriced = [(p["report_date"], u) for p in res["periods"] for u in p["unpriced"]]
    if unpriced:
        tot = len(unpriced)
        L.append(f"- ⚠ **評価不能・写せない銘柄 {tot} 件**（期ごと）。0 円扱いにも削除にもせず、"
                 "その期の買える銘柄だけで按分し直した。下の「評価不能の銘柄」に 13F 上の比率つきで一覧")
    else:
        L.append("- 評価不能の銘柄はなかった（全期・全銘柄にエントリー日の価格があった）")
    gaps = [(p["report_date"], g) for p in res["periods"] for g in p["trading_gaps"]]
    for rd, g in gaps:
        if g["kind"] == "stopped":
            L.append(f"- ⚠ {g['symbol']}（{rd} 期）: {g['last_date']} を最後に価格が途絶"
                     f"（期末まで {g['missing_days']} 営業日）。最後の終値で据え置いた"
                     "（その値で現金化したのと同じ扱い）。次の組み替えでその値で売ったものとして計算")
        else:
            L.append(f"- ⚠ {g['symbol']}（{rd} 期）: {g['from']}〜{g['to']} に {g['missing_days']} 営業日"
                     "価格が無い。直前の終値で据え置いて計算")
    for b in res["bench_unavailable"]:
        L.append(f"- ⚠ ベンチマーク {b['symbol']} は比較できない: {b['reason']}")
    for n in meta.get("notes", []) + res["notes"]:
        L.append(f"- {n}")
    if meta.get("price_warnings"):
        L.append("- 価格データの警告（src/marketdata.py の検査。除外していない急変は本物の値動きの可能性が高い）:")
        for w in meta["price_warnings"]:
            L.append(f"  - {w}")
    L.append("")

    # ---- 戦略とベンチマーク
    L.append("## 写した場合とベンチマーク")
    L.append("")
    fx_txt = (f"円建ては 1 ドル {res['fx_start']:.2f} 円 → {res['fx_end']:.2f} 円で換算。"
              if res["fx_start"] and res["fx_end"] else "為替が取れず、円建ては出していない。")
    L.append(f"期間はすべて同じ {res['start']}〜{res['end']}。資金を 1 として、"
             f"ベンチマークは初日に買って持つだけ（片道コストは初日の 1 回だけ）。{fx_txt}")
    L.append("")
    for c in res["cost_bps"]:
        L.append(f"### 片道コスト {c:g}bp")
        L.append("")
        L.append("| | 総リターン（ドル） | 総リターン（円） | 年率（ドル） | 年率（円） | 年率ボラ | 最大下落 | "
                 "ピーク → 底 | 最悪の期 | マイナスの期 | 年率÷ボラ | 年率÷最大下落 |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|")
        rows = [(f"写す: {WEIGHT_DESC[s['weights']]}", s) for s in res["strategies"] if s["cost_bps"] == c]
        rows += [(f"{b['symbol']}（買って持つ）", b) for b in res["benchmarks"] if b["cost_bps"] == c]
        for label, s in rows:
            m = s["metrics"]
            dd = m["max_drawdown_usd"]
            wp = m["worst_period"] or {}
            L.append(
                f"| {label} | {_pct(m['total_return_usd'])} | {_pct(m['total_return_jpy'])} | "
                f"{_pct(m['cagr_usd'])} | {_pct(m['cagr_jpy'])} | {_pct(m['vol_ann'], signed=False)} | "
                f"{_pct(dd['mdd'])} | {dd['peak'] or '—'} → {dd['trough'] or '—'} | "
                f"{_pct(wp.get('return'))}（{wp.get('report_date', '—')} 期） | "
                f"{m.get('losing_periods', '—')}/{m.get('n_periods', '—')} | "
                f"{_ratio(m.get('ret_per_vol'))} | {_ratio(m.get('calmar'))} |"
            )
        L.append("")
    L.append("年率÷ボラ・年率÷最大下落は、リスク 1 単位あたりの年率リターン（大きいほど効率が良い）。"
             "総リターンが高くても、この値が低ければ、同じリスクで指数を多めに持つ方が効率が良かったことになる。")
    L.append("")
    for w in res["weights"]:
        s = _strategy(res, w, c0)
        if s and s["turnover"]:
            L.append(f"- {WEIGHT_JP[w]}の組み替えで売買した金額（資産に対する比率、初回の購入 100% を含む）: "
                     + " / ".join(_w(t) for t in s["turnover"]))
    for w in res["weights"]:
        s = _strategy(res, w, c0)
        if s and s.get("buys_by_year"):
            yb = s["buys_by_year"]
            top = max(yb.values())
            unit = "円換算" if s.get("buys_in_jpy") else "ドル建て"
            L.append(f"- NISA 成長投資枠の消費（{WEIGHT_JP[w]}。年ごとの買付額 ÷ 初期資金、{unit}、初回の購入を含む）: "
                     + " / ".join(f"{y} 年 {v:.2f} 倍" for y, v in sorted(yb.items()))
                     + f"。枠は買付額で減り売っても年内は戻らないので、年 {NISA_GROWTH_MANYEN} 万円の枠の中で"
                     f"全額を写し続けられる初期資金は約 {NISA_GROWTH_MANYEN / top:.0f} 万円まで（買付の最も多い年で決まる）")
    L.append("")

    # ---- 感度
    if sensitivity:
        L.append("## エントリーの仮定を変えると")
        L.append("")
        sb = [b for b in res["bench"] if any(b in (r.get("benches") or {}) for r in sensitivity)]
        L.append(f"片道コスト {c0:g}bp。期間の始まりが変わるので、ベンチマークも同じ期間で出す。")
        L.append("")
        L.append("| エントリー | 期間 | " + " | ".join(WEIGHT_JP[w] for w in res["weights"]) + " | "
                 + " | ".join(sb) + " |")
        L.append("|---|---|" + "---:|" * (len(res["weights"]) + len(sb)))
        for r in sensitivity:
            L.append(f"| {MODE_JP[r['mode']]} | {r['start']}〜{r['end']} | "
                     + " | ".join(_pct(r.get(w)) for w in res["weights"]) + " | "
                     + " | ".join(_pct((r.get("benches") or {}).get(b)) for b in sb) + " |")
        L.append("")
        L.append("「報告日の終値」は、四半期末に保有を知っていたとした場合で、13F の開示前なので"
                 "誰にも実行できない。既定（翌営業日）との差が、開示の遅れで取れなかった分の目安。")
        L.append("")

    if variants:
        L.append("## 組み入れ比率・店頭銘柄の扱いを変えると")
        L.append("")
        L.append(f"エントリーは{MODE_JP[res['mode']]}、片道コスト {c0:g}bp、期間は上と同じ。"
                 "どれも「写した」と言える範囲の違いで、結果がこれだけ動く。")
        L.append("")
        L.append("| 組み入れ | 店頭（OTC）銘柄 | 総リターン（ドル） | 年率 | 年率ボラ | 最大下落 | 年率÷ボラ |")
        L.append("|---|---|---:|---:|---:|---:|---:|")
        for v in variants:
            m = v["metrics"]
            L.append(f"| {WEIGHT_DESC[v['weights']]} | {'買えたことにする' if v['include_otc'] else '除外'} | "
                     f"{_pct(m['total_return_usd'])} | {_pct(m['cagr_usd'])} | "
                     f"{_pct(m['vol_ann'], signed=False)} | {_pct(m['max_drawdown_usd']['mdd'])} | "
                     f"{_ratio(m.get('ret_per_vol'))} |")
        L.append("")

    # ---- 期ごと
    L.append(f"## 期ごとの結果（片道コスト {c0:g}bp）")
    L.append("")
    bh = [b for b in res["bench"] if any(b in p["bench_returns"] for p in res["periods"])]
    head = "| 報告日 | 提出日 | 受理（米東部） | エントリー | 出口 | 銘柄 | 価格あり | 評価不能（13F 比率） | " + \
        " | ".join(WEIGHT_JP[w] for w in res["weights"]) + " | " + " | ".join(bh) + " |"
    L.append(head)
    L.append("|" + "---|" * 8 + "---:|" * (len(res["weights"]) + len(bh)))
    for p in res["periods"]:
        un = ", ".join(f"{u['symbol'] or u['issuer']} {_w(u['reported_weight'])}" for u in p["unpriced"]) or "—"
        L.append(
            f"| {p['report_date']} | {p['filed_date']} | {_et(p['accepted_et'])}"
            f"{'※' if p.get('accepted_source') == 'submissions_json' else ''} | {p['entry']} | {p['exit']} | "
            f"{p['n_positions']} | {p['n_priced']} | {un} | "
            + " | ".join(_pct(p["returns"][w]) for w in res["weights"]) + " | "
            + " | ".join(_pct(p["bench_returns"].get(b)) for b in bh) + " |"
        )
    L.append("")
    L.append("各期の組み入れ（時価加重の比率）:")
    L.append("")
    for p in res["periods"]:
        L.append(f"- {p['report_date']} 期: " + ", ".join(
            f"{r['symbol']} {_w(r['weight_value'])}" for r in p["priced"]))
    L.append("")

    # ---- 取れなかった値動き
    fb = res["first_bench"] or "—"
    L.append("## フォロワーが取れなかった値動き")
    L.append("")
    L.append("13F は四半期末の保有を最大 45 日後に開示する。その間の値動きと、開示直後の急騰は、"
             "開示を見てから買う人には取れない。")
    L.append("")
    L.append("報告日終値→エントリーは、フォロワーが写せる銘柄（店頭・価格の無い銘柄を除く。戦略と同じ）で計算し、"
             "13F の全銘柄で計算した値も並べる。開示ポップの起点は開示前の最後の終値"
             "（受理が 16:00 ET 以降なら受理日の終値、それより前なら前営業日の終値）。")
    L.append("")
    L.append(f"| 報告日 | 提出日 | 報告日終値→エントリー（時価加重） | 同（等金額） | 同（13F 全銘柄・時価加重） | "
             f"{fb} 同期間 | 新規開示 | 開示前の終値 | 開示ポップ（新規・写せる銘柄・時価加重） | {fb} 同期間 |")
    L.append("|---|---|---:|---:|---:|---:|---|---|---:|---:|")
    for r in res["missed"]:
        new = ", ".join(r["new_names"]) or "—"
        if r.get("new_label"):
            new = f"{new}（{r['new_label']}）"
        L.append(
            f"| {r['report_date']} | {r['filed_date']} | {_pct(r['pre_disclosure']['value'])} | "
            f"{_pct(r['pre_disclosure']['equal'])} | {_pct(r['pre_disclosure_all_holdings']['value'])} | "
            f"{_pct(r['pre_disclosure_bench'])} | {new} | {r.get('pre_filing_close') or '—'} | "
            f"{_pct(r['pop']['value'])} | {_pct(r['pop_bench'])} |"
        )
    L.append("")
    if res["new_names"]:
        L.append("新規に開示された銘柄ごと（開示ポップは開示前の最後の終値→エントリーの終値。"
                 "その後はエントリー→その期の出口。写せない銘柄は参考として出すが、結論の上位には入れない）:")
        L.append("")
        L.append("| 報告日 | 銘柄 | 13F 上の比率 | 開示前の終値 | 開示ポップ | エントリー後（その期） | 出口 |")
        L.append("|---|---|---:|---|---:|---:|---|")
        for r in res["new_names"]:
            nm = f"{r['symbol'] or '—'}（{r['issuer']}）"
            note = f" ※{r['excluded_reason']}" if r.get("excluded_reason") else ""
            if r.get("not_copyable_reason") and not r.get("excluded_reason"):
                note += " ※写せない銘柄（店頭など）"
            L.append(f"| {r['report_date']} | {nm}{note} | {_w(r['reported_weight'])} | "
                     f"{r.get('pre_filing_close') or '—'} | {_pct(r['pop'])} | "
                     f"{_pct(r['after_entry_to_exit'])} | {r['exit'] or '—'} |")
        L.append("")

    # ---- 銘柄ごと
    L.append("## 銘柄ごと: 持っていた間の最大下落と、全体への寄与")
    L.append("")
    L.append(f"寄与は、資金 1 に対してその銘柄で増減した額（片道コスト {c0:g}bp。"
             "全銘柄の寄与 − コスト = 総リターン）。")
    L.append("")
    L.append("| 銘柄 | 保有した期 | 持っていた期間 | その間の値動き | 最大下落 | ピーク → 底 | "
             + " | ".join(f"寄与（{WEIGHT_JP[w]}）" for w in res["weights"]) + " |")
    L.append("|---|---:|---|---:|---:|---|" + "---:|" * len(res["weights"]))
    for h in res["holdings"]:
        dd = h["max_drawdown_while_held"]
        L.append(
            f"| {h['symbol']}（{h['issuer']}） | {h['periods_held']} | {h['held_from']}〜{h['held_to']} | "
            f"{_pct(h['return_while_held'])} | {_pct(dd['mdd'])} | {dd['peak'] or '—'} → {dd['trough'] or '—'} | "
            + " | ".join(_pct(h["contribution"].get(w)) for w in res["weights"]) + " |"
        )
    L.append("")

    # ---- 評価不能
    L.append("## 評価不能の銘柄")
    L.append("")
    if unpriced:
        L.append("| 報告日 | 銘柄 | CUSIP | 13F 上の比率 | 理由 |")
        L.append("|---|---|---|---:|---|")
        for rd, u in unpriced:
            L.append(f"| {rd} | {u['symbol'] or '—'}（{u['issuer']}） | {', '.join(u['cusips'])} | "
                     f"{_w(u['reported_weight'])} | {u['reason']} |")
    else:
        L.append("なし。")
    L.append("")

    # ---- オプション
    L.append("## プット・コール（写していない）")
    L.append("")
    opts = [(p["report_date"], o) for p in res["periods"] for o in p["options"]]
    if opts:
        L.append("| 報告日 | 発行体 | 種別 | 想定元本（ドル） |")
        L.append("|---|---|---|---:|")
        for rd, o in opts:
            L.append(f"| {rd} | {o['issuer']} | {o['put_call']} | {o['value']:,.0f} |")
    else:
        L.append("この提出者の 13F にプット・コールの行は無かった。")
    L.append("")

    # ---- 限界
    L.append("## この計算の限界")
    L.append("")
    for head, body in limitations(res):
        L.append(f"- **{head}** {body}" if head else f"- {body}")
    L.append("")

    # ---- 方法
    L.append("## 計算の方法")
    L.append("")
    L.append(f"- エントリー: {MODE_JP[res['mode']]}。カレンダーは {meta.get('calendar_ref', '—')} の取引日。"
             "出口は次の提出のエントリー日、最後の期は最新の確定した終値")
    L.append("- 組み入れ: その提出の現物株ロング。時価加重 = 13F の報告日の金額の比率、"
             "株数×エントリー日株価 = 13F の株数をエントリー日の株価で評価した比率、"
             "等金額 = 1/銘柄数（13F の比率は無視するので、ごく小さい保有も同じ重みになる）。"
             "期中は比率を戻さない（持ちっぱなし）")
    L.append("- 受理時刻: EDGAR の各提出のヘッダー（ACCEPTANCE-DATETIME、米東部）。"
             "※印は submissions JSON の値で代えたもの（4〜5 時間遅く出ることがある）")
    L.append("- リスク当たりのリターン: 年率÷年率ボラ（日次の変化率の標準偏差 × √252）と、年率÷最大下落")
    L.append(f"- 売買できる価格: その日から {MAX_STALE} 営業日以内に終値があること。無ければ評価不能として除外し一覧")
    L.append("- コスト: 片道コスト × 組み替えで売買した金額。最初の購入は全額。最後の売却は課さない")
    L.append("- 開示ポップ: 前の 13F に無く今回の 13F に出た銘柄の、開示前の最後の終値→エントリーの終値。"
             "開示前の最後の終値は、受理が 16:00 ET 以降なら受理日の終値、16:00 より前（寄り付き前・場中）なら"
             "前営業日の終値。受理時刻が不明なら提出日の前営業日の終値")
    L.append("- 出来高 0 の日の終値は売買が成立していないので、保有銘柄の価格としては使わない"
             "（直前の売買のあった日の終値を使う）")
    L.append(f"- 再実行: `python tools/backtest_13f_copy.py --cik {meta['cik']}`（--no-fetch でキャッシュのみ）")
    L.append("")
    return "\n".join(L)


# ------------------------------------------------------------ Web 用（公開してよい要約）
#
# web/ledger.html の「13F を写す」タブが読む。13F（SEC が公開）と市場価格から作った数字だけで、
# 個人の金額・ファイルの場所・SEC への連絡先は入れない。数字はページに書かず、ここから差し込む
# （四半期ごとに作り直してもページの文章が古くならないように）。

WEB_MAX_BYTES = 80_000
BENCH_JP = {"SPY": "S&P500（米国の大型株）", "QQQ": "NASDAQ100（米国のハイテク中心）",
            "SMH": "米国の半導体株", "NVDA": "NVIDIA 株そのもの", "ACWI": "全世界株"}


def _r(x, nd: int = 6):
    """丸めた float。None・NaN・無限大は None（厳密な JSON にする）。

    比率は小数 6 桁（0.0001% 単位）で持つ。ページは % の小数 1 桁で表示するので、ここで 4 桁に丸めると
    2 回丸めになり、レポートと 0.1pt ずれることがある（-7.554% → -7.55 → -7.5%）。
    6 桁でも境目の値はずれる（0.0444999… → 0.0445 → 4.5%、レポートは 4.4%）。表示より細かく持つ値
    （nd が 4 以上）は、ページの表示（% の小数 0・1 桁、比率の小数 2 桁）が丸めで変わるときだけ丸めずに出す。
    年数・為替（nd が 2）は表示に合わせた丸めなので、そのまま丸める。
    """
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    r = round(f, nd) + 0.0
    if nd < 4:
        return r

    def shown(v: float) -> tuple[float, float, float]:     # 0.0 と -0.0 は同じ表示（ページは符号を付けない）
        return float(f"{v * 100:.0f}"), float(f"{v * 100:.1f}"), float(f"{v:.2f}")

    return r if shown(r) == shown(f) else f + 0.0


def _public(text: str) -> str:
    """公開する文から、手元のパスと SEC への連絡先（User-Agent）を消す。"""
    s = str(text).replace(str(ROOT) + "/", "").replace(str(ROOT), ".")
    ua = getattr(fetch_13f, "USER_AGENT", "")
    if ua and "example.com" not in ua:
        s = s.replace(ua, "（連絡先）")
    return s


def _web_dd(dd: dict | None) -> dict | None:
    if not dd:
        return None
    return {"v": _r(dd.get("mdd")), "peak": dd.get("peak"), "trough": dd.get("trough"),
            "recovery": dd.get("recovery")}


def _web_metrics(m: dict) -> dict:
    wp = m.get("worst_period") or {}
    return {
        "total_usd": _r(m.get("total_return_usd")), "total_jpy": _r(m.get("total_return_jpy")),
        "cagr_usd": _r(m.get("cagr_usd")), "cagr_jpy": _r(m.get("cagr_jpy")),
        "vol": _r(m.get("vol_ann")),
        "mdd": _web_dd(m.get("max_drawdown_usd")), "mdd_jpy": _web_dd(m.get("max_drawdown_jpy")),
        "worst_period": {"ret": _r(wp.get("return")), "report_date": wp.get("report_date")} if wp else None,
        "losing_periods": m.get("losing_periods"), "n_periods": m.get("n_periods"),
        "ret_per_vol": _r(m.get("ret_per_vol"), 4), "calmar": _r(m.get("calmar"), 4),
    }


def _web_summary(res: dict) -> list[dict]:
    """コストごとに、写した戦略とベンチマーク（買って持つだけ）の指標を並べる。"""
    out = []
    for c in res["cost_bps"]:
        rows = []
        for s in res["strategies"]:
            if s["cost_bps"] != c:
                continue
            w = s["weights"]
            bp = s.get("best_period") or {}
            rows.append({
                "kind": "copy", "key": w, "label": f"写す（{WEIGHT_JP[w]}）", "desc": WEIGHT_DESC[w],
                **_web_metrics(s["metrics"]),
                "best_period": {"report_date": bp.get("report_date"), "ret": _r(bp.get("return"))},
                "total_without_best": _r(s.get("total_without_best_period")),
                "first_bench_without_best": _r(s.get("first_bench_without_best_period")),
                "periods_beating_first_bench": s.get("periods_beating_first_bench"),
                "cost_total": _r(s.get("cost_total")),
                "turnover": [_r(t, 4) for t in s.get("turnover", [])],
            })
        for b in res["benchmarks"]:
            if b["cost_bps"] != c:
                continue
            rows.append({"kind": "bench", "key": b["symbol"], "label": b["symbol"],
                         "desc": BENCH_JP.get(b["symbol"], b["symbol"]), **_web_metrics(b["metrics"])})
        out.append({"cost_bps": c, "rows": rows})
    return out


def _web_sensitivity(res: dict, sensitivity: list[dict]) -> list[dict]:
    """エントリーの仮定別。実行できるかどうかと、その理由を付ける。"""
    ac = acceptance_counts(res["periods"])
    late = ac["after_close"] + ac["unknown"]
    same_ok = late == 0
    why = {
        "next": (True, "提出日の翌営業日の引けで買う。開示を見てから動く人が実際に取れる値"),
        "accepted": (True, ("EDGAR の受理時刻（米東部）が 16:00 より前ならその日の引け、後なら翌営業日の引け。"
                            "受理を見てすぐ動ければ実行できる")),
        "same": (same_ok, "全回とも受理が引け前で、提出日の終値に間に合う" if same_ok else
                 (f"{len(res['periods'])} 回中 {late} 回は受理が引け後（16:00 以降）か時刻不明で、"
                  "その日の終値ではもう買えない。実行できない楽観値")),
        "report": (False, ("四半期末の時点では 13F はまだ開示されていない（開示は最大 45 日後）。"
                           "誰にも実行できない参考値")),
    }
    out = []
    for r in sensitivity:
        ok, txt = why.get(r["mode"], (None, ""))
        out.append({
            "mode": r["mode"], "label": MODE_JP.get(r["mode"], r["mode"]),
            "start": r.get("start"), "end": r.get("end"),
            "copy": {w: _r(r.get(w)) for w in res["weights"]},
            "bench": {k: _r(v) for k, v in (r.get("benches") or {}).items()},
            "executable": ok, "why": txt,
        })
    return out


def _web_periods(res: dict) -> list[dict]:
    out = []
    for p in res["periods"]:
        out.append({
            "report_date": p["report_date"], "filed_date": p["filed_date"],
            "accepted_et": p.get("accepted_et"), "accepted_source": p.get("accepted_source"),
            "entry": p["entry"], "exit": p["exit"],
            "n_positions": p["n_positions"], "n_priced": p["n_priced"],
            "positions": [{"symbol": r["symbol"], "issuer": r.get("issuer"),
                           "w13f": _r(r.get("reported_weight")), "w_value": _r(r.get("weight_value")),
                           "w_equal": _r(r.get("weight_equal"))} for r in p["priced"]],
            "unpriced": [{"symbol": u.get("symbol"), "issuer": u.get("issuer"),
                          "w13f": _r(u.get("reported_weight")), "reason": u.get("reason")}
                         for u in p["unpriced"]],
            "unpriced_weight": _r(p.get("unpriced_weight")),
            "returns": {w: _r(v) for w, v in p["returns"].items()},
            "turnover": {w: _r(v, 4) for w, v in p["turnover"].items()},
            "bench": {b: _r(v) for b, v in p["bench_returns"].items()},
            "gaps": [{k: (_r(v) if isinstance(v, float) else v) for k, v in g.items()}
                     for g in p.get("trading_gaps", [])],
            "n_options": len(p.get("options") or []),
        })
    return out


def _web_missed(res: dict) -> dict:
    def wr(x: dict) -> dict:
        return {"value": _r(x.get("value")), "equal": _r(x.get("equal")), "n": x.get("n", 0)}

    rows = [{
        "report_date": r["report_date"], "filed_date": r["filed_date"],
        "report_close": r.get("report_close"), "pre_filing_close": r.get("pre_filing_close"),
        "pre_filing_rule": r.get("pre_filing_rule"), "entry": r.get("entry"),
        "pre": wr(r["pre_disclosure"]), "pre_all": _r(r["pre_disclosure_all_holdings"].get("value")),
        "pre_bench": _r(r.get("pre_disclosure_bench")),
        "new_names": r.get("new_names", []), "new_label": r.get("new_label"),
        "pop": wr(r["pop"]), "pop_bench": _r(r.get("pop_bench")),
    } for r in res["missed"]]
    names = [{
        "report_date": r["report_date"], "symbol": r.get("symbol"), "issuer": r.get("issuer"),
        "w13f": _r(r.get("reported_weight")), "pre_filing_close": r.get("pre_filing_close"),
        "pop": _r(r.get("pop")), "after": _r(r.get("after_entry_to_exit")), "exit": r.get("exit"),
        "copyable": bool(r.get("copyable", True)),
        "reason": r.get("excluded_reason") or r.get("not_copyable_reason"),
    } for r in res["new_names"]]
    return {"bench": res.get("first_bench"), "rows": rows, "new_names": names}


def _web_holdings(res: dict) -> list[dict]:
    return [{
        "symbol": h["symbol"], "issuer": h.get("issuer"), "periods_held": h["periods_held"],
        "held_from": h["held_from"], "held_to": h["held_to"], "ret": _r(h["return_while_held"]),
        "mdd": _web_dd(h["max_drawdown_while_held"]),
        "contrib": {w: _r(v) for w, v in h["contribution"].items()},
    } for h in res["holdings"]]


def _web_nisa(res: dict) -> dict:
    c0 = min(res["cost_bps"])
    out = {"quota_manyen": NISA_GROWTH_MANYEN, "by_weights": {}}
    for w in res["weights"]:
        s = _strategy(res, w, c0)
        yb = (s or {}).get("buys_by_year") or {}
        if not yb:
            continue
        y_top = max(yb, key=yb.get)
        top = yb[y_top]
        out["by_weights"][w] = {
            "buys_by_year": {y: _r(v, 4) for y, v in sorted(yb.items())}, "in_jpy": bool(s.get("buys_in_jpy")),
            "max_year": y_top, "max_multiple": _r(top, 4),
            "max_initial_manyen": round(NISA_GROWTH_MANYEN / top) if top > 0 else None,
        }
    return out


def _web_curve(res: dict) -> dict:
    """最小コストの資産推移（開始 = 1.0）を週次に間引く。最大下落のピークと底の日は残す。"""
    c0 = min(res["cost_bps"])
    dates = res["nav"]["dates"]
    allser = res["nav"]["series"]
    keys = [(w, f"{w}_{c0:g}bp") for w in res["weights"]] + [(b, f"{b}_{c0:g}bp") for b in res["bench"]]
    keys = [(k, s) for k, s in keys if s in allser and len(allser[s]) == len(dates)]
    if not dates:
        return {"cost_bps": c0, "dates": [], "series": {}, "entries": []}
    wk = pd.DatetimeIndex(pd.to_datetime(dates)).to_period("W-FRI")
    last_of_week = np.r_[np.asarray(wk[1:] != wk[:-1]), True]
    keep = set(np.flatnonzero(last_of_week).tolist()) | {0, len(dates) - 1}
    pos = {d: i for i, d in enumerate(dates)}
    for row in res["strategies"] + res["benchmarks"]:
        if row["cost_bps"] != c0:
            continue
        dd = row["metrics"]["max_drawdown_usd"]
        for k in ("peak", "trough"):
            if dd.get(k) in pos:
                keep.add(pos[dd[k]])
    sel = sorted(keep)
    return {
        "cost_bps": c0, "freq": "週次（各週の最後の終値。最大下落のピークと底の日を含む）",
        "dates": [dates[i] for i in sel],
        "series": {k: [_r(allser[s][i], 5) for i in sel] for k, s in keys},
        "entries": [p["entry"] for p in res["periods"]],
    }


def data_warnings(res: dict) -> list[str]:
    """結果に効くデータの問題（評価不能・写せない銘柄、取引の途絶、比べられないベンチマーク）。"""
    out = []
    for p in res["periods"]:
        for u in p["unpriced"]:
            out.append(f"{p['report_date']} 期: {u.get('symbol') or '—'}（{u.get('issuer')}、13F 上の比率 "
                       f"{_w(u.get('reported_weight'))}）は写せない・評価できない銘柄として除外し、その期の買える銘柄"
                       f"だけで按分し直した（0 円扱いにはしていない）。理由: {u.get('reason')}")
        for g in p.get("trading_gaps", []):
            if g["kind"] == "stopped":
                out.append(f"{g['symbol']}（{p['report_date']} 期）: {g['last_date']} を最後に価格が途絶"
                           f"（期末まで {g['missing_days']} 営業日）。最後の終値で据え置いた")
            else:
                out.append(f"{g['symbol']}（{p['report_date']} 期）: {g['from']}〜{g['to']} に "
                           f"{g['missing_days']} 営業日価格が無い。直前の終値で据え置いた")
    for b in res["bench_unavailable"]:
        out.append(f"ベンチマーク {b['symbol']} は比較できない: {b['reason']}")
    return out


def web_payload(res: dict, meta: dict, sensitivity: list[dict],
                variants: list[dict] | None = None) -> dict:
    """ページ用の要約（丸めた数字・週次の資産推移）。公開してよい情報だけを選んで入れる。"""
    c0 = min(res["cost_bps"])
    per = res["periods"]
    return _clean({
        "schema": 1,
        "meta": {
            "generated": meta.get("generated_at"),
            "sources": {"filings": "SEC EDGAR（13F-HR）",
                        "prices": "Yahoo Finance（配当込みの調整後終値）",
                        "fx": "Yahoo Finance（JPY=X、1 ドル = 何円）"},
            "tool": "tools/backtest_13f_copy.py",
            "cik": str(meta.get("cik")), "filer": meta.get("name"), "edgar_name": meta.get("edgar_name"),
            "n_filings": len(per), "report_first": per[0]["report_date"], "report_last": per[-1]["report_date"],
            "start": res["start"], "end": res["end"], "years": _r(_years(res), 2),
            "entry": {"mode": res["mode"], "label": MODE_JP[res["mode"]]},
            "calendar": meta.get("calendar_ref"),
            "weights": res["weights"], "weights_jp": {w: WEIGHT_JP[w] for w in WEIGHT_JP},
            "cost_bps": res["cost_bps"], "cost_base": c0,
            "bench": res["bench"], "bench_jp": {b: BENCH_JP.get(b, b) for b in res["bench"]},
            "first_bench": res.get("first_bench"), "include_otc": bool(res.get("include_otc")),
            "max_stale_days": MAX_STALE, "roughly_equal": ROUGHLY_EQUAL,
            "fx_start": _r(res.get("fx_start"), 2), "fx_end": _r(res.get("fx_end"), 2),
            "acceptance": acceptance_counts(per),
            "first_filing_is_initial": meta.get("first_filing_is_initial"),
            "caveats": [{"head": h, "body": b} for h, b in limitations(res)],
            "warnings": [_public(x) for x in data_warnings(res)],
            "notes": [_public(x) for x in list(meta.get("notes", [])) + list(res.get("notes", []))],
            "price_warnings": [_public(x) for x in meta.get("price_warnings", [])],
        },
        "summary": _web_summary(res),
        "compare": {w: compare_to_benchmarks(res, w) for w in res["weights"]},
        "sensitivity": _web_sensitivity(res, sensitivity),
        "variants": [{
            "weights": v["weights"], "label": WEIGHT_DESC[v["weights"]], "include_otc": bool(v["include_otc"]),
            "total_usd": _r(v["metrics"].get("total_return_usd")), "cagr_usd": _r(v["metrics"].get("cagr_usd")),
            "vol": _r(v["metrics"].get("vol_ann")), "mdd": _r(v["metrics"]["max_drawdown_usd"]["mdd"]),
            "ret_per_vol": _r(v["metrics"].get("ret_per_vol"), 4),
        } for v in (variants or [])],
        "periods": _web_periods(res),
        "missed": _web_missed(res),
        "holdings": _web_holdings(res),
        "nisa": _web_nisa(res),
        "curve": _web_curve(res),
    })


def web_json(payload: dict) -> str:
    """厳密な JSON（NaN・Infinity を出さない）で、空白を詰めて書く。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


# ------------------------------------------------------------ CLI

def _sensitivity_row(res: dict, mode: str, c0: float) -> dict:
    row = {"mode": mode, "start": res["start"], "end": res["end"]}
    for w in res["weights"]:
        s = _strategy(res, w, c0)
        row[w] = s["metrics"]["total_return_usd"] if s else None
    row["benches"] = {b["symbol"]: b["metrics"]["total_return_usd"]
                      for b in res["benchmarks"] if b["cost_bps"] == c0}
    return row


def otc_held(res: dict) -> bool:
    """店頭（OTC）として除外した、または店頭なのに組み入れた銘柄があるか。"""
    return any("店頭" in (u.get("reason") or "") for p in res["periods"] for u in p["unpriced"]) \
        or bool(res.get("include_otc"))


def otc_hindsight_notes(res: dict, volume: pd.DataFrame | None,
                        exchanges: dict[str, str | None] | None) -> list[str]:
    """店頭の判定は今の上場市場で行うので、エントリーの時点ではまだ上場していた銘柄を後知恵で落としていないか。

    価格データに、エントリー以前の出来高 0 の日（上場廃止・店頭移行のしるし）があるかで確かめる。
    """
    out = []
    first_entry: dict[str, str] = {}
    for p in res["periods"]:
        for u in p["unpriced"]:
            if u.get("symbol") and "店頭" in (u.get("reason") or ""):
                first_entry.setdefault(u["symbol"], p["entry"])
    for s, e in sorted(first_entry.items()):
        z = None
        if volume is not None and s in volume.columns:
            zs = volume.index[(volume[s] == 0).to_numpy()]
            z = zs.min() if len(zs) else None
        ex = (exchanges or {}).get(s)
        if z is not None and z <= pd.Timestamp(e):
            out.append(f"{s}（今の市場 {ex}）は {z.date()} に出来高 0 の日があり、最初のエントリー（{e}）の時点で"
                       "既に上場廃止・店頭に移っていたとみられる。写せない銘柄として除外した")
        else:
            out.append(f"⚠ {s}（今の市場 {ex}）は、最初のエントリー（{e}）より前に店頭へ移ったしるしが"
                       "価格データに無い。エントリー時点では上場していて、後から店頭になったのに除外した"
                       "（後知恵で負けを落とした）可能性がある。下の「組み入れ比率・店頭銘柄の扱いを変えると」"
                       "の、店頭銘柄を買えたことにした行と比べること（--include-otc で再計算できる）")
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="13F を開示後に写した場合のバックテスト（先読みなし）")
    ap.add_argument("--cik", default=DEFAULT_CIK, help="提出者の CIK（既定 1045810 = NVIDIA）")
    ap.add_argument("--name", help="レポートに出す名前（既定は EDGAR の登録名）")
    ap.add_argument("--weights", choices=["value", "shares", "equal", "both"], default="both",
                    help="value = 13F の金額比、shares = 13F の株数×エントリー日株価、equal = 等金額"
                         "（既定 both = value と equal。ほかの組み入れはレポートの感度の表に出る）")
    ap.add_argument("--cost-bps", type=float, nargs="+", default=DEFAULT_COSTS,
                    help="片道コスト（bp）。複数指定可（既定 0 50）")
    ap.add_argument("--bench", nargs="+", default=DEFAULT_BENCH, help="比べるティッカー")
    ap.add_argument("--entry", choices=["next", "same", "accepted"], default="next",
                    help="next = 提出日の翌営業日の終値（既定）、same = 提出日の終値（楽観）、"
                         "accepted = 受理時刻の直後の終値")
    ap.add_argument("--include-otc", action="store_true",
                    help="店頭（OTC）銘柄も買えたことにする（既定は除外して一覧）")
    ap.add_argument("--refresh", action="store_true", help="13F・ティッカー・価格のキャッシュを取り直す")
    ap.add_argument("--no-fetch", action="store_true", help="SEC に行かずキャッシュだけで計算する")
    ap.add_argument("--out", help="Markdown の出力先（既定 reports/13f_copy_<cik>.md）")
    ap.add_argument("--json", help="JSON の出力先（既定 data/13f_copy_<cik>.json）")
    ap.add_argument("--web", metavar="PATH",
                    help="ページ用の公開してよい要約 JSON も書く（例 web/copycheck.json。丸めた数字と週次の資産推移）")
    args = ap.parse_args(argv)

    cik = str(int(args.cik))
    cik10 = cik.zfill(10)
    out_md = Path(args.out) if args.out else ROOT / "reports" / f"13f_copy_{cik}.md"
    out_js = Path(args.json) if args.json else ROOT / "data" / f"13f_copy_{cik}.json"
    weight_modes = ["value", "equal"] if args.weights == "both" else [args.weights]
    costs = sorted(set(args.cost_bps))
    notes: list[str] = []

    db = fetch_13f.init_db()
    init_meta_tables(db)
    if not args.no_fetch:
        if "your-email@example.com" in fetch_13f.USER_AGENT:
            print("⚠ SEC_USER_AGENT が既定値のままです。連絡先を含む文字列を環境変数で渡してください。")
        ed = fetch_13f.Edgar()
        try:
            r = fetch_13f.process(ed, db, {"name": args.name or f"CIK {cik}", "cik": cik},
                                  args.refresh, quarters=ALL_QUARTERS)
            print(f"EDGAR: {r}")
            notes.extend(fetch_filing_meta(ed, db, cik10))
        except fetch_13f.SecBlocked as e:
            notes.append(f"SEC に拒否されたため、キャッシュの 13F で計算した: {e}")
        except Exception as e:  # noqa: BLE001 — 取れなければキャッシュで続け、そのことを書く
            notes.append(f"SEC から取得できなかったため、キャッシュの 13F で計算した: {type(e).__name__}: {e}")
        finally:
            ed.close()
    else:
        notes.append("--no-fetch: SEC には行かず、キャッシュの 13F で計算した")

    filings = load_filings(db, cik10)
    if not filings:
        raise SystemExit(f"CIK {cik} の 13F がキャッシュにありません。--no-fetch を外して実行してください。")
    cov = coverage(db, cik10)
    initial = first_is_initial(filings, cov)
    if cov:
        notes.append(f"EDGAR の提出一覧は {cov.get('recent_from')} 以降を確認（それより古いページ "
                     f"{cov.get('older_pages')} 件は見ていない）。13F は {len(filings)} 回分"
                     f"（{filings[0]['report_date']}〜{filings[-1]['report_date']}）")
    if any(not f.get("accepted_et") for f in filings):
        notes.append("受理時刻の無い提出がある（--no-fetch か取得失敗）。accepted モードでは翌営業日、"
                     "開示ポップの起点は提出日の前営業日の終値とした")

    cusips = sorted({p["cusip"] for f in filings for p in f["positions"]})
    cmap = figi.resolve(db, cusips, refresh=args.refresh)
    attach_symbols(filings, cmap)
    unresolved = sorted({p["cusip"] for f in filings for p in f["positions"] if p["symbol"] is None})
    if unresolved:
        notes.append(f"ティッカー未解決・株以外の CUSIP {len(unresolved)} 件: {', '.join(unresolved)}")

    syms = sorted({p["symbol"] for f in filings for p in f["positions"] if p["symbol"]})
    exchanges, ex_notes = listing_exchanges(db, syms, refresh=args.refresh, fetch=not args.no_fetch)
    notes.extend(ex_notes)
    otc = sorted(s for s, e in exchanges.items() if e in OTC_EXCHANGES)
    if otc:
        notes.append(("店頭（OTC）銘柄: " + ", ".join(f"{s}（{exchanges[s]}）" for s in otc))
                     + ("。--include-otc で買えたことにしている" if args.include_otc
                        else "。日本のネット証券では通常買えないので、写せない銘柄として除外した"))

    md = MarketData()
    first_rd = pd.Timestamp(filings[0]["report_date"])
    years = ((pd.Timestamp(date.today()) - first_rd).days + 30) / 365.25
    want = sorted(set(syms) | set(args.bench) | {FX_SYMBOL})
    if args.refresh:
        md._db.execute(  # 履歴のキャッシュを無効にする（MarketData に強制取得の引数が無いため）
            f"DELETE FROM history_meta WHERE symbol IN ({','.join('?' * len(want))})", want)
        md._db.commit()
    hist = md.get_history(want, years=years)
    price_warnings = list(dict.fromkeys(md.fetch_errors))
    fetched_at = {}
    for s in hist:
        row = md._db.execute("SELECT fetched_at FROM history_meta WHERE symbol=?", (s,)).fetchone()
        if row:
            fetched_at[s] = datetime.fromtimestamp(row[0], NY)
    md.close()

    fx_df = hist.pop(FX_SYMBOL, None)
    fx = fx_df["close"] if fx_df is not None and not fx_df.empty else None
    if fx is None:
        notes.append("為替（JPY=X）が取れず、円建ての数字は出していない")
    adj = price_frame(hist)
    volume = price_frame(hist, "volume")
    if adj.empty:
        raise SystemExit("価格を 1 件も取得できませんでした（ネットワーク制限・レート制限の可能性）。"
                         "\n" + "\n".join(price_warnings))
    cal, cal_ref = trading_calendar(adj, args.bench)
    # 場中に取ってキャッシュした足かどうかは、実行時刻ではなく取得時刻で判定する
    cal, unsettled = drop_unsettled_close(cal, fetched=last_bar_fetch_time(adj, cal, fetched_at))
    if unsettled:
        notes.append(unsettled)
    if fx is not None:
        last_fx = fx.dropna().index[fx.dropna().index <= cal[-1]].max()
        if last_fx < cal[-1]:
            notes.append(f"為替の最終日は {last_fx.date()}。それ以降は直前の値で換算")

    kw = dict(fx=fx, bench=args.bench, weight_modes=weight_modes, costs=costs,
              exchanges=exchanges, include_otc=args.include_otc, initial=initial, volume=volume)
    res = run_backtest(filings, adj, cal, mode=args.entry, **kw)
    c0 = min(costs)

    # 組み入れ比率・店頭銘柄の扱いの感度（同じエントリー、最小コスト）
    variants = []
    for w, inc in [("value", args.include_otc), ("shares", args.include_otc), ("equal", args.include_otc),
                   ("value", not args.include_otc), ("equal", not args.include_otc)]:
        if otc_held(res) is False and inc != args.include_otc:
            continue                       # 店頭銘柄が無ければ同じ結果になるので出さない
        r = res if (w in weight_modes and inc == args.include_otc) else run_backtest(
            filings, adj, cal, mode=args.entry, **(kw | {"weight_modes": [w], "costs": [c0],
                                                         "include_otc": inc}))
        st = _strategy(r, w, c0)
        if st:
            variants.append({"weights": w, "include_otc": inc, "metrics": st["metrics"]})
    notes.extend(otc_hindsight_notes(res, volume, exchanges))
    sens = []
    for m in ["next", "accepted", "same", "report"]:
        try:
            r = res if m == args.entry else run_backtest(filings, adj, cal, mode=m, **kw)
        except ValueError as e:
            notes.append(f"感度（{MODE_JP[m]}）を計算できなかった: {e}")
            continue
        sens.append(_sensitivity_row(r, m, c0))

    edgar_name = filings[-1].get("edgar_name") or f"CIK {cik}"
    meta = {
        "name": args.name or edgar_name, "edgar_name": edgar_name, "cik": cik,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "calendar_ref": cal_ref, "notes": notes, "price_warnings": price_warnings,
        "params": {"weights": weight_modes, "cost_bps": costs, "bench": args.bench,
                   "entry": args.entry, "include_otc": args.include_otc, "max_stale_days": MAX_STALE},
        "edgar_coverage": cov, "first_filing_is_initial": initial,
        "exchanges": exchanges,
    }
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render_markdown(res, meta, sens, variants), encoding="utf-8")
    out_js.parent.mkdir(parents=True, exist_ok=True)
    out_js.write_text(json.dumps(_clean({"meta": meta, "result": res, "sensitivity": sens,
                                         "variants": variants}),
                                 ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")

    print("=" * 74)
    for line in _headline(res, meta["name"], sens):
        print(f"- {line}")
    n_un = sum(len(p["unpriced"]) for p in res["periods"])
    if n_un:
        print(f"⚠ 評価不能・写せない銘柄 {n_un} 件（除外して按分し直した。レポートに一覧）")
    print(f"レポート: {out_md}")
    print(f"JSON    : {out_js}")
    if args.web:
        out_web = Path(args.web)
        out_web.parent.mkdir(parents=True, exist_ok=True)
        text = web_json(web_payload(res, meta, sens, variants))
        out_web.write_text(text, encoding="utf-8")
        size = len(text.encode("utf-8"))
        print(f"Web 用  : {out_web}（{size / 1000:.1f} KB）")
        if size > WEB_MAX_BYTES:
            print(f"⚠ Web 用の JSON が {WEB_MAX_BYTES / 1000:.0f} KB を超えた。ページの読み込みが重くなる")
    db.close()


if __name__ == "__main__":
    main()
