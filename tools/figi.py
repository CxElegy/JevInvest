"""CUSIP からティッカー・会社名を解決する（OpenFIGI）。

13F には発行体名しか入っておらず、その名前は集計キーとして使えない:

  1. 総称名の衝突 — "ISHARES TR" は数百の iShares ETF が共有する名前。
     名前照合は一致度 1.00 で SLV（銀 ETF）を返したが、CUSIP 別では
     464287200 = IVV（S&P500）、464287655 = IWM（Russell2000）だった。
     **自信ありげに間違う**最悪のパターン。
  2. 略語 — "APPLIED MATLS INC" の MATLS は MATERIALS の略で前方一致しない。
  3. 切り詰め — 28〜30 文字で切られている提出が多い。

CUSIP は一意なのでこれらが全部消える。結果は SQLite にキャッシュする。
"""

from __future__ import annotations

import re
import sqlite3
import time

import httpx
import pandas as pd

FIGI_URL = "https://api.openfigi.com/v3/mapping"
# API キー無しの制限: 1 リクエスト 10 件まで、6 秒あたり 25 リクエスト。
# 0.3 秒間隔では 429 を踏んだ（固定ウィンドウ制限で平均間隔では足りない）。
FIGI_BATCH = 10
FIGI_INTERVAL = 0.55
FIGI_RETRIES = 4

# FIGI の名称末尾に付くクラス・種別の表記。
# 例: "ALPHABET INC-CL A" / "MASTERCARD INC - A" / "TAIWAN SEMICONDUCTOR-SP ADR"
_CLASS_SUFFIX = re.compile(
    r"""\s*-\s*(
        CL(ASS)?\s*[A-Z0-9]{1,2}      # -CL A / -CLASS B
      | SP\s*ADR | ADR | ADS | GDR    # 預託証券
      | US | REDH | UNSPON
      | [A-Z]                          # " - A" のような裸のクラス表記
    )\s*$""",
    re.X,
)


def strip_share_class(name) -> str | None:
    """FIGI の名称からクラス表記を落として会社名にする。

    GOOGL("ALPHABET INC-CL A") と GOOG("ALPHABET INC-CL C") を
    同じ会社として扱うために使う。複数段の表記も落とせるよう繰り返す。

    ★NaN の扱いに注意。`if not name` では NaN を捕まえられない
      （NaN は Python では真値）。それをやると str(nan) = "NAN" が返り、
      未解決の CUSIP がすべて "NAN" という 1 つの会社にまとまってしまう。
      実際にこれを踏んで「保有者 41 社」という実在しない集計が出た。
    """
    if name is None or pd.isna(name):
        return None
    s = str(name).upper().strip()
    if not s or s == "NAN":
        return None
    for _ in range(3):
        new = _CLASS_SUFFIX.sub("", s).strip()
        if new == s:
            break
        s = new
    return s or None


def init_table(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS cusip_map (
               cusip            TEXT PRIMARY KEY,
               ticker           TEXT,
               name             TEXT,
               exch_code        TEXT,
               security_type    TEXT,
               share_class_figi TEXT,
               resolved         INTEGER NOT NULL,
               fetched_at       REAL
           )"""
    )
    db.commit()


def _pick(entries: list[dict]) -> dict:
    """FIGI の複数結果から US 上場を優先して 1 つ選ぶ。

    同じ CUSIP が取引所ごとに返ってくるので、これをやらないと
    海外の重複上場（GE が exchCode=PE など）を拾ってしまう。
    """
    for e in entries:
        if e.get("exchCode") == "US":
            return e
    return entries[0]


def resolve(db: sqlite3.Connection, cusips: list[str], refresh: bool = False,
            verbose: bool = True) -> dict[str, dict]:
    """CUSIP 群を解決して辞書で返す。キャッシュ済みのものは API を叩かない。"""
    init_table(db)

    known: dict[str, dict] = {}
    if not refresh:
        # resolved=0 は「FIGI に無い」ではなく「取得できなかった」場合もあるので、
        # 未解決のものは毎回再挑戦する。
        for row in db.execute(
            "SELECT cusip, ticker, name, exch_code, security_type, share_class_figi"
            " FROM cusip_map WHERE resolved = 1"
        ):
            known[row[0]] = {
                "ticker": row[1], "name": row[2], "exch_code": row[3],
                "security_type": row[4], "share_class_figi": row[5],
            }

    todo = [c for c in cusips if c not in known]
    if not todo:
        return known

    if verbose:
        n_req = (len(todo) + FIGI_BATCH - 1) // FIGI_BATCH
        print(f"OpenFIGI で {len(todo):,} 件の CUSIP を解決します（{n_req:,} リクエスト）…")

    client = httpx.Client(timeout=60, headers={"Content-Type": "application/json"})
    try:
        for i in range(0, len(todo), FIGI_BATCH):
            chunk = todo[i : i + FIGI_BATCH]
            body = [{"idType": "ID_CUSIP", "idValue": c} for c in chunk]

            # 429 は固定ウィンドウ制限なので、待って再試行すれば通る。
            # 一度の失敗で未解決として確定させると、その CUSIP が
            # キャッシュに「解決不能」として残ってしまう。
            results = None
            delay = 6.0
            for attempt in range(FIGI_RETRIES):
                try:
                    r = client.post(FIGI_URL, json=body)
                    if r.status_code == 429:
                        time.sleep(delay)
                        delay = min(delay * 1.5, 30.0)
                        continue
                    r.raise_for_status()
                    results = r.json()
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == FIGI_RETRIES - 1:
                        if verbose:
                            print(f"  ⚠ バッチ {i // FIGI_BATCH + 1} 失敗: {e}")
                    else:
                        time.sleep(delay)
                        delay = min(delay * 1.5, 30.0)

            if results is None:
                continue  # 解決不能として記録せず、次回に再挑戦できるよう飛ばす

            rows = []
            for cusip, res in zip(chunk, results, strict=False):
                data = (res or {}).get("data") or []
                if data:
                    d = _pick(data)
                    rec = {
                        "ticker": d.get("ticker"), "name": d.get("name"),
                        "exch_code": d.get("exchCode"),
                        "security_type": d.get("securityType"),
                        "share_class_figi": d.get("shareClassFIGI"),
                    }
                    known[cusip] = rec
                    ok = 1
                else:
                    rec = {k: None for k in
                           ("ticker", "name", "exch_code", "security_type", "share_class_figi")}
                    ok = 0
                rows.append(
                    (cusip, rec["ticker"], rec["name"], rec["exch_code"],
                     rec["security_type"], rec["share_class_figi"], ok, time.time())
                )

            db.executemany("INSERT OR REPLACE INTO cusip_map VALUES (?,?,?,?,?,?,?,?)", rows)
            db.commit()

            if verbose and (i // FIGI_BATCH) % 25 == 24:
                print(f"  {min(i + FIGI_BATCH, len(todo)):,}/{len(todo):,} 件")
            time.sleep(FIGI_INTERVAL)
    finally:
        client.close()

    return known


def annotate(pos: pd.DataFrame, cmap: dict[str, dict]) -> pd.DataFrame:
    """positions に ticker / 会社名 / 種別 / グループキーを付ける。

    group_key は「会社単位」。shareClassFIGI は株式クラスの識別子であって
    会社の ID ではない（GOOGL と GOOG で別値になる）ので使えない。
    FIGI の標準化された名称からクラス表記を落としてまとめる。
    """
    pos = pos.copy()
    pos["ticker"] = pos["cusip"].map(lambda c: (cmap.get(c) or {}).get("ticker"))
    pos["figi_name"] = pos["cusip"].map(lambda c: (cmap.get(c) or {}).get("name"))
    pos["sec_type"] = pos["cusip"].map(lambda c: (cmap.get(c) or {}).get("security_type"))
    pos["company"] = pos["figi_name"].map(strip_share_class)

    # fillna(Series) に頼らず明示的に組む。
    # fillna でやると未解決分が 1 グループに潰れ、実在しない集計が出る。
    # `c if c else ...` では NaN が真値として通り、group_key が NaN になって
    # 集計の groupby に黙って捨てられる。pd.isna で明示的に判定する。
    pos["group_key"] = [
        f"CUSIP:{cu}" if (c is None or pd.isna(c) or not c) else c
        for c, cu in zip(pos["company"], pos["cusip"], strict=True)
    ]
    return pos


def modal(s: pd.Series):
    """グループ内の最頻値。pd.notna で弾く（`if v` では NaN が残る）。"""
    from collections import Counter

    vals = [v for v in s if pd.notna(v) and v]
    return Counter(vals).most_common(1)[0][0] if vals else None
