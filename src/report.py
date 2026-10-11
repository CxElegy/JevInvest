"""分析結果を Markdown レポートにする。

このレポートが Claude Code に読ませる成果物。計算はすべて済んだ状態で渡し、
Claude には「判断」だけをさせる。API を呼ばないので Claude Pro のまま使える。

anonymize=True にすると絶対額を落として比率だけにする。
持仓の金額を外に出したくない場合に使う（PC を買うより安い解決策）。
"""

from __future__ import annotations

import pandas as pd

from .analytics import Analysis
from .compliance import ComplianceRegistry

# レポート上での表示ラベル
_STATE_LABEL = {
    "ok": "購入可",
    "unchecked": "DESC未確認",
    "stale": "再確認要",
    "pending": "承認待ち",
    "blocked": "不可",
    "escalate": "照会要",
}


def _money(v: float, ccy: str, anonymize: bool) -> str:
    if anonymize:
        return "—"
    return f"{v:,.0f} {ccy}"


def _pct(v: float) -> str:
    return f"{v:+.2f}%"


def _table(df: pd.DataFrame, money_cols: tuple[str, ...], ccy: str, anonymize: bool) -> str:
    if df.empty:
        return "_（データなし）_\n"
    d = df.copy()
    # 評価不能の行は None / NaN のまま来る。数値として書式化せず「—」にする
    # （None は書式化で落ち、NaN は "nan" と表に出てしまう）。
    for c in money_cols:
        if c in d.columns:
            d[c] = d[c].map(lambda v: "—" if anonymize or pd.isna(v) else f"{v:,.0f}")
    for c in d.columns:
        if c in money_cols:
            continue
        if pd.api.types.is_float_dtype(d[c]) or d[c].map(lambda v: v is None).any():
            d[c] = d[c].map(
                lambda v: "—" if v is None or (isinstance(v, float) and pd.isna(v))
                else (f"{v:,.2f}" if isinstance(v, (int, float)) else v)
            )
    del ccy
    return d.to_markdown() + "\n"


def _compliance_section(a: Analysis, reg: ComplianceRegistry) -> list[str]:
    """independence の確認状況。数字より前に置く — これは判断ではなくゲート。"""
    L: list[str] = []
    # 現金は証券ではないので independence の対象外
    held = sorted({v.position.symbol for v in a.valuations if not v.position.is_cash})

    rows = []
    restricted_held: list[str] = []   # DESC で制限事業体と確認済み
    unknown_held: list[str] = []      # 未確認・照会中で可否が不明
    for sym in held:
        v = reg.assess(sym)
        rows.append(
            {
                "銘柄": sym,
                "状態": _STATE_LABEL.get(v.state, v.state),
                "保有可": "○" if v.can_hold else "×",
                "買増可": "○" if v.can_buy else "×",
                "内容": v.reason,
            }
        )
        if v.state == "blocked" and not v.can_hold:
            restricted_held.append(sym)
        elif not v.can_hold:
            unknown_held.append(sym)

    L.append("## Independence（Deloitte 独立性）")
    L.append("")

    if not rows:
        L.append("_確認対象の証券はありません（現金のみ）。_")
        L.append("")
        return L

    if restricted_held:
        L.append(f"### 🚨 保有不可（制限事業体と確認済み）: {', '.join(restricted_held)}")
        L.append("")
        L.append(
            "**直ちに Independence & Conflicts Network に連絡してください。**"
            "売却が必要になり、その際の損失は自己負担になります。"
        )
        L.append("")

    if unknown_held:
        L.append(f"### ⚠ 保有可否が未確認: {', '.join(unknown_held)}")
        L.append("")
        L.append(
            "制限対象かどうか分からない状態で保有しています。"
            "DESC で確認してください。制限対象だった場合は上記と同じ対応が必要になります。"
        )
        L.append("")

    if reg.warnings:
        for w in dict.fromkeys(reg.warnings):
            L.append(f"- ⚠ {w}")
        L.append("")

    L.append(pd.DataFrame(rows).set_index("銘柄").to_markdown() + "\n")
    L.append(
        f"> 判定の正は DESC と Independence & Conflicts Network です。"
        f"このツールは「確認したか」の進行管理だけを行い、制限対象かどうかは判定しません。"
        f"DESC の再確認しきい値は {reg.recheck_after_days} 日に設定されています。"
    )
    L.append("")
    return L


def to_markdown(
    a: Analysis,
    anonymize: bool = False,
    compliance: ComplianceRegistry | None = None,
) -> str:
    ccy = a.base_currency
    L: list[str] = []

    L.append("# 持仓分析レポート")
    L.append("")
    L.append(f"基準日時: {a.as_of:%Y-%m-%d %H:%M}　/　基準通貨: {ccy}")
    if anonymize:
        L.append("")
        L.append("> 匿名モード: 絶対額を出力していません。比率と率だけで判断してください。")
    L.append("")

    # --- 警告を最初に出す。数字を信じる前に読むべきもの ---
    if a.warnings or a.unpriced_count:
        L.append("## ⚠ 先に確認すべきこと")
        L.append("")
        if a.unpriced_count:
            L.append(
                f"- **{a.unpriced_count} 件のポジションが評価不能** "
                f"（下記の集計・比率すべてから除外されています）"
            )
        for w in dict.fromkeys(a.warnings):  # 重複を潰して順序は保つ
            L.append(f"- {w}")
        L.append("")

    # --- ガードレール違反。自分で決めた上限を破っているかどうか ---
    #
    # 基準が設定されていれば、違反ゼロでも節を出す。
    # 何も出ないと「検査したが問題なし」と「検査していない」の区別がつかない。
    if a.guard_config:
        viol = [g for g in a.guards if g.severity == "violation"]
        warn = [g for g in a.guards if g.severity == "warning"]

        L.append("## ガードレール")
        L.append("")
        L.append("設定した基準: " + " / ".join(
            f"`{k}` = {v}" for k, v in a.guard_config.items()
        ))
        L.append("")
        if viol:
            L.append(f"### 🚨 上限・下限の違反 {len(viol)} 件")
            L.append("")
            for g in viol:
                L.append(
                    f"- **{g.subject}**: {g.detail}"
                    f"（現在 {g.current:.2f}% / 基準 {g.limit:.0f}%）"
                )
            L.append("")
        if warn:
            L.append(f"### ⚠ 注意 {len(warn)} 件")
            L.append("")
            for g in warn:
                unit = "" if g.guard == "high_correlation_threshold" else "%"
                L.append(
                    f"- **{g.subject}**: {g.detail}"
                    + ("" if g.guard == "high_correlation_threshold"
                       else f"（現在 {g.current:.2f}{unit} / 基準 {g.limit:.0f}{unit}）")
                )
            L.append("")
        if not viol and not warn:
            L.append("設定した基準はすべて満たしています。")
            L.append("")

    # --- independence は数字より前。買えないものを検討しても意味がない ---
    if compliance is not None:
        L.extend(_compliance_section(a, compliance))

    # --- サマリ ---
    L.append("## サマリ")
    L.append("")
    L.append("| 項目 | 値 |")
    L.append("|---|---:|")
    L.append(f"| 評価額 | {_money(a.total_value_base, ccy, anonymize)} |")
    L.append(f"| 取得原価 | {_money(a.total_cost_base, ccy, anonymize)} |")
    L.append(f"| 損益 | {_money(a.total_pnl_base, ccy, anonymize)} |")
    L.append(f"| 損益率 | {_pct(a.total_pnl_pct)} |")
    L.append("")

    # 値上がり益と為替差益の分解（分かる分だけ）
    price_pnl = sum(v.price_pnl_base for v in a.valuations if v.price_pnl_base is not None)
    fx_pnl = sum(v.fx_pnl_base for v in a.valuations if v.fx_pnl_base is not None)
    n_decomposed = sum(1 for v in a.valuations if v.price_pnl_base is not None)
    if n_decomposed:
        total_decomposed = price_pnl + fx_pnl
        L.append(f"### 損益の内訳（cost_fx_rate がある {n_decomposed} 件のみ）")
        L.append("")
        L.append("| 要因 | 金額 | 内訳 |")
        L.append("|---|---:|---:|")
        for label, v in (("値上がり益", price_pnl), ("為替差益", fx_pnl)):
            share = (v / total_decomposed * 100.0) if total_decomposed else 0.0
            L.append(f"| {label} | {_money(v, ccy, anonymize)} | {share:+.1f}% |")
        L.append("")
        L.append(
            "> 為替差益の比率が高い場合、その利益は銘柄選択の成果ではなく"
            "円安の結果です。円高に振れれば同じだけ戻ります。"
        )
        L.append("")

    # --- ポジション ---
    L.append("## ポジション")
    L.append("")
    rows = []
    for v in sorted(a.valuations, key=lambda x: -x.market_value_base):
        p = v.position
        if not v.priced:
            rows.append(
                {
                    "銘柄": p.symbol, "名称": p.name, "資産クラス": p.asset_class,
                    "口座": p.account, "評価額": None, "構成比%": None,
                    "損益": None, "損益率%": None, "価格": "評価不能",
                }
            )
            continue
        rows.append(
            {
                "銘柄": p.symbol,
                "名称": p.name[:24],
                "資産クラス": p.asset_class,
                "口座": p.account,
                "評価額": v.market_value_base,
                "構成比%": v.weight_pct,
                "損益": v.pnl_base,
                "損益率%": v.pnl_pct,
                "価格": v.price_source,
            }
        )
    pos_df = pd.DataFrame(rows).set_index("銘柄")
    L.append(_table(pos_df, ("評価額", "損益"), ccy, anonymize))

    # --- 配分 ---
    for title, df in (
        ("資産クラス別", a.by_asset_class),
        ("口座別", a.by_account),
        ("通貨別", a.by_currency),
    ):
        L.append(f"## {title}")
        L.append("")
        L.append(_table(df, ("評価額", "取得原価", "損益"), ccy, anonymize))

    # --- 集中度 ---
    if a.concentration:
        c = a.concentration
        L.append("## 集中度")
        L.append("")
        L.append("| 指標 | 値 |")
        L.append("|---|---:|")
        L.append(f"| 実銘柄数 | {c['実銘柄数']} |")
        L.append(f"| 実質銘柄数 (1/HHI) | {c['実質銘柄数']:.2f} |")
        L.append(f"| HHI | {c['hhi']:.4f} |")
        L.append(f"| 最大1銘柄 | {c['最大1銘柄%']:.2f}% |")
        L.append(f"| 上位3銘柄 | {c['上位3銘柄%']:.2f}% |")
        L.append(f"| 上位5銘柄 | {c['上位5銘柄%']:.2f}% |")
        L.append("")
        L.append(
            f"> 実銘柄数 {c['実銘柄数']} に対して実質 {c['実質銘柄数']:.1f} 銘柄分。"
            "この差が大きいほど、数だけ並んでいて実際は少数銘柄に偏っている状態です。"
        )
        L.append("")

    # --- リスク ---
    r = a.risk
    if r and "年率ボラ%" in r:
        L.append("## リスク")
        L.append("")
        L.append(
            "> **これは実際の運用成績ではありません。**"
            "「今のウェイトをこの期間ずっと保有していたら」という仮定計算です。"
            "実際には売買していたはずで、過去の自分のリターンとは一致しません。"
        )
        L.append("")
        L.append("| 指標 | 値 |")
        L.append("|---|---:|")
        L.append(f"| 対象期間 | {r['対象期間']}（{r['営業日数']} 営業日） |")
        L.append(f"| 評価額カバー率 | {r['カバー率%']:.1f}% |")
        L.append(f"| 年率ボラティリティ | {r['年率ボラ%']:.2f}% |")
        L.append(f"| 最大ドローダウン | {r['最大DD%']:.2f}% |")
        if "加重平均ボラ%" in r:
            L.append(f"| 加重平均ボラ（分散効果なし） | {r['加重平均ボラ%']:.2f}% |")
            L.append(f"| 分散効果 | {r['分散効果%']:.2f}% ボラ低減 |")
        if "ベータ" in r:
            L.append(f"| ベータ（対 {r['ベンチマーク']}） | {r['ベータ']:.3f} |")
            L.append(f"| 相関（対 {r['ベンチマーク']}） | {r['相関(対ベンチ)']:.3f} |")
        L.append("")

        L.append("### 銘柄別ボラティリティとウェイト")
        L.append("")
        L.append(_table(r["銘柄別"], (), ccy, anonymize))

        corr = r.get("相関")
        if isinstance(corr, pd.DataFrame) and not corr.empty:
            L.append("### 相関行列（基準通貨建て日次リターン）")
            L.append("")
            L.append(corr.round(2).to_markdown() + "\n")
            L.append(
                "> 相関が 0.8 を超えるペアは実質同じ資産です。"
                "分散しているつもりで同じリスクを重ねていないか確認してください。"
            )
            L.append("")

    # --- リバランス ---
    if not a.rebalance.empty:
        L.append("## 目標配分との乖離")
        L.append("")
        L.append(_table(a.rebalance, ("現在額", "目標額", "調整額"), ccy, anonymize))
        L.append("")
        if "状態" in a.rebalance.columns:
            L.append(
                "> **⚠ 評価不能の保有があるクラスは、乖離と調整額を出していません（—）。**"
                "総額も評価できた分だけなので、他のクラスの調整額も実態とずれます。"
                "価格を取り直すか price_override を入れてから判断してください。"
            )
        L.append("> 調整額は正が買い増し、負が売却。税と手数料は考慮していません。")
        L.append(
            "> 課税口座での売却には約 20.315% の税がかかるため、"
            "乖離の解消は「新規資金の配分先を変える」ほうが有利な場合が多いです。"
        )
        L.append("")

        if compliance is not None:
            buyable = sorted(
                s for s in {
                    v.position.symbol for v in a.valuations if not v.position.is_cash
                }
                if compliance.can_buy(s)
            )
            L.append(
                "> **⚠ この調整額は independence のゲートを通っていません。**"
                "資産クラス単位の計算であり、個別銘柄の購入可否は別途確認が必要です。"
            )
            if buyable:
                L.append(f"> 現時点で購入可と記録されているのは: {', '.join(buyable)}")
            else:
                L.append(
                    "> **現時点で購入可と記録されている銘柄はありません。**"
                    "DESC 確認と事前承認を済ませてから購入してください。"
                )
            L.append("")

    # --- Claude への引き継ぎ ---
    L.append("---")
    L.append("")
    L.append("## この先の判断について")
    L.append("")
    L.append("ここまでの数値はすべてローカルで計算済みです。以下は計算では出せないので判断が必要です。")
    L.append("")
    L.append("1. **集中度は許容範囲か** — 上位銘柄の比率と、その銘柄の固有リスクをどう見るか")
    L.append("2. **相関の高いペアを整理すべきか** — 分散しているつもりで重複していないか")
    L.append("3. **目標配分そのものが妥当か** — 乖離の解消より、目標の見直しが先の場合もある")
    L.append("4. **為替エクスポージャーは意図通りか** — 通貨別の比率を見て、円安前提に賭けていないか")
    L.append("5. **税制上の配置が最適か** — 成長期待の高い資産を NISA に、という配置になっているか")
    L.append("")
    L.append("**判断の前に必ず確認すること:** 上の「先に確認すべきこと」に挙がった警告。")
    L.append("評価不能なポジションがあると、すべての比率が実態からずれます。")
    L.append("")

    if compliance is not None:
        L.append(
            "**そして independence が先。** 購入可と記録されていない銘柄について"
            "配分を議論しても実行できません。DESC 確認と事前承認が済んでいない"
            "ものは、検討順序の最後に回してください。"
        )
        L.append("")

    return "\n".join(L)
