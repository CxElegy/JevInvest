"""コマンドラインの入口。

    python -m src.cli report                  # レポートを出力
    python -m src.cli report --anonymize      # 金額を隠して比率だけ
    python -m src.cli report --no-risk        # 履歴取得を省いて高速に
    python -m src.cli quotes                  # 価格だけ確認
    python -m src.cli validate                # 台帳の検証だけ
    python -m src.cli import-csv FILE.csv     # 証券会社の取引履歴 CSV を取り込む

出力した Markdown を Claude Code に読ませて判断させる想定。
このスクリプト自体は LLM を一切呼ばないので、API クレジットは不要。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOLDINGS = ROOT / "config" / "holdings.yaml"
DEFAULT_COMPLIANCE = ROOT / "config" / "compliance.yaml"
DEFAULT_TRANSACTIONS = ROOT / "config" / "transactions.yaml"
OUT_DIR = ROOT / "reports"


def _load(path: Path):
    from .holdings import HoldingsError, load

    try:
        return load(path)
    except HoldingsError as e:
        print(f"台帳の読み込みに失敗しました:\n  {e}", file=sys.stderr)
        raise SystemExit(1) from e


def cmd_validate(args) -> None:
    h = _load(args.holdings)
    print(f"基準通貨      : {h.base_currency}")
    print(f"ポジション数  : {len(h.positions)}")
    print(f"口座          : {', '.join(a.name for a in h.accounts) or '（未定義）'}")
    print(f"資産クラス    : {', '.join(h.asset_classes)}")
    print(f"通貨          : {', '.join(h.currencies)}")
    print(f"価格取得が必要: {len(h.symbols_needing_quote)} 銘柄")
    if h.warnings:
        print(f"\n警告 {len(h.warnings)} 件:")
        for w in h.warnings:
            print(f"  - {w}")
    else:
        print("\n警告なし。")


def cmd_compliance(args) -> None:
    """independence の確認状況を一覧する。次に何を申請すべきかを出す。"""
    from .compliance import load as load_compliance
    from .compliance import summarize

    h = _load(args.holdings)
    reg = load_compliance(args.compliance)
    symbols = h.symbols_needing_compliance   # 現金は対象外

    s = summarize(reg, symbols)

    print(f"確認記録: {args.compliance}")
    print(f"再確認しきい値: {reg.recheck_after_days} 日")
    print(f"対象銘柄: {len(symbols)} 件")
    print()

    if reg.warnings:
        print("記録の不備:")
        for w in dict.fromkeys(reg.warnings):
            print(f"  ⚠ {w}")
        print()

    order = ["blocked", "escalate", "unchecked", "stale", "pending", "ok"]
    labels = {
        "blocked": "🚨 不可（保有・購入いずれも）",
        "escalate": "❓ 照会が必要",
        "unchecked": "◻ DESC 未確認",
        "stale": "🕑 再確認が必要",
        "pending": "⏳ 事前承認 未申請／申請中",
        "ok": "✅ 購入可",
    }
    for state in order:
        items = s["buckets"].get(state)
        if not items:
            continue
        print(f"{labels[state]}  ({len(items)} 件)")
        for sym, v in items:
            print(f"    {sym:<22} {v.reason}")
        print()

    # 「照会が必要」と「自分で進められる」を分ける。混ぜると優先順位が見えない。
    escalate = [sym for sym, _ in s["buckets"].get("blocked", [])]
    escalate += [sym for sym, _ in s["buckets"].get("escalate", [])]
    to_check = [sym for sym, _ in s["buckets"].get("unchecked", [])]
    to_check += [sym for sym, _ in s["buckets"].get("stale", [])]
    to_submit = [sym for sym, _ in s["buckets"].get("pending", [])]

    if not (escalate or to_check or to_submit):
        print("── 対応が必要な銘柄はありません ──")
        return

    print("── 次にやること ──")
    n = 0
    if escalate:
        n += 1
        print(f"  {n}. Independence & Conflicts Network へ照会: {', '.join(escalate)}")
        print("     保有中で制限対象だった場合、売却が必要になります（損失は自己負担）")
    if to_check:
        n += 1
        print(f"  {n}. DESC で確認: {', '.join(to_check)}")
    if to_submit:
        n += 1
        print(f"  {n}. 事前承認を申請／承認待ち: {', '.join(to_submit)}")


def cmd_dashboard(args) -> None:
    """HTML ダッシュボードを生成する。単一ファイルで完結する。"""
    import yaml

    from .analytics import analyze
    from .compliance import load as load_compliance
    from .dashboard import to_html
    from .marketdata import MarketData
    from .plan import build as build_plan

    h = _load(args.holdings)
    cfg = yaml.safe_load(args.holdings.read_text(encoding="utf-8"))
    reg = load_compliance(args.compliance)

    from .compliance import cleared_by_class

    cleared = cleared_by_class(reg, h.positions)
    plan = build_plan(cfg, compliance=reg, cleared_symbols_by_class=cleared)

    md = MarketData()
    try:
        a = analyze(h, md, years=args.years, benchmark=args.benchmark,
                    with_risk=not args.no_risk)
        html = to_html(a, plan=plan, anonymize=args.anonymize)
    finally:
        md.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "-anon" if args.anonymize else ""
    out = OUT_DIR / f"dashboard{suffix}.html"
    out.write_text(html, encoding="utf-8")
    print(f"ダッシュボードを出力しました: {out}")
    print(f"  評価額 {a.total_value_base:,.0f} {a.base_currency}")
    if a.guards:
        v = sum(1 for g in a.guards if g.severity == "violation")
        print(f"  ガードレール違反 {v} 件 / 注意 {len(a.guards) - v} 件")
    print("  ブラウザで開いてください（単一ファイルで完結します）")


def cmd_transactions(args) -> None:
    """取引履歴から保有状態と実績リターンを出す。"""
    from datetime import date  # noqa: F401 — 下の XIRR ガードで使う

    from .analytics import analyze
    from .marketdata import MarketData
    from .transactions import cash_flows, load as load_tx, xirr

    h = _load(args.holdings)
    led = load_tx(args.transactions, base_currency=h.base_currency)

    if not led.transactions:
        print(f"取引履歴がありません: {args.transactions}")
        print("config/transactions.example.yaml をコピーして記録を始めてください。")
        for w in led.warnings:
            print(f"  ⚠ {w}")
        return

    print(f"取引 {len(led.transactions)} 件"
          f"（{led.transactions[0].date} 〜 {led.transactions[-1].date}）")
    print()

    if led.warnings:
        print("── 警告 ──")
        for w in dict.fromkeys(led.warnings):
            print(f"  ⚠ {w}")
        print()

    open_lots = led.open_lots
    if open_lots:
        print("── 取引履歴から導出した保有（総平均法）──")
        rows = []
        for l in sorted(open_lots, key=lambda x: -x.cost_base):
            rows.append(
                {
                    "銘柄": l.symbol, "口座": l.account,
                    "資産クラス": l.asset_class,
                    "数量": f"{l.quantity:,.4f}".rstrip("0").rstrip("."),
                    "平均単価": f"{l.avg_price_native:,.4f}",
                    "通貨": l.currency,
                    "平均為替": f"{l.avg_fx:,.2f}",
                    "取得原価": f"{l.cost_base:,.0f}",
                }
            )
        print(pd_frame(rows).to_markdown(index=False))
        print()
        print("  ↑ これを config/holdings.yaml に反映すれば手入力が不要になります")
        print("     （--write-derived で別ファイルに書き出せます）")
        print()

    print("── 実現分 ──")
    print(f"  実現損益 : {led.realized_total:+,.0f} {h.base_currency}")
    print(f"  配当受取 : {led.dividends_total:+,.0f} {h.base_currency}")
    print(f"  手数料   : {led.fees_total:,.0f} {h.base_currency}")
    print()

    # 現在の評価額は台帳側から取る（取引履歴には時価が無い）
    current = 0.0
    if not args.no_quotes:
        md = MarketData()
        try:
            a = analyze(h, md, with_risk=False)
            current = a.total_value_base
            securities_value = sum(
                v.market_value_base for v in a.valuations
                if v.priced and not v.position.is_cash
            )
        finally:
            md.close()
        print("── 実績リターン（XIRR = 金額加重）──")
        # 入金・出金の記録があれば現金込みの評価額、無ければ売買を出入りとして有価証券のみ
        if led.flow_mode == "external":
            terminal = current
            print(f"  現在の評価額: {current:,.0f} {h.base_currency}"
                  f"（台帳 {args.holdings.name} より・現金込み）")
        else:
            terminal = securities_value
            print(f"  現在の評価額: {securities_value:,.0f} {h.base_currency}"
                  f"（台帳 {args.holdings.name} より・現金を除く有価証券。"
                  f"入出金の記録が無いため売買を出入りとして計算）")

        # XIRR を出す前に、その数字が意味を持つかを確認する。
        # 台帳と取引履歴が対応していないと、無関係な評価額で年率が出てしまう。
        blockers: list[str] = []

        last_tx = max(t.date for t in led.transactions)
        as_of = date.today()
        if last_tx > as_of:
            blockers.append(
                f"最後の取引 {last_tx} が現在日 {as_of} より後です。"
                "未来日付の取引を直してください"
            )

        tx_symbols = {l.symbol for l in open_lots}
        ledger_symbols = {p.symbol for p in h.positions if not p.is_cash}
        missing = tx_symbols - ledger_symbols
        if missing:
            blockers.append(
                f"取引履歴にある {', '.join(sorted(missing))} が台帳に無い。"
                "評価額が取引履歴と対応しないため XIRR は意味を持ちません"
                "（--write-derived で導出結果を出して台帳に反映してください）"
            )

        # 売買ベースでは、台帳にあって取引履歴に買いの無い銘柄の評価額は
        # 「タダで手に入れた資産」として年率を押し上げる
        untracked = ledger_symbols - tx_symbols
        if led.flow_mode == "trade" and untracked:
            blockers.append(
                f"台帳にある {', '.join(sorted(untracked))} が取引履歴（保有中）に無い。"
                "買いのキャッシュフローが無いまま評価額だけが乗るため XIRR が過大になります"
            )

        # 評価不能の銘柄は評価額から抜けるので、黙って計算すると XIRR が過小になる
        if a.unpriced_count:
            unpriced = sorted({v.position.symbol for v in a.valuations if not v.priced})
            blockers.append(
                f"{', '.join(unpriced)} が評価不能（価格または為替が取れない）。"
                "評価額から抜けるため XIRR は実際より低く出ます"
                "（取れない原因を解消するか price_override を入れてください）"
            )

        if blockers:
            print("  ⚠ 計算しません:")
            for b in blockers:
                print(f"     - {b}")
        else:
            flows = cash_flows(led, terminal)
            r = xirr(flows)
            if r is None:
                print("  キャッシュフローの符号が揃っておらず計算できません")
            else:
                days = (flows[-1][0] - flows[0][0]).days
                print(f"  XIRR        : {r * 100:+.2f}% / 年")
                print(f"  対象期間    : {flows[0][0]} 〜 {flows[-1][0]}（{days} 日）")
                if days < 180:
                    print("  ⚠ 期間が短いため年率換算は大きく振れます。"
                          "参考値として扱ってください")
            print()
            print("  ※ XIRR は「自分がいつ何を入れたか」を反映する金額加重リターン。")
            print("     市場のタイミングの影響が入るので、時間加重リターン（TWR）とは")
            print("     意味が違います。ファンドの公表リターンと直接比較はできません。")

    if args.write_derived:
        out = ROOT / "config" / "holdings.derived.yaml"
        _write_derived(out, h, open_lots)
        print()
        print(f"導出した保有を書き出しました: {out}")
        print("  内容を確認して config/holdings.yaml の positions に反映してください")


def pd_frame(rows):
    import pandas as pd

    return pd.DataFrame(rows)


def _write_derived(path: Path, h, lots) -> None:
    """取引履歴から導出した positions を YAML で書き出す。

    holdings.yaml を直接書き換えないのは、コメントや目標配分を
    壊さないため。中身を見てから手で反映する。
    """
    lines = [
        "# 取引履歴から自動導出した保有。",
        "# config/holdings.yaml の positions に反映するための参考出力。",
        "# このファイルは毎回上書きされる。",
        "",
        "positions:",
    ]
    for l in sorted(lots, key=lambda x: -x.cost_base):
        lines.append(f"  - symbol: {l.symbol}")
        lines.append(f"    name: {l.name}")
        lines.append(f"    asset_class: {l.asset_class}")
        lines.append(f"    account: {l.account}")
        lines.append(f"    quantity: {l.quantity:.6f}".rstrip("0").rstrip("."))
        lines.append(f"    cost_basis_per_unit: {l.avg_price_native:.6f}")
        lines.append(f"    currency: {l.currency}")
        if l.currency != h.base_currency:
            lines.append(f"    cost_fx_rate: {l.avg_fx:.4f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_check_trade(args) -> None:
    """売買の前の点検。やめる理由を先に全部出す。発注はしない。"""
    import yaml

    from .analytics import analyze
    from .compliance import load as load_compliance
    from .marketdata import MarketData
    from .pretrade import MARK, Intent, run_checks, verdict

    h = _load(args.holdings)
    cfg = yaml.safe_load(args.holdings.read_text(encoding="utf-8")) or {}
    reg = load_compliance(args.compliance)

    md = MarketData()
    try:
        a = analyze(h, md, with_risk=False)
    finally:
        md.close()

    if args.amount is None and args.pct is None:
        raise SystemExit("--amount（金額）か --pct（全体に対する %）のどちらかを指定してください")
    amount = args.amount if args.amount is not None else a.total_value_base * args.pct / 100

    held = [p for p in h.positions if p.symbol == args.symbol]
    asset_class = args.asset_class or (held[0].asset_class if held else None)
    if not asset_class:
        raise SystemExit(f"{args.symbol} は台帳に無いので --asset-class を指定してください")
    account = args.account or (held[0].account if held else None)
    if not account:
        raise SystemExit("--account を指定してください")

    tx = []
    if args.transactions.exists():
        tx = (yaml.safe_load(args.transactions.read_text(encoding="utf-8")) or {}).get("transactions") or []

    intent = Intent(args.side, args.symbol, float(amount), asset_class, account)
    checks = run_checks(intent, a, h, cfg, reg, tx, ROOT / "config" / "trade_intents.yaml")

    print(f"点検: {args.side} {args.symbol}（{asset_class}・{account}・"
          + (f"全体の {args.pct:g}%" if args.pct is not None else "指定額") + "）")
    print()
    for c in checks:
        print(f"  {MARK[c.level]:<8} {c.title}: {c.detail}")
    print()
    print(f"判定: {verdict(checks)}")
    print("（このツールは発注しない。independence の判定もしない）")


def cmd_scoreboard(args) -> None:
    """遊び枠（個別株）の成績表。同じ日に同じ額を基準に入れていたら、と比べる。"""
    from datetime import date

    import yaml

    from .analytics import analyze
    from .marketdata import MarketData
    from .scoreboard import shadow
    from .transactions import load as load_tx

    h = _load(args.holdings)
    cfg = yaml.safe_load(args.holdings.read_text(encoding="utf-8")) or {}
    classes = set(args.classes or cfg.get("requires_stock_clearance") or [])
    if not classes:
        raise SystemExit("対象の資産クラスがありません（--classes か requires_stock_clearance を設定）")

    led = load_tx(args.transactions, base_currency=h.base_currency)
    symbols = {t.symbol for t in led.transactions if t.type == "buy" and t.asset_class in classes}
    if not symbols:
        print(f"{', '.join(sorted(classes))} の買付が取引履歴にありません: {args.transactions}")
        return

    first = min(t.date for t in led.transactions if t.symbol in symbols)
    years = (date.today() - first).days / 365.25 + 0.1
    md = MarketData()
    try:
        a = analyze(h, md, with_risk=False)
        fx_sym = f"USD{h.base_currency}=X"
        hist = md.get_history([args.benchmark, fx_sym], years=years)
        errors = list(md.fetch_errors)
    finally:
        md.close()

    unpriced = [v.position.symbol for v in a.valuations
                if v.position.symbol in symbols and not v.priced]
    # 取引履歴では保有が残っているのに台帳に無い銘柄は、評価額 0 として数えてしまう
    ledger_syms = {p.symbol for p in h.positions}
    not_in_ledger = sorted({l.symbol for l in led.open_lots if l.symbol in symbols} - ledger_syms)
    if unpriced or not_in_ledger or args.benchmark not in hist or fx_sym not in hist:
        print("⚠ 比較できません（黙って 0 円扱いにしない）:")
        if unpriced:
            print(f"   評価不能: {', '.join(unpriced)}")
        if not_in_ledger:
            print(f"   取引履歴では保有中なのに台帳に無い: {', '.join(not_in_ledger)}"
                  "（transactions --write-derived で台帳に反映する）")
        for e in dict.fromkeys(errors):
            print(f"   - {e}")
        raise SystemExit(1)

    import pandas as pd

    fx = hist[fx_sym]["close"]
    bench = hist[args.benchmark]["adj_close"]
    if not args.benchmark.endswith(".T"):
        bench = (bench * fx.reindex(bench.index, method="ffill")).dropna()
    current = sum(v.market_value_base for v in a.valuations
                  if v.position.symbol in symbols and v.priced)
    r = shadow(led.transactions, symbols, pd.Series(bench), current, args.benchmark)

    print(f"遊び枠の成績表（対象: {', '.join(sorted(classes))} / 基準: {args.benchmark}）")
    print(f"  銘柄: {', '.join(sorted(symbols))}　期間: {first} 〜 今日")
    print()
    print(f"  自分の選択: 今の評価額 + 受取配当 = {r.actual_value:,.0f}")
    print(f"  同じ日に同じ額を {args.benchmark} に入れていたら = {r.shadow_value:,.0f}")
    print(f"  差: {r.edge:+,.0f}" + ("（基準に勝っている）" if r.edge > 0 else "（基準に負けている）"))
    print(f"  入れた額 {r.invested:,.0f} / 売却で受け取った額 {r.withdrawn:,.0f}")
    for w in r.warnings:
        print(f"  ⚠ {w}")
    print()
    print("  ※ 数年分の差でも運の影響が大きい。docs/PLAYBOOK.md の遊び枠のルール"
          "（3 年続けて負けたら本体に合流）と合わせて読む")


def cmd_import_csv(args) -> None:
    """証券会社の取引履歴 CSV を transactions.yaml の書式に変換する。

    金額は画面に出さない（件数と銘柄だけ）。Claude Code に金額を渡さずに済むように。
    """
    from .importers import convert, decode, drop_existing, dump, load_map

    mapping = load_map(args.map)
    collected: list[dict] = []
    failed = False
    for f in args.files:
        print(f"── {f.name} ──")
        try:
            text = decode(f.read_bytes())
        except (OSError, ValueError) as e:
            print(f"  ⚠ 読めません: {e}")
            failed = True
            continue
        res = convert(text, preset_key=args.broker, mapping=mapping)
        if res.preset:
            print(f"  書式: {res.preset.label}（{res.preset.key}）")
        buys = sum(1 for t in res.transactions if t["type"] == "buy")
        sells = len(res.transactions) - buys
        print(f"  取り込み: {len(res.transactions)} 件（買付 {buys} / 売付 {sells}）")
        for w in res.warnings:
            print(f"  ⚠ {w}")
        if res.preset is None:
            failed = True
        collected.extend(res.transactions)

    if not collected:
        print("\n取り込める取引がありませんでした。")
        raise SystemExit(1 if failed else 0)

    new, dropped = drop_existing(collected, args.transactions)
    print()
    if dropped:
        print(f"{args.transactions.name} に既にある {dropped} 件は除きました（二重計上の防止）")
    if not new:
        print("新しい取引はありません。")
        return

    out = args.out
    out.write_text(dump(new), encoding="utf-8")
    symbols = sorted({t["symbol"] for t in new})
    print(f"新しい取引 {len(new)} 件を書き出しました: {out}")
    print(f"  期間: {min(t['date'] for t in new)} 〜 {max(t['date'] for t in new)}")
    print(f"  銘柄: {', '.join(symbols)}")
    print()
    print("次にやること:")
    print(f"  1. {out.name} の asset_class と account を確認する（警告に出た銘柄は特に）")
    print(f"  2. 中身を {args.transactions.name} の transactions に追記する")
    print("  3. python -m src.cli transactions --no-quotes --write-derived で保有を導出する")
    print("  ※ 入金・配当は CSV に含まれないので、XIRR を正しく出すには deposit を手で足す")


def cmd_plan(args) -> None:
    """積立計画を出す。NISA 枠の割り当てと independence のゲートを含む。"""
    import yaml

    from .compliance import load as load_compliance
    from .plan import annual_projection, build

    cfg = yaml.safe_load(args.holdings.read_text(encoding="utf-8"))
    h = _load(args.holdings)
    reg = load_compliance(args.compliance)

    # 資産クラスごとに「事前承認が取れている銘柄」を集める。保有中の銘柄に加えて、
    # compliance.yaml に asset_class を書いた購入可の候補も数える（最初の 1 本を買う前用）。
    from .compliance import cleared_by_class

    cleared = cleared_by_class(reg, h.positions)

    plan = build(cfg, compliance=reg, cleared_symbols_by_class=cleared)

    print(f"毎月の投入額: {plan.monthly:,.0f} 円")
    if plan.months_left < 12:
        print(f"（今年の残り {plan.months_left} か月の割り当て。NISA の年枠の残りを"
              f"残り月数で割っている。来年以降の割り当ては下の累計表に反映）")
    print()
    df = plan.to_frame()
    for c in ("月額", "つみたて枠", "成長枠", "課税口座", "留保(現金)"):
        df[c] = df[c].map(lambda v: f"{v:,.0f}")
    df["目標%"] = df["目標%"].map(lambda v: f"{v:.1f}")
    print(df.to_markdown())
    print()
    print(f"  実際に投資される額 : {plan.invested:,.0f} 円")
    print(f"    うち つみたて枠   : {plan.tsumitate_total:,.0f} 円")
    print(f"    うち 成長枠       : {plan.growth_total:,.0f} 円")
    print(f"    うち 課税口座     : {plan.taxable_total:,.0f} 円")
    print(f"  現金で留保される額 : {plan.parked:,.0f} 円")
    print()

    holds = [c for c in plan.classes if c.status == "保留"]
    if holds:
        print("── 承認待ちで積立を保留しているクラス ──")
        for c in holds:
            print(f"  {c.asset_class:<20} 月 {c.monthly_amount:,.0f} 円")
            print(f"    {c.note}")
        print()

    if plan.warnings:
        print("── 注意 ──")
        for w in dict.fromkeys(plan.warnings):
            print(f"  ⚠ {w}")
        print()

    print(f"── 投入額の累計と NISA 枠の消化（{args.years} 年） ──")
    print("※ リターンは一切仮定していない。「いくら入れたか」の表。")
    print()
    proj = annual_projection(cfg, plan, years=args.years)
    print(proj.map(lambda v: f"{v:,.0f}").to_markdown())
    for n in proj.attrs.get("notes", []):
        print(f"  ※ {n}")


def cmd_quotes(args) -> None:
    from .marketdata import MarketData

    h = _load(args.holdings)
    md = MarketData()
    try:
        quotes = md.get_quotes(h.symbols_needing_quote, force=args.force)
        fx = md.get_fx(h.currencies, h.base_currency)

        print("価格:")
        for sym in h.symbols_needing_quote:
            v = quotes.get(sym)
            print(f"  {sym:<24} {v:>12,.4f}" if v else f"  {sym:<24} {'取得失敗':>12}")

        print("\n為替:")
        for ccy, rate in sorted(fx.items()):
            print(f"  {ccy}/{h.base_currency:<6} {rate:>12,.4f}")

        if md.fetch_errors:
            print(f"\n警告 {len(md.fetch_errors)} 件:")
            for e in dict.fromkeys(md.fetch_errors):
                print(f"  - {e}")
    finally:
        md.close()


def cmd_report(args) -> None:
    from .analytics import analyze
    from .compliance import load as load_compliance
    from .marketdata import MarketData
    from .report import to_markdown

    h = _load(args.holdings)
    # independence のゲートは既定で有効。--no-compliance で外せるが、
    # 外すと「買えないものを買え」と書かれたレポートが出る点に注意。
    reg = None if args.no_compliance else load_compliance(args.compliance)

    md = MarketData()
    try:
        a = analyze(
            h, md,
            years=args.years,
            benchmark=args.benchmark,
            with_risk=not args.no_risk,
        )
        text = to_markdown(a, anonymize=args.anonymize, compliance=reg)
    finally:
        md.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "-anon" if args.anonymize else ""
    out = OUT_DIR / f"holdings-{datetime.now():%Y%m%d-%H%M}{suffix}.md"
    out.write_text(text, encoding="utf-8")

    latest = OUT_DIR / f"latest{suffix}.md"
    latest.write_text(text, encoding="utf-8")

    if args.stdout:
        print(text)
    else:
        print(f"レポートを出力しました:\n  {out}\n  {latest}")
        print(f"\n評価額 {a.total_value_base:,.0f} {a.base_currency}"
              f" / 損益 {a.total_pnl_base:+,.0f} ({a.total_pnl_pct:+.2f}%)")
        if a.unpriced_count:
            print(f"⚠ 評価不能 {a.unpriced_count} 件あり。レポート冒頭を確認してください。")
        print(f"\nClaude Code で判断させるには:\n  reports/latest{suffix}.md を読んで分析して")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="持仓の分析・管理ツール（計算はローカル、判断は Claude Code）",
    )
    ap.add_argument(
        "--holdings", type=Path, default=DEFAULT_HOLDINGS,
        help=f"台帳のパス（既定: {DEFAULT_HOLDINGS.relative_to(ROOT)}）",
    )
    ap.add_argument(
        "--compliance", type=Path, default=DEFAULT_COMPLIANCE,
        help=f"independence 確認記録のパス（既定: {DEFAULT_COMPLIANCE.relative_to(ROOT)}）",
    )
    ap.add_argument(
        "--transactions", type=Path, default=DEFAULT_TRANSACTIONS,
        help=f"取引履歴のパス（既定: {DEFAULT_TRANSACTIONS.relative_to(ROOT)}）",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate", help="台帳の検証だけ行う")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("compliance", help="independence の確認状況を一覧する")
    p.set_defaults(func=cmd_compliance)

    p = sub.add_parser("plan", help="積立計画と NISA 枠の割り当てを出す")
    p.add_argument("--years", type=int, default=5, help="累計を出す年数（既定 5）")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("dashboard", help="HTML ダッシュボードを生成する")
    p.add_argument("--anonymize", action="store_true", help="絶対額を出さず比率だけにする")
    p.add_argument("--no-risk", action="store_true", help="履歴取得とリスク計算を省略する")
    p.add_argument("--years", type=float, default=3.0)
    p.add_argument("--benchmark", default="VT")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("transactions", help="取引履歴から保有と実績リターンを出す")
    p.add_argument("--no-quotes", action="store_true", help="時価取得を省く（XIRR は出ない）")
    p.add_argument("--write-derived", action="store_true",
                   help="導出した保有を config/holdings.derived.yaml に書き出す")
    p.set_defaults(func=cmd_transactions)

    p = sub.add_parser("check-trade", help="売買の前に、やめる理由がないか点検する")
    p.add_argument("side", choices=["buy", "sell"])
    p.add_argument("symbol")
    p.add_argument("--amount", type=float, help="金額（基準通貨）")
    p.add_argument("--pct", type=float, help="全体の評価額に対する %（金額を出したくないとき）")
    p.add_argument("--asset-class", help="資産クラス（台帳にある銘柄なら省略可）")
    p.add_argument("--account", help="口座名（台帳にある銘柄なら省略可）")
    p.set_defaults(func=cmd_check_trade)

    p = sub.add_parser("scoreboard", help="遊び枠（個別株）を、同じ日に同じ額を基準に入れた場合と比べる")
    p.add_argument("--classes", nargs="+", help="対象の資産クラス（既定: requires_stock_clearance）")
    p.add_argument("--benchmark", default="VT", help="比べる基準（既定 VT）")
    p.set_defaults(func=cmd_scoreboard)

    p = sub.add_parser("import-csv", help="証券会社の取引履歴 CSV を取り込む")
    p.add_argument("files", type=Path, nargs="+", help="CSV ファイル（複数可）")
    p.add_argument("--broker", default=None,
                   help="書式を明示する: sbi_jp / rakuten_jp / rakuten_us / rakuten_fund"
                        "（省略時は列名で自動判定）")
    p.add_argument("--map", type=Path, default=ROOT / "config" / "import_map.yaml",
                   help="銘柄・投信・口座の対応表（既定: config/import_map.yaml）")
    p.add_argument("--out", type=Path, default=ROOT / "config" / "transactions.imported.yaml",
                   help="出力先（既定: config/transactions.imported.yaml）")
    p.set_defaults(func=cmd_import_csv)

    p = sub.add_parser("quotes", help="価格と為替の取得を確認する")
    p.add_argument("--force", action="store_true", help="キャッシュを無視して取り直す")
    p.set_defaults(func=cmd_quotes)

    p = sub.add_parser("report", help="Markdown レポートを生成する")
    p.add_argument("--anonymize", action="store_true", help="絶対額を出さず比率だけにする")
    p.add_argument("--no-risk", action="store_true", help="履歴取得とリスク計算を省略する")
    p.add_argument(
        "--no-compliance", action="store_true",
        help="independence のゲートを外す（非推奨）",
    )
    p.add_argument("--years", type=float, default=3.0, help="リスク計算の対象年数（既定 3）")
    p.add_argument("--benchmark", default="VT", help="ベータ計算のベンチマーク（既定 VT）")
    p.add_argument("--stdout", action="store_true", help="レポート本文を標準出力にも出す")
    p.set_defaults(func=cmd_report)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
