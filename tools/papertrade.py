"""紙上の売買（ペーパートレード）とバックテスト。実際の発注はしない。

仮想の 500 万円を戦略ごとに 1 口座ずつ持たせ、Yahoo Finance の実際の終値で約定させる。
エンジンと決まりは src/sim.py（ライブとバックテストで同じコード）。

    python tools/papertrade.py backtest                 # 2009-07〜今日。paper/BACKTEST.md と web/papertrade.json
    python tools/papertrade.py init --decide-on 2026-10-09   # 口座を作り、その日の終値までで最初の判断
    python tools/papertrade.py run                      # 新しい営業日を約定・評価し paper/REPORT.md を更新
    python tools/papertrade.py report                   # 取得なしでレポートだけ作り直す

状態は paper/state.json。仮想のお金なので個人データではなく、コミットして記録を残す
（約定の日付・価格・為替を全件残すので、あとから実際の値と照合できる）。

Claude の裁量口座は paper/claude.yaml に判断（日付・目標ウェイト・理由）を追記すると、
次の run でその日より後の最初の終値で約定する。判断日より前の価格では約定しない。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.sim import (  # noqa: E402
    ALL_STRATEGIES, FX_SYMBOL, STRATEGY_LABELS, call_strategy, is_due, TAX_RATE, UNIVERSE, Book, Costs, Prices,
    backtest, is_jpy, is_rebalance_day, window_returns,
)

PAPER = ROOT / "paper"
STATE = PAPER / "state.json"
CLAUDE = PAPER / "claude.yaml"
REPORT = PAPER / "REPORT.md"
BT_MD = PAPER / "BACKTEST.md"
BT_JSON = ROOT / "web" / "papertrade.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ------------------------------------------------------------------ データ

def fetch_bars(years: float, fresh: bool = False):
    """実際の日足を取る。取れなかった銘柄は警告して止める（黙って欠けたまま進めない）。

    fresh=True はキャッシュを使わない。ライブでは、取引時間中に取った途中の足が
    キャッシュに残っていると、それを終値として約定させてしまうため。
    """
    import tempfile

    from src.marketdata import MarketData

    tmp = tempfile.TemporaryDirectory() if fresh else None
    md = MarketData(Path(tmp.name) / "fresh.sqlite") if tmp else MarketData()
    try:
        hist = md.get_history(UNIVERSE + [FX_SYMBOL], years=years)
        errors = list(md.fetch_errors)
    finally:
        md.close()
        if tmp:
            tmp.cleanup()
    missing = [s for s in UNIVERSE + [FX_SYMBOL] if s not in hist or hist[s].empty]
    if missing:
        raise SystemExit(
            f"価格を取得できない銘柄があります: {', '.join(missing)}\n"
            + "\n".join(f"  - {e}" for e in errors)
            + "\nレート制限かネットワーク制限の可能性があります。時間をおいて再実行してください"
        )
    for e in errors:
        print(f"  ⚠ {e}")
    return hist


def fetch_dividends(symbols: list[str]) -> dict[str, pd.Series]:
    import yfinance as yf

    out = {}
    for s in symbols:
        try:
            d = yf.Ticker(s).dividends
        except Exception as e:  # noqa: BLE001
            # 分配金を落としたまま日付だけ進めると、後から取り戻せない。だから止める
            raise SystemExit(f"{s}: 分配金の履歴を取得できません（{type(e).__name__}: {e}）。"
                             "日付を進めずに止めます。時間をおいて再実行してください")
        if d is None or d.empty:
            continue
        idx = pd.to_datetime(d.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        out[s] = pd.Series(d.values, index=idx.normalize())
    return out


def final_cutoff(now: pd.Timestamp | None = None) -> pd.Timestamp:
    """終値が確定したとみなせる最後の日。米国の引けは UTC 20:00〜21:00 なので、
    その日の UTC 21:30 を過ぎていれば確定（東証は UTC 06:30 に引けている）。"""
    now = now if now is not None else pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)
    return (now - pd.Timedelta(hours=21, minutes=30)).normalize()


def live_prices(years: float = 1.7) -> Prices:
    """確定した足だけを残した価格。全銘柄と為替の足が揃った日までに切る。"""
    hist = fetch_bars(years, fresh=True)
    close = {s: hist[s]["close"] for s in UNIVERSE}
    adj = {s: hist[s]["adj_close"] for s in UNIVERSE}
    return Prices(close, hist[FX_SYMBOL]["close"], dividends=fetch_dividends(UNIVERSE), signals=adj)


def settled(prices: Prices) -> Prices:
    """確定済みで、全銘柄と為替の足が揃った日までに切る。

    どれかの足が遅れている日を処理すると、その銘柄は前日の値で評価・判断され、
    本当の終値が後から使われなくなる。だから揃うまで待つ（休場なら 1 日遅れるだけ）。
    """
    last = prices.common_last(UNIVERSE)
    if last is None:
        raise SystemExit("価格の足が揃っていない銘柄があります。時間をおいて再実行してください")
    return prices.truncate(min(final_cutoff(), last))


# ------------------------------------------------------------------ 状態

def load_state() -> dict:
    if not STATE.exists():
        raise SystemExit("paper/state.json がありません。先に init を実行してください")
    st = json.loads(STATE.read_text(encoding="utf-8"))
    st["books"] = {n: Book.from_dict(b) for n, b in st["books"].items()}
    return st


def save_state(st: dict) -> None:
    out = dict(st)
    out["books"] = {n: b.to_dict() for n, b in st["books"].items()}
    out["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STATE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def claude_decisions() -> list[dict]:
    if not CLAUDE.exists():
        return []
    cfg = yaml.safe_load(CLAUDE.read_text(encoding="utf-8")) or {}
    out = []
    for d in cfg.get("decisions") or []:
        w = {str(k): float(v) for k, v in (d.get("weights") or {}).items()}
        bad = [s for s in w if s not in UNIVERSE]
        if bad:
            raise SystemExit(f"paper/claude.yaml: 対象外の銘柄 {bad}（使えるのは {UNIVERSE}）")
        if sum(w.values()) > 1.0 + 1e-9 or any(v < 0 for v in w.values()):
            raise SystemExit(f"paper/claude.yaml {d.get('date')}: ウェイトは 0 以上・合計 1 以下")
        out.append({"date": str(d["date"]), "weights": w,
                    "rationale": str(d.get("rationale", "")).strip(),
                    "summary_zh": str(d.get("summary_zh", "")).strip()})
    return sorted(out, key=lambda d: d["date"])


# ------------------------------------------------------------------ コマンド

def cmd_init(args) -> None:
    if STATE.exists() and not args.force:
        raise SystemExit("paper/state.json が既にあります（やり直すなら --force）")
    prices = settled(live_prices())
    d0 = pd.Timestamp(args.decide_on)
    if d0 > prices.fx.index[-1]:
        raise SystemExit(f"{d0.date()} の終値はまだ確定していません（確定済みは {prices.fx.index[-1].date()} まで）")
    books = {n: Book(name=n, cash=args.capital) for n in [*ALL_STRATEGIES, "claude"]}
    for name, (fn, _) in ALL_STRATEGIES.items():
        target = call_strategy(fn, prices.upto(d0), d0, d0)
        if target is not None:
            books[name].decide(prices, d0, target, reason=name)
    for b in books.values():
        b.last_day = d0.date().isoformat()
        b.nav.append([b.last_day, float(args.capital)])
    st = {"capital": args.capital, "start": d0.date().isoformat(),
          "end": args.end, "costs": Costs().__dict__, "books": books}
    _apply_claude(st, prices, upto=None)
    save_state(st)
    print(f"口座を作りました（{len(books)} 口座 × {args.capital:,.0f} 円）。判断日 {d0.date()}")
    print("約定は判断日より後の最初の終値。python tools/papertrade.py run で進めます")


def _apply_claude(st: dict, prices: Prices, upto: pd.Timestamp | None) -> None:
    """Claude の判断のうち、まだ反映していないものを反映する。

    判断日 D の注文は D より後の最初の終値で約定する。D が最後に処理した日より前なら
    （判断を書くのが遅れた）、最後に処理した日の判断として扱い、そのことを記録に残す。
    """
    b: Book = st["books"]["claude"]
    for d in claude_decisions():
        if b.last_decision and d["date"] <= b.last_decision:
            continue
        when = pd.Timestamp(d["date"])
        if upto is not None and when >= upto:
            continue
        last = pd.Timestamp(b.last_day)
        if when < last:
            b.notes.append(f"判断 {d['date']} は処理済みの {b.last_day} より前のため、{b.last_day} の判断として扱いました")
            when = last
        b.decide(prices, when, d["weights"], reason=f"claude {d['date']}")
        b.last_decision = d["date"]


def cmd_run(args) -> None:
    st = load_state()
    prices = settled(live_prices())
    costs = Costs(**st["costs"])
    books: dict[str, Book] = st["books"]
    last = pd.Timestamp(min(b.last_day for b in books.values()))
    # live_prices が確定済みで全銘柄が揃った日までに切ってある。
    # 東証だけ・米国だけの休場日は、片方の足が無いまま進む（約定は次の足に持ち越す）
    days = [d for d in prices.calendar() if last < d]
    if not days:
        print(f"新しい営業日がありません（最後に処理した日 {last.date()}）")
    for day in days:
        _apply_claude(st, prices, upto=day)
        prev = pd.Timestamp(books["allcountry"].last_day)
        for name, b in books.items():
            if pd.Timestamp(b.last_day) >= day:
                continue
            b.process_day(prices, day, costs)
            if name in ALL_STRATEGIES:
                fn, freq = ALL_STRATEGIES[name]
                due = is_due(freq, day, prev, b.last_decision is not None)
                # 買って持つだけの口座も、受け取った分配金などの現金が残っていたら月初に買い足す
                # （バックテストは配当込み価格で再投資扱いなので、それと揃える）。売りは出さない
                idle = (freq == "once" and is_rebalance_day(day, prev)
                        and b.cash > 0.005 * b.value(prices, day))
                if due or idle:
                    target = call_strategy(fn, prices.upto(day), day, pd.Timestamp(st["start"]))
                    if target is not None:
                        b.decide(prices, day, target, reason=name)
                        if not due:
                            b.pending = [o for o in b.pending if o["side"] == "buy"]
    _apply_claude(st, prices, upto=None)
    save_state(st)
    write_report(st)
    print(f"{len(days)} 営業日を処理しました → {REPORT.relative_to(ROOT)}")


def cmd_brief(args) -> None:
    """Claude 口座の判断材料（毎週火曜の定期実行で使う）。paper/briefs/<日付>.md に残す。

    判断に使ってよいのは、ここに出る「最後に処理した日までの」数字だけ。
    """
    from src.sim import _ret, _vals, _vol

    st = load_state()
    prices = settled(live_prices())
    last = pd.Timestamp(st["books"]["allcountry"].last_day)
    hist = prices.truncate(last).upto(last)
    lines = [f"# 判断材料 {last.date()}（この日の終値まで）", "",
             "| 銘柄 | 1 か月 | 3 か月 | 12 か月 | 200 日線との差 | 60 日変動率 | 1 年高値から |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for s in UNIVERSE:
        a = _vals(hist, s)
        if len(a) < 253:
            continue
        sma = sum(a[-200:]) / 200
        lines.append(f"| {s} | {_pct(_ret(a, 21))} | {_pct(_ret(a, 63))} | {_pct(_ret(a, 252))} | "
                     f"{_pct(a[-1] / sma - 1)} | {(_vol(a, 60) or 0) * 100:.1f}% | {_pct(a[-1] / max(a[-252:]) - 1)} |")
    fx = prices.fx.loc[:last]
    lines += ["", f"USDJPY {fx.iloc[-1]:.2f}（1 か月 {_pct(fx.iloc[-1] / fx.iloc[-22] - 1)}、"
                  f"12 か月 {_pct(fx.iloc[-1] / fx.iloc[-253] - 1)}）", ""]
    cap = st["capital"]
    base = st["books"]["allcountry"].nav[-1][1] / cap - 1
    c = st["books"]["claude"]
    lines += [f"Claude 口座: 開始来 {_pct(c.nav[-1][1] / cap - 1)}（全世界株 1 本 {_pct(base)}）",
              "保有: " + ("、".join(f"{s} {q:g}" for s, q in sorted(c.qty.items())) or "なし") + f"、現金 {c.cash:,.0f} 円",
              "", "※ 変えるなら paper/claude.yaml に追記（date はこの日）。据え置きも判断。売買はコスト。"]
    out = PAPER / "briefs" / f"{last.date()}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n→ {out.relative_to(ROOT)}")


def cmd_report(args) -> None:
    write_report(load_state())
    print(f"→ {REPORT.relative_to(ROOT)}")


# ------------------------------------------------------------------ レポート

def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def write_report(st: dict) -> None:
    books: dict[str, Book] = st["books"]
    cap = st["capital"]
    base = books["allcountry"]
    base_ret = base.nav[-1][1] / cap - 1
    lines = [
        f"# ペーパートレード（仮想 {cap / 1e4:,.0f} 万円 × {len(books)} 口座）",
        "",
        "実際の発注はしていない。価格は Yahoo Finance の実際の終値。判断の翌営業日の終値で約定し、"
        "手数料 0.10%・スリッページ 0.05%・為替スプレッド 25 銭を引いている。"
        "NISA 口座を想定（日本の税 0、米国籍 ETF の分配金は米国源泉税 10% を差し引き）。",
        "",
        f"- 開始: {st['start']}（この日の終値までで最初の判断）　予定終了: {st.get('end') or '—'}",
        f"- 最終更新: {st.get('updated_at', '—')}　最後に処理した営業日: {base.last_day}",
        "",
        "| 口座 | 評価額 | 開始来 | 全世界株 1 本との差 | 最大下落 | 約定 | 手数料等 | 分配金 | 課税口座なら税 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, b in books.items():
        nav = pd.Series([v for _, v in b.nav])
        ret = nav.iloc[-1] / cap - 1
        dd = float((nav / nav.cummax() - 1).min())
        inc = sum(i["jpy"] for i in b.income)
        tax = max(b.realized, 0) * TAX_RATE
        lines.append(
            f"| {STRATEGY_LABELS[name]} | {nav.iloc[-1]:,.0f} | {_pct(ret)} | "
            f"{_pct(ret - base_ret) if name != 'allcountry' else '—'} | {_pct(dd)} | "
            f"{len(b.trades)} | {b.fees:,.0f} | {inc:,.0f} | {tax:,.0f} |")
    lines += [
        "",
        "> **1 か月の成績はほぼ運で決まる。** 下のバックテストにある「1 か月リターンのばらつき」と並べて読むこと。"
        "全世界株 1 本との差が、そのばらつきの範囲に収まっているなら、腕の差とは言えない。",
        "",
    ]
    bt = _backtest_summary()
    if bt:
        lines += ["## 参考: 同じ戦略の過去の 1 か月リターン（21 営業日、2009 年〜）", "", *bt, ""]

    for name, b in books.items():
        lines += [f"## {STRATEGY_LABELS[name]}", ""]
        if b.qty:
            day = b.last_day
            lines.append(f"保有（{day} 時点）: " + "、".join(
                f"{s} {q:g}" + ("口" if is_jpy(s) else "株") for s, q in sorted(b.qty.items())))
        lines.append(f"現金: {b.cash:,.0f} 円")
        if b.pending:
            lines.append("未約定の注文: " + "、".join(
                f"{o['side']} {o['symbol']} {o['qty']:g}（{o['decided']} の判断）" for o in b.pending))
        if b.trades:
            lines += ["", "| 約定日 | 判断日 | 銘柄 | 売買 | 数量 | 約定値 | 為替 | 金額(円) |",
                      "|---|---|---|---|---:|---:|---:|---:|"]
            for t in b.trades[-12:]:
                lines.append(f"| {t['date']} | {t['decided']} | {t['symbol']} | {t['side']} | "
                             f"{t['qty']:g} | {t['price']:,.2f} | {t['fx'] or '—'} | {t['jpy']:,.0f} |")
        for n in b.notes[-5:]:
            lines.append(f"- ⚠ {n}")
        lines.append("")

    ds = claude_decisions()
    if ds:
        lines += ["## Claude の判断の記録", ""]
        for d in ds:
            w = "、".join(f"{s} {v * 100:.0f}%" for s, v in d["weights"].items())
            lines += [f"### {d['date']}　{w}（残りは現金）", "", d["rationale"], ""]

    lines += ["## NAV 推移", "", "| 日付 | " + " | ".join(books) + " |",
              "|---|" + "---:|" * len(books)]
    dates = [d for d, _ in base.nav]
    navs = {n: dict((d, v) for d, v in b.nav) for n, b in books.items()}
    for d in dates:
        lines.append(f"| {d} | " + " | ".join(f"{navs[n].get(d, float('nan')):,.0f}" for n in books) + " |")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    _export_live_json(st)


def _export_live_json(st: dict) -> None:
    """web/papertrade.json の live 部分を更新する（バックテスト部分は残す）。"""
    data = json.loads(BT_JSON.read_text(encoding="utf-8")) if BT_JSON.exists() else {}
    data["live"] = {
        "start": st["start"], "end": st.get("end"), "capital": st["capital"],
        "updated_at": st.get("updated_at"),
        "books": {n: {"label": STRATEGY_LABELS[n], "nav": b.nav, "trades": b.trades,
                      "qty": b.qty, "cash": b.cash, "fees": b.fees,
                      "pending": b.pending, "income": b.income, "notes": b.notes[-5:]}
                  for n, b in st["books"].items()},
        "claude": claude_decisions(),
    }
    BT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _backtest_summary() -> list[str]:
    if not BT_JSON.exists():
        return []
    data = json.loads(BT_JSON.read_text(encoding="utf-8"))
    m = data.get("monthly")
    if not m:
        return []
    out = ["| 戦略 | 下位 5% | 下位 25% | 中央 | 上位 25% | 上位 5% | 全世界株 1 本に勝った割合 |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for name, q in m.items():
        out.append(f"| {STRATEGY_LABELS[name]} | {_pct(q['p05'])} | {_pct(q['p25'])} | "
                   f"{_pct(q['p50'])} | {_pct(q['p75'])} | {_pct(q['p95'])} | "
                   f"{q['beat_base'] * 100:.0f}% |" if name != "allcountry" else
                   f"| {STRATEGY_LABELS[name]} | {_pct(q['p05'])} | {_pct(q['p25'])} | "
                   f"{_pct(q['p50'])} | {_pct(q['p75'])} | {_pct(q['p95'])} | — |")
    return out


# ------------------------------------------------------------------ バックテスト

def cmd_backtest(args) -> None:
    hist = fetch_bars(years=args.years)
    adj = {s: hist[s]["adj_close"] for s in UNIVERSE}
    prices = Prices(adj, hist[FX_SYMBOL]["close"])
    start = pd.Timestamp(args.start)
    res = backtest(prices, start, args.capital)
    base = res["allcountry"].nav

    stats = {n: r.stats() for n, r in res.items()}
    monthly = {}
    base_w = window_returns(base)
    for n, r in res.items():
        w = window_returns(r.nav)
        q = w.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
        aligned = pd.concat([w, base_w], axis=1, join="inner")
        monthly[n] = {"p05": q[0.05], "p25": q[0.25], "p50": q[0.5], "p75": q[0.75],
                      "p95": q[0.95],
                      "beat_base": float((aligned.iloc[:, 0] > aligned.iloc[:, 1]).mean())}

    # 年ごとのリターン
    yearly = {}
    for n, r in res.items():
        y = r.nav.groupby(r.nav.index.year).last()
        first = r.nav.iloc[0]
        prev = pd.concat([pd.Series([first]), y.iloc[:-1]]).values
        yearly[n] = {int(k): float(v / p - 1) for k, v, p in zip(y.index, y.values, prev)}

    # あなたの計画に近い形: 初期 100 万円 + 毎月 20 万円の積立
    dca = backtest(prices, start, args.dca_initial, monthly=args.dca_monthly)
    dca_stats = {}
    for n, r in dca.items():
        paid = -sum(a for _, a in r.flows)
        dca_stats[n] = {"paid": paid, "final": float(r.nav.iloc[-1]),
                        "xirr": r.money_weighted(), "trades": r.trades, "fees": r.fees,
                        "tax_if_taxable": max(r.realized, 0.0) * TAX_RATE}

    # 開始年をずらした 10 年の積立。2009 年開始だけを見ると、上げ相場と円安を拾った結論になりうる
    rolling = {}
    last_day = res["allcountry"].nav.index[-1]
    for y in range(start.year, 2027):
        s0 = pd.Timestamp(f"{y}-{start.month:02d}-01")
        e0 = s0 + pd.DateOffset(years=args.window)
        if e0 > last_day:
            break
        rr = backtest(prices, s0, args.dca_initial, monthly=args.dca_monthly, end=e0)
        rolling[f"{y}"] = {n: {"xirr": r.money_weighted(),
                               "final": float(r.nav.iloc[-1]),
                               "paid": -sum(a for _, a in r.flows)} for n, r in rr.items()}
        print(f"  {y}〜{e0.year}: " + " / ".join(
            f"{n} {rolling[f'{y}'][n]['xirr'] * 100:+.1f}%" for n in rr))

    data = json.loads(BT_JSON.read_text(encoding="utf-8")) if BT_JSON.exists() else {}
    data.update({
        "dca": {"initial": args.dca_initial, "monthly": args.dca_monthly, "stats": dca_stats,
                "nav": {n: [[str(d.date()), round(float(v))] for d, v in r.nav.resample("W-FRI").last().dropna().items()]
                        for n, r in dca.items()}},
        "rolling": {"years": args.window, "windows": rolling},
        "generated": date.today().isoformat(),
        "start": str(res["allcountry"].nav.index[0].date()),
        "end": str(res["allcountry"].nav.index[-1].date()),
        "capital": args.capital,
        "labels": STRATEGY_LABELS,
        "stats": stats, "monthly": monthly, "yearly": yearly,
        # グラフ用に週次へ間引く
        "nav": {n: [[str(d.date()), round(float(v))] for d, v in r.nav.resample("W-FRI").last().dropna().items()]
                for n, r in res.items()},
    })
    BT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    L = [
        "# バックテスト（実際の過去データ）",
        "",
        f"期間 {data['start']} 〜 {data['end']}、初期 {args.capital:,.0f} 円、円建て評価。"
        "価格は Yahoo Finance の配当調整後終値（分配金は再投資扱い・源泉税の差は入っていない）。"
        "判断は月初の終値まで、約定は翌営業日の終値。手数料 0.10%・スリッページ 0.05%・"
        "為替スプレッド 25 銭。追加の積立は無し（一括投資）。",
        "",
        "コア・サテライトの個別株 20% は QQQ で代用した。今の大型株を過去に遡って選ぶと、"
        "勝ち残った銘柄を後から選ぶことになる（生存バイアス）ため。",
        "",
        "Claude の裁量はバックテストできない（モデルが過去の結果を知っている可能性がある）。",
        "",
        "| 戦略 | 最終額 | 年率 | 年率ボラ | 最大下落 | 約定回数 | 手数料等 | 課税口座なら税（実現益分） |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for n, s in stats.items():
        L.append(f"| {STRATEGY_LABELS[n]} | {s['final']:,.0f} | {_pct(s['cagr'])} | "
                 f"{s['vol'] * 100:.1f}% | {_pct(s['max_dd'])} | {s['trades']} | "
                 f"{s['fees']:,.0f} | {s['tax_if_taxable']:,.0f} |")
    L += ["", "## 1 か月（21 営業日）リターンのばらつき", "",
          "開始日を 1 日ずつずらして、すべての 1 か月を数えたもの。ペーパートレードの 1 か月の結果は、"
          "この表のどこかに落ちる。", "", *_backtest_summary(), "",
          "## 年ごとのリターン", "",
          "| 年 | " + " | ".join(STRATEGY_LABELS[n] for n in res) + " |",
          "|---|" + "---:|" * len(res)]
    for yr in sorted(next(iter(yearly.values()))):
        L.append(f"| {yr} | " + " | ".join(_pct(yearly[n][yr]) for n in res) + " |")
    L += ["", f"## 積立にした場合（初期 {args.dca_initial:,.0f} 円 + 毎月 {args.dca_monthly:,.0f} 円）", "",
          "毎月最初の営業日に入金し、その日の終値までで判断、翌営業日に約定。"
          "年率は入金のタイミングを反映した金額加重（XIRR）。", "",
          "| 戦略 | 入れた額 | 最終額 | 年率（XIRR） | 約定回数 | 手数料等 | 課税口座なら税 |",
          "|---|---:|---:|---:|---:|---:|---:|"]
    for n, s in dca_stats.items():
        L.append(f"| {STRATEGY_LABELS[n]} | {s['paid']:,.0f} | {s['final']:,.0f} | {_pct(s['xirr'])} | "
                 f"{s['trades']} | {s['fees']:,.0f} | {s['tax_if_taxable']:,.0f} |")
    L += ["", f"## 開始年をずらした {args.window} 年の積立（年率 XIRR）", "",
          "2009 年開始だけだと、上げ相場と円安の時期をたまたま拾った結論になりうる。開始年を 1 年ずつずらした。", "",
          "| 開始 | " + " | ".join(STRATEGY_LABELS[n] for n in res) + " | 1 位 |",
          "|---|" + "---:|" * len(res) + "---|"]
    for y, row in rolling.items():
        best = max(row, key=lambda n: row[n]["xirr"])
        L.append(f"| {y} | " + " | ".join(_pct(row[n]["xirr"]) for n in res) + f" | {STRATEGY_LABELS[best]} |")
    L += ["", "※ 過去の結果は将来を保証しない。戦略は結果を見る前に固定した（src/sim.py）。", ""]
    BT_MD.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L[:20 + len(stats)]))
    print(f"\n→ {BT_MD.relative_to(ROOT)} / {BT_JSON.relative_to(ROOT)}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("backtest")
    p.add_argument("--start", default="2009-07-01")
    p.add_argument("--years", type=float, default=18.5)
    p.add_argument("--capital", type=float, default=5_000_000)
    p.add_argument("--dca-initial", type=float, default=1_000_000)
    p.add_argument("--dca-monthly", type=float, default=200_000)
    p.add_argument("--window", type=int, default=10, help="開始年をずらす比較の年数")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("init")
    p.add_argument("--capital", type=float, default=5_000_000)
    p.add_argument("--decide-on", required=True, help="最初の判断に使う終値の日（YYYY-MM-DD）")
    p.add_argument("--end", default=None, help="予定終了日（記録用）")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("run")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("report")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("brief", help="Claude 口座の判断材料を出す（paper/briefs/ に残す）")
    p.set_defaults(func=cmd_brief)

    args = ap.parse_args(argv)
    PAPER.mkdir(exist_ok=True)
    args.func(args)


if __name__ == "__main__":
    main()
