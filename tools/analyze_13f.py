"""fetch_13f.py で貯めた 13F を集計して Markdown にする。

    python tools/analyze_13f.py                 # reports/investors-13f.md を出力
    python tools/analyze_13f.py --top 40
    python tools/analyze_13f.py --no-resolve    # OpenFIGI を叩かない（ティッカー無しの集計）

集計で処理している落とし穴（README にも一覧がある）:
  - 最新四半期以外の運用会社（提出が止まったもの）は除外し、レポートに明記する
  - 同じ運用会社の GOOG / GOOGL のような議決権クラス違いは、先に (運用会社, 会社) でまとめる
    （まとめないと保有者数が 2 票になる）
  - 銘柄のキーは会社名ではなく CUSIP → OpenFIGI の標準名。未解決は CUSIP のまま別扱いにし、
    1 つの「NAN」に潰さない
  - クオンツ（数千銘柄）が票を独占しないよう、50 銘柄以下の「選別型」の集計を別に出す
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import figi  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "edgar13f.sqlite"
OUT_DIR = ROOT / "reports"

SELECTIVE_MAX_POSITIONS = 50
EQUITY_TYPES = {"Common Stock", "ADR", "REIT", "Depositary Receipt"}

LIMITS = """\
> **13F の限界（数字を読む前に）**
> - 米国上場の株式ロングのみ。現金・債券・非米国株・空売り・非上場事業は写らない。
>   Berkshire の巨額の短期債も写らないので、13F を「その人の資産配分」と読むのは誤り
> - 四半期末から 45 日後の提出なので、常に 1〜4 ヶ月古い
> - 取得単価と保有期間が写らない。数十年保有で含み益が巨大な銘柄は、税金の都合で売れないだけの場合がある
> - 米国外の運用者は米国保有分だけが見える
> - **生存者バイアス。** 名簿は「今有名な人」で作られており、同じ手法で失敗した運用会社は入っていない
> - 買い推奨ではない。独立性の確認（DESC・事前承認）が済むまで購入候補にすぎない
"""


def load_positions(db: sqlite3.Connection, cmap: dict | None = None,
                   resolve: bool = True, verbose: bool = True):
    """最新四半期の保有明細に、ティッカー・会社名・グループキーを付けて返す。

    戻り値: (positions, filers, excluded)
      excluded … 最新四半期ではないため集計から外した運用会社
    """
    filers = pd.read_sql("SELECT * FROM filers", db)
    if filers.empty:
        raise SystemExit("データがありません。先に python tools/fetch_13f.py を実行してください。")

    latest_by_cik = filers.groupby("cik")["report_date"].max()
    latest = latest_by_cik.max()
    current_ciks = set(latest_by_cik[latest_by_cik == latest].index)
    excluded = filers[filers["cik"].isin(set(latest_by_cik.index) - current_ciks)]
    excluded = excluded.sort_values("report_date").drop_duplicates("cik", keep="last")
    filers = filers[(filers["cik"].isin(current_ciks)) & (filers["report_date"] == latest)]

    pos = pd.read_sql(
        "SELECT * FROM positions WHERE report_date = ?", db, params=[latest]
    )
    pos = pos[pos["cik"].isin(current_ciks)]
    pos = pos.merge(
        filers[["cik", "roster_name", "style", "n_positions", "total_value"]],
        on="cik", how="left",
    )
    pos["weight_pct"] = pos["value"] / pos["total_value"].where(pos["total_value"] > 0) * 100.0

    if cmap is None:
        cmap = figi.resolve(db, sorted(pos["cusip"].unique()), verbose=verbose) if resolve else {}
    pos = figi.annotate(pos, cmap)
    return pos, filers, excluded


def aggregate(pos: pd.DataFrame) -> pd.DataFrame:
    """会社単位に集計する。先に (運用会社, 会社) でまとめて議決権クラスの二重計上を防ぐ。"""
    if pos.empty:
        return pd.DataFrame()

    per_filer = (
        pos.groupby(["cik", "group_key"], as_index=False)
        .agg(
            value=("value", "sum"),
            weight_pct=("weight_pct", "sum"),
            roster_name=("roster_name", "first"),
            ticker=("ticker", figi.modal),
            company=("company", figi.modal),
            sec_type=("sec_type", figi.modal),
        )
    )

    def top_holders(g: pd.DataFrame) -> str:
        top = g.sort_values("value", ascending=False)["roster_name"].head(3)
        return ", ".join(str(n) for n in top)

    rows = []
    for key, g in per_filer.groupby("group_key"):
        rows.append(
            {
                "group_key": key,
                "ticker": figi.modal(g["ticker"]),
                "company": figi.modal(g["company"]) or key,
                "sec_type": figi.modal(g["sec_type"]),
                "holders": g["cik"].nunique(),
                "total_value": g["value"].sum(),
                "avg_weight_pct": g["weight_pct"].mean(),
                "top_holders": top_holders(g),
            }
        )
    out = pd.DataFrame(rows)
    return out.sort_values(["holders", "avg_weight_pct"], ascending=False).reset_index(drop=True)


def _table(agg: pd.DataFrame, top: int) -> str:
    if agg.empty:
        return "_（データなし）_\n"
    d = agg.head(top).copy()
    d["保有額($M)"] = (d["total_value"] / 1e6).map(lambda v: f"{v:,.0f}")
    d["平均比率%"] = d["avg_weight_pct"].map(lambda v: f"{v:.1f}")
    d = d.rename(columns={"ticker": "ティッカー", "company": "会社", "holders": "保有者数",
                          "top_holders": "上位保有者"})
    d["ティッカー"] = d["ティッカー"].fillna("（未解決）")
    return d[["ティッカー", "会社", "保有者数", "保有額($M)", "平均比率%", "上位保有者"]].to_markdown(
        index=False) + "\n"


def build_report(pos: pd.DataFrame, filers: pd.DataFrame, excluded: pd.DataFrame,
                 top: int) -> str:
    latest = filers["report_date"].max()
    unresolved = pos[pos["ticker"].isna()]["cusip"].nunique()

    L = ["# 著名投資家の 13F 集計", "", f"報告基準日: {latest} ／ 対象 {len(filers)} 社", "", LIMITS]

    if not excluded.empty:
        L.append(f"## 集計から除外した運用会社（{len(excluded)} 社・最新四半期の提出なし）")
        L.append("")
        for _, r in excluded.iterrows():
            L.append(f"- {r['roster_name']}（最後の報告日 {r['report_date']}）")
        L.append("")

    if unresolved:
        L.append(f"> ⚠ ティッカー未解決の CUSIP が {unresolved} 件あります"
                 "（CUSIP のまま別々に集計。1 つにまとめていません）。")
        L.append("")

    L.append(f"## 保有者数の多い銘柄（全 {len(filers)} 社）")
    L.append("")
    L.append(_table(aggregate(pos), top))

    selective_ciks = set(filers[filers["n_positions"] <= SELECTIVE_MAX_POSITIONS]["cik"])
    sel = pos[pos["cik"].isin(selective_ciks)]
    L.append(f"## 選別型のみ（{SELECTIVE_MAX_POSITIONS} 銘柄以下の {len(selective_ciks)} 社）")
    L.append("")
    L.append("数千銘柄を持つクオンツが全銘柄に票を入れて大型株一覧になるのを避けるため、"
             "集中投資型に絞った集計です。")
    L.append("")
    L.append(_table(aggregate(sel), top))
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="13F を集計して Markdown にする")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--no-resolve", action="store_true", help="OpenFIGI を使わない")
    args = ap.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"{DB_PATH} がありません。先に python tools/fetch_13f.py を実行してください。")

    db = sqlite3.connect(str(DB_PATH))
    pos, filers, excluded = load_positions(db, resolve=not args.no_resolve)
    text = build_report(pos, filers, excluded, args.top)
    db.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "investors-13f.md"
    out.write_text(text, encoding="utf-8")
    print(f"出力しました: {out}（{len(filers)} 社 / 除外 {len(excluded)} 社）")


if __name__ == "__main__":
    main()
