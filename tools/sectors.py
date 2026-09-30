"""ティッカーに業種を付ける（SEC に登録された SIC コードから）。

13F にも OpenFIGI にも業種は入っていない。各社が SEC に届け出ている
SIC（Standard Industrial Classification）を、画面用の大まかな業種に丸める。

  1. ティッカー → CIK   : https://www.sec.gov/files/company_tickers.json
  2. CIK → SIC          : https://data.sec.gov/submissions/CIK##########.json

結果は SQLite（sic_map）にキャッシュする。SIC は GICS（証券会社の業種分類）とは
粒度が違うので、あくまで「おおよその分野」として扱う。たとえば Alphabet と Meta は
SIC では 7370（コンピュータ関連サービス）なので「ソフトウェア・インターネット」に入る。
"""

from __future__ import annotations

import sqlite3
import time

# 画面に出す業種（この順で並べる）
SECTORS = (
    "テクノロジー（ハード・半導体）",
    "ソフトウェア・インターネット",
    "通信・メディア",
    "金融",
    "決済・ビジネスサービス",
    "ヘルスケア",
    "生活必需品",
    "一般消費財・小売",
    "資本財・運輸",
    "エネルギー",
    "素材",
    "公益",
    "不動産",
    "ETF・ファンド",
    "その他・不明",
)

(TECH_HW, SOFT, COMM, FIN, BIZ, HEALTH, STAPLES, DISC, IND, ENERGY, MAT, UTIL,
 REALTY, FUND, OTHER) = SECTORS

# (開始, 終了, 業種)。上から順に最初に当たったものを使う。細かい範囲を先に置く。
_RANGES: tuple[tuple[int, int, str], ...] = (
    (2830, 2836, HEALTH), (2840, 2844, STAPLES), (3570, 3579, TECH_HW),
    (3660, 3699, TECH_HW), (3630, 3639, DISC), (3710, 3716, DISC), (3751, 3751, DISC),
    (3720, 3729, IND), (3826, 3826, HEALTH), (3840, 3851, HEALTH), (3861, 3861, TECH_HW),
    (3820, 3829, TECH_HW), (5122, 5122, HEALTH), (5400, 5499, STAPLES), (5912, 5912, STAPLES),
    (5331, 5331, STAPLES), (5399, 5399, STAPLES), (6770, 6770, OTHER), (6798, 6798, REALTY),
    (7370, 7379, SOFT), (7380, 7399, BIZ), (7812, 7841, COMM),
    (100, 999, MAT), (1000, 1099, MAT), (1200, 1399, ENERGY), (1400, 1499, MAT),
    (1500, 1799, IND), (2000, 2199, STAPLES), (2200, 2399, DISC), (2400, 2499, MAT),
    (2500, 2599, DISC), (2600, 2699, MAT), (2700, 2799, COMM), (2800, 2899, MAT),
    (2900, 2999, ENERGY), (3000, 3099, MAT), (3100, 3199, DISC), (3200, 3399, MAT),
    (3400, 3569, IND), (3580, 3629, IND), (3640, 3659, IND), (3700, 3799, IND),
    (3800, 3899, TECH_HW), (3900, 3999, DISC), (4000, 4799, IND), (4800, 4899, COMM),
    (4900, 4999, UTIL), (5000, 5199, IND), (5200, 5999, DISC), (6000, 6499, FIN),
    (6500, 6599, REALTY), (6700, 6799, FIN), (7000, 7299, DISC), (7300, 7369, BIZ),
    (7800, 7999, COMM), (8000, 8099, HEALTH), (8200, 8299, DISC), (8700, 8799, IND),
)

FUND_TYPES = {"ETP", "Mutual Fund", "Closed-End Fund", "Unit"}


def sector_for(sic: int | None, security_type: str | None = None) -> str:
    if security_type in FUND_TYPES:
        return FUND
    if not sic:
        return OTHER
    for lo, hi, name in _RANGES:
        if lo <= sic <= hi:
            return name
    return OTHER


def base_ticker(figi_ticker: str) -> str:
    """社債・優先株の表記（"GPN 1.5 03/01/31"、"GOOGL 6.25 05/15/29 A"）や
    末尾の記号（"EA*"）を落として、発行体の株式ティッカーにする。"""
    return figi_ticker.split()[0].rstrip("*") if figi_ticker else figi_ticker


def is_non_common(figi_ticker: str) -> bool:
    """ティッカーに利率や満期が付いている = 社債・優先株など、普通株以外。"""
    return bool(figi_ticker) and " " in figi_ticker.strip()


def sec_ticker(figi_ticker: str) -> str:
    """OpenFIGI の表記（BRK/B）を SEC の表記（BRK-B）にそろえる。"""
    return base_ticker(figi_ticker).replace("/", "-").upper()


def init_table(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS sic_map (
               ticker TEXT PRIMARY KEY, cik TEXT, sic INTEGER, sic_desc TEXT, fetched_at REAL
           )"""
    )
    db.commit()


def resolve(db: sqlite3.Connection, ed, tickers: list[str], verbose: bool = True) -> dict[str, dict]:
    """ティッカー群の SIC を返す。ed は fetch_13f.Edgar（SEC の連絡先付きクライアント）。"""
    init_table(db)
    want = sorted({sec_ticker(t) for t in tickers if t})
    known: dict[str, dict] = {}
    for t, cik, sic, desc in db.execute("SELECT ticker, cik, sic, sic_desc FROM sic_map"):
        known[t] = {"cik": cik, "sic": sic, "sic_desc": desc}

    todo = [t for t in want if t not in known]
    if not todo:
        return {t: known[t] for t in want if t in known}

    r = ed.get("https://www.sec.gov/files/company_tickers.json")
    table = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in r.json().values()}

    if verbose:
        print(f"SEC で {len(todo)} 銘柄の業種（SIC）を取得します…")
    for i, t in enumerate(todo, 1):
        cik = table.get(t)
        sic = desc = None
        if cik:
            resp = ed.get(f"https://data.sec.gov/submissions/CIK{cik}.json")
            if resp.status_code == 200:
                d = resp.json()
                sic = int(d["sic"]) if str(d.get("sic") or "").isdigit() else None
                desc = d.get("sicDescription")
        db.execute("INSERT OR REPLACE INTO sic_map VALUES (?,?,?,?,?)",
                   (t, cik, sic, desc, time.time()))
        known[t] = {"cik": cik, "sic": sic, "sic_desc": desc}
        if verbose and i % 100 == 0:
            print(f"  {i}/{len(todo)}")
            db.commit()
    db.commit()
    return {t: known[t] for t in want if t in known}
