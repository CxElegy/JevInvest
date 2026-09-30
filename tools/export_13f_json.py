"""13F を、ページ（web/ledger.html の「著名投資家」タブ）用の JSON にする。

    SEC_USER_AGENT="名前 連絡先メール" python tools/export_13f_json.py
    # → web/investors.json

先に tools/fetch_13f.py（既定で直近 2 四半期）を実行しておくこと。

出すもの:
  - 投資家ごと: 業種の配分とその前期比、上位銘柄（前期比の売買つき）、新規購入、全売却
  - 選別型（50 銘柄以下）の投資家全体: 共通して持たれている銘柄、買われた銘柄・売られた銘柄、
    業種配分の平均とその前期比

データの正直さのルール:
  - 最新四半期の提出が無い運用会社は集計から外し、一覧で明示する
  - 前期が極端に小さい（提出法人の移行期など）場合は前期比を出さない
  - 数千銘柄を持つ運用会社は、上位 20 銘柄だけに業種を付け、その旨を表示する
  - 売買の判定は株数で行う（評価額は株価で動くため）
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import figi  # noqa: E402
import sectors as sec  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "edgar13f.sqlite"
ROSTER = ROOT / "config" / "investors.yaml"
OUT = ROOT / "web" / "investors.json"

SELECTIVE_MAX = 50        # これ以下の銘柄数を「選別型」とする
LARGE_TOP_N = 20          # 大規模な運用会社で業種を付ける上位銘柄数
CHANGE_THRESHOLD = 0.10   # 株数が ±10% 以上動いたら買い増し・減少とみなす
MIN_PREV_RATIO = 0.25     # 前期の総額が今期の 25% 未満なら前期比を出さない

STYLE_JP = {
    "value": "バリュー", "quality": "クオリティ", "growth": "グロース",
    "activist": "アクティビスト", "macro": "マクロ", "quant": "クオンツ",
    "concentrated": "集中投資",
}


def next_deadline(report_date: str) -> str:
    """次の四半期末から 45 日後（13F の提出期限）。"""
    d = date.fromisoformat(report_date)
    q_end = {3: (6, 30), 6: (9, 30), 9: (12, 31), 12: (3, 31)}[d.month]
    y = d.year + (1 if d.month == 12 else 0)
    return (date(y, *q_end) + timedelta(days=45)).isoformat()


def _positions(db: sqlite3.Connection, cik: str, report_date: str) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT cusip, issuer, value, shares, share_type FROM positions"
        " WHERE cik=? AND report_date=?", db, params=[cik, report_date],
    )


def _grouped(df: pd.DataFrame, total: float) -> pd.DataFrame:
    """会社単位（議決権クラスをまとめる）に集計する。"""
    if df.empty:
        return pd.DataFrame(columns=["value", "shares", "weight", "ticker", "company", "sector",
                                     "resolved"])
    df = df.copy()
    df["sh"] = df["shares"].where(df["share_type"] == "SH", 0.0)
    g = df.groupby("group_key").agg(
        value=("value", "sum"), shares=("sh", "sum"),
        ticker=("ticker", figi.modal), company=("company", figi.modal),
        issuer=("issuer", figi.modal), sector=("sector", figi.modal),
    )
    g["company"] = g["company"].fillna(g["issuer"])
    g["sector"] = g["sector"].where(g["sector"].notna(), sec.OTHER)
    g["resolved"] = g["ticker"].notna()
    g["weight"] = g["value"] / total * 100.0 if total else 0.0
    return g.sort_values("value", ascending=False)


def _label(r) -> dict:
    """画面に出す銘柄の表記。社債・優先株は発行体のティッカーにして注記する。"""
    t = r["ticker"] if isinstance(r["ticker"], str) else None
    c = r["company"] if isinstance(r["company"], str) else None
    if t and sec.is_non_common(t):
        c = f"{c}（社債・優先株など）"
    return {"t": sec.base_ticker(t) if t else None, "c": c, "s": r["sector"]}


def _clean(obj):
    """JSON に NaN を出さない（ブラウザの JSON.parse が失敗する）。"""
    if isinstance(obj, float) and obj != obj:
        return None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def _change(cur_sh: float, prev_sh: float) -> tuple[str, float | None]:
    if prev_sh <= 0 and cur_sh > 0:
        return "new", None
    if cur_sh <= 0 < prev_sh:
        return "exit", -100.0
    if prev_sh <= 0:
        return "hold", None
    r = cur_sh / prev_sh - 1.0
    if r >= CHANGE_THRESHOLD:
        return "add", r * 100
    if r <= -CHANGE_THRESHOLD:
        return "trim", r * 100
    return "hold", r * 100


def _sector_weights(g: pd.DataFrame) -> dict[str, float]:
    """業種ごとの比率。業種が分かった銘柄の合計を 100 とする。"""
    known = g[g["resolved"]]
    tot = known["value"].sum()
    if tot <= 0:
        return {}
    s = known.groupby("sector")["value"].sum() / tot * 100.0
    return {k: float(v) for k, v in s.items()}


def build(db: sqlite3.Connection, roster: list[dict], cmap: dict, sic: dict,
          generated: str | None = None) -> dict:
    filers = pd.read_sql("SELECT * FROM filers", db)
    person = {e["name"]: e.get("person", e["name"]) for e in roster}

    dates = filers.groupby("cik")["report_date"].apply(lambda s: sorted(s, reverse=True))
    latest_q = max(v[0] for v in dates)

    def annotate(df: pd.DataFrame) -> pd.DataFrame:
        df = figi.annotate(df, cmap)
        df["sector"] = [
            sec.sector_for((sic.get(sec.sec_ticker(t)) or {}).get("sic"), st)
            if isinstance(t, str) else None
            for t, st in zip(df["ticker"], df["sec_type"], strict=True)
        ]
        return df

    out_filers, excluded = [], []
    focus: list[dict] = []   # 全体集計に使う（選別型・今期あり・前期比あり）

    for cik, ds in dates.items():
        cur_row = filers[(filers["cik"] == cik) & (filers["report_date"] == ds[0])].iloc[0]
        name = cur_row["roster_name"]
        if ds[0] != latest_q:
            excluded.append({"name": name, "person": person.get(name, name),
                             "report_date": ds[0]})
            continue

        total = float(cur_row["total_value"])
        n = int(cur_row["n_positions"])
        cur = _grouped(annotate(_positions(db, cik, ds[0])), total)

        prev = None
        prev_date = ds[1] if len(ds) > 1 else None
        comparable = False
        if prev_date:
            prow = filers[(filers["cik"] == cik) & (filers["report_date"] == prev_date)].iloc[0]
            gap = (date.fromisoformat(ds[0]) - date.fromisoformat(prev_date)).days
            if 80 <= gap <= 100 and float(prow["total_value"]) >= total * MIN_PREV_RATIO:
                comparable = True
                prev = _grouped(annotate(_positions(db, cik, prev_date)),
                                float(prow["total_value"]))

        selective = n <= SELECTIVE_MAX
        basis = cur if selective else cur.head(LARGE_TOP_N)
        pbasis = None if prev is None else (prev if selective else prev.head(LARGE_TOP_N))
        sw = _sector_weights(basis)
        psw = _sector_weights(pbasis) if pbasis is not None else {}
        coverage = float(basis[basis["resolved"]]["value"].sum() / total * 100) if total else 0.0

        def chg(key: str) -> tuple[str | None, float | None]:
            if prev is None:
                return None, None
            cs = float(cur.loc[key, "shares"]) if key in cur.index else 0.0
            ps = float(prev.loc[key, "shares"]) if key in prev.index else 0.0
            return _change(cs, ps)

        top = []
        for key, r in cur.head(10).iterrows():
            c, pct = chg(key)
            top.append({**_label(r),
                        "w": round(float(r["weight"]), 2), "chg": c,
                        "chg_pct": None if pct is None else round(pct, 1)})

        new, exits = [], []
        if prev is not None and selective:
            for key, r in cur[~cur.index.isin(prev.index)].head(8).iterrows():
                new.append({**_label(r),
                            "w": round(float(r["weight"]), 2)})
            for key, r in prev[~prev.index.isin(cur.index)].head(8).iterrows():
                exits.append({**_label(r),
                              "pw": round(float(r["weight"]), 2)})

        rec = {
            "name": name, "person": person.get(name, name),
            "style": cur_row["style"], "style_jp": STYLE_JP.get(cur_row["style"], cur_row["style"]),
            "n": n, "total_usd": total, "report_date": ds[0], "filed": cur_row["filed_date"],
            "prev_date": prev_date if comparable else None, "comparable": comparable,
            "selective": selective,
            "sector_basis": "全保有" if selective else f"上位 {LARGE_TOP_N} 銘柄",
            "coverage_pct": round(coverage, 1),
            "sectors": sorted(
                ({"s": k, "w": round(v, 1),
                  "dw": round(v - psw.get(k, 0.0), 1) if psw else None}
                 for k, v in sw.items()),
                key=lambda d: -d["w"]),
            "top": top, "new": new, "exits": exits,
        }
        out_filers.append(rec)
        if selective and prev is not None:
            focus.append({"rec": rec, "cur": cur, "prev": prev, "sw": sw, "psw": psw})

    out_filers.sort(key=lambda r: -r["total_usd"])

    # ---- 選別型の投資家全体 ----
    holders: dict[str, dict] = {}
    flows: dict[str, dict] = {}
    for f in focus:
        who = f["rec"]["person"]
        cur, prev = f["cur"], f["prev"]
        for key, r in cur.iterrows():
            h = holders.setdefault(key, {**_label(r), "ws": [], "names": []})
            h["ws"].append(float(r["weight"]))
            h["names"].append(who)
        for key in set(cur.index) | set(prev.index):
            cs = float(cur.loc[key, "shares"]) if key in cur.index else 0.0
            ps = float(prev.loc[key, "shares"]) if key in prev.index else 0.0
            c, _ = _change(cs, ps)
            if c == "hold":
                continue
            src = cur.loc[key] if key in cur.index else prev.loc[key]
            e = flows.setdefault(key, {**_label(src), "new": [], "add": [], "trim": [], "exit": []})
            e[c].append(who)

    consensus = sorted(
        ({"t": h["t"], "c": h["c"], "s": h["s"], "holders": len(h["names"]),
          "avg_w": round(sum(h["ws"]) / len(h["ws"]), 2), "names": h["names"]}
         for h in holders.values() if len(h["names"]) >= 2),
        key=lambda d: (-d["holders"], -d["avg_w"]))[:25]
    buys = sorted((e for e in flows.values() if e["new"] or e["add"]),
                  key=lambda e: (-(len(e["new"]) + len(e["add"])), -len(e["new"])))[:20]
    sells = sorted((e for e in flows.values() if e["exit"] or e["trim"]),
                   key=lambda e: (-(len(e["exit"]) + len(e["trim"])), -len(e["exit"])))[:20]

    shift = []
    if focus:
        for s in sec.SECTORS:
            w = sum(f["sw"].get(s, 0.0) for f in focus) / len(focus)
            pw = sum(f["psw"].get(s, 0.0) for f in focus) / len(focus)
            if w > 0.05 or pw > 0.05:
                shift.append({"s": s, "w": round(w, 1), "pw": round(pw, 1), "dw": round(w - pw, 1)})
        shift.sort(key=lambda d: -d["w"])

    prev_q = max((f["rec"]["prev_date"] for f in focus), default=None)
    return _clean({
        "generated": generated or datetime.now().strftime("%Y-%m-%d %H:%M"),
        "report_date": latest_q, "prev_date": prev_q, "next_deadline": next_deadline(latest_q),
        "sectors": list(sec.SECTORS),
        "group": {"n": len(focus), "names": [f["rec"]["person"] for f in focus],
                  "consensus": consensus, "buys": buys, "sells": sells, "sector_shift": shift},
        "filers": out_filers, "excluded": excluded,
    })


def cusips_to_resolve(db: sqlite3.Connection) -> list[str]:
    filers = pd.read_sql("SELECT cik, report_date, n_positions FROM filers", db)
    out: set[str] = set()
    for cik, g in filers.groupby("cik"):
        g = g.sort_values("report_date", ascending=False).head(2)
        big = int(g.iloc[0]["n_positions"]) > SELECTIVE_MAX
        for rd in g["report_date"]:
            q = ("SELECT cusip FROM positions WHERE cik=? AND report_date=? ORDER BY value DESC"
                 + (f" LIMIT {LARGE_TOP_N}" if big else ""))
            out.update(c for (c,) in db.execute(q, (cik, rd)))
    return sorted(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="13F をページ用の JSON にする")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    if not DB_PATH.exists():
        raise SystemExit("先に python tools/fetch_13f.py を実行してください。")
    if "SEC_USER_AGENT" not in os.environ:
        print("⚠ SEC_USER_AGENT が未設定です。業種の取得で SEC に拒否される可能性があります。")

    import fetch_13f  # SEC の連絡先付きクライアントを使う

    db = sqlite3.connect(str(DB_PATH))
    roster = yaml.safe_load(ROSTER.read_text(encoding="utf-8"))["investors"]
    cmap = figi.resolve(db, cusips_to_resolve(db))
    tickers = [v["ticker"] for v in cmap.values() if v.get("ticker")
               and v.get("security_type") not in sec.FUND_TYPES]
    ed = fetch_13f.Edgar()
    try:
        sic = sec.resolve(db, ed, tickers)
    finally:
        ed.close()

    payload = build(db, roster, cmap, sic)
    db.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                        encoding="utf-8")
    g = payload["group"]
    print(f"出力しました: {args.out}")
    print(f"  基準日 {payload['report_date']}（前期 {payload['prev_date']}）"
          f" / 投資家 {len(payload['filers'])} 社 / 選別型の集計 {g['n']} 社"
          f" / 除外 {len(payload['excluded'])} 社")


if __name__ == "__main__":
    main()
