"""13F の集計から、ティッカー付きの購入候補リストを作る。

    python tools/screen_candidates.py --top 25
    python tools/screen_candidates.py --styles value quality
    python tools/screen_candidates.py --min-holders 3

reports/candidates.md に、候補の表と config/compliance.yaml へ追記する YAML を出す。
候補はあくまで「独立性の確認を始める対象」。買い推奨ではない。

対象は 50 銘柄以下の選別型の運用会社（クオンツの一括票を避ける）。
ETF は候補から外し、普通株・ADR・REIT のみ。ティッカーが解決できないものは載せない。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_13f import DB_PATH, EQUITY_TYPES, LIMITS, OUT_DIR, SELECTIVE_MAX_POSITIONS  # noqa: E402
from analyze_13f import aggregate, load_positions  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def screen(pos, filers, styles=None, min_holders=2):
    ciks = set(filers[filers["n_positions"] <= SELECTIVE_MAX_POSITIONS]["cik"])
    if styles:
        wanted = {s.lower() for s in styles}
        ciks &= set(filers[filers["style"].fillna("").str.lower().isin(wanted)]["cik"])

    agg = aggregate(pos[pos["cik"].isin(ciks)])
    if agg.empty:
        return agg, len(ciks)

    agg = agg[agg["ticker"].notna() & agg["sec_type"].isin(EQUITY_TYPES)]
    agg = agg[agg["holders"] >= min_holders]
    return agg.reset_index(drop=True), len(ciks)


def main() -> None:
    ap = argparse.ArgumentParser(description="13F から購入候補を抽出する")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--styles", nargs="+", help="運用者のスタイルで絞る（例: value quality）")
    ap.add_argument("--min-holders", type=int, default=2, help="最低保有者数（既定 2）")
    args = ap.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"{DB_PATH} がありません。先に python tools/fetch_13f.py を実行してください。")

    db = sqlite3.connect(str(DB_PATH))
    pos, filers, _ = load_positions(db)
    db.close()

    cand, n_filers = screen(pos, filers, args.styles, args.min_holders)
    top = cand.head(args.top)

    L = ["# 購入候補（13F より）", "",
         f"報告基準日: {filers['report_date'].max()} ／ 対象の運用者 {n_filers} 社"
         + (f"（スタイル: {', '.join(args.styles)}）" if args.styles else ""), "", LIMITS, ""]

    if top.empty:
        L.append("_条件に合う銘柄がありません。--min-holders を下げるか、スタイルの指定を外してください。_")
    else:
        L.append("| # | ティッカー | 会社 | 保有者数 | 平均比率% | 上位保有者 |")
        L.append("|--:|---|---|--:|--:|---|")
        for i, r in top.iterrows():
            L.append(f"| {i + 1} | {r['ticker']} | {r['company']} | {r['holders']} | "
                     f"{r['avg_weight_pct']:.1f} | {r['top_holders']} |")
        L.append("")
        L.append("## config/compliance.yaml への追記用")
        L.append("")
        L.append("独立性の確認を始める銘柄の一覧です。確認結果は自分で DESC を見て記入してください。")
        L.append("")
        L.append("```yaml")
        L.append("securities:")
        for _, r in top.iterrows():
            L.append(f"  - symbol: {r['ticker']}")
            L.append("    desc_result: unchecked")
            L.append(f"    note: \"13F 候補（保有者 {r['holders']} 社）\"")
        L.append("```")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "candidates.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"出力しました: {out}（候補 {len(top)} 件 / 運用者 {n_filers} 社）")


if __name__ == "__main__":
    main()
