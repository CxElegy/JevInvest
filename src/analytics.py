"""持仓の評価・配分・リスク・リバランス計算。

すべてローカルで計算する。ここに LLM は一切関与しない。
Claude には計算結果（report.py の出力）を読ませて判断させる。
この分離のおかげで、API クレジットを買わずに Claude Pro のまま運用できる。

正直さのルール:
  - 価格が取れなかったものを黙って 0 にしない。「評価不能」として残す
  - 現在ウェイトで過去をなぞった指標は「実際の運用成績ではない」と明記する
  - 外貨建ての損益は、値上がり益と為替差益に分けて出す
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from .holdings import Holdings, Position
from .marketdata import MarketData

TRADING_DAYS = 252


@dataclass
class PositionValuation:
    position: Position
    priced: bool
    price_source: str
    price_native: float | None
    fx_rate: float
    market_value_base: float
    cost_base: float
    pnl_base: float
    pnl_pct: float
    # 外貨建てで cost_fx_rate がある場合のみ埋まる
    price_pnl_base: float | None = None
    fx_pnl_base: float | None = None
    weight_pct: float = 0.0


@dataclass
class GuardViolation:
    guard: str
    severity: str     # "violation"（上限・下限を破っている） / "warning"（近い・注意）
    subject: str
    detail: str
    current: float
    limit: float


@dataclass
class Analysis:
    as_of: datetime
    base_currency: str
    valuations: list[PositionValuation]
    total_value_base: float
    total_cost_base: float
    unpriced_count: int
    by_asset_class: pd.DataFrame
    by_account: pd.DataFrame
    by_currency: pd.DataFrame
    concentration: dict
    rebalance: pd.DataFrame
    risk: dict = field(default_factory=dict)
    guards: list[GuardViolation] = field(default_factory=list)
    # 設定されている基準そのもの。違反ゼロのときに
    # 「検査したが問題なし」と「そもそも設定が無い」を区別するために持つ。
    guard_config: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_pnl_base(self) -> float:
        return self.total_value_base - self.total_cost_base

    @property
    def total_pnl_pct(self) -> float:
        return (self.total_pnl_base / self.total_cost_base * 100.0) if self.total_cost_base else 0.0


# --------------------------------------------------------------- 評価

def _value_positions(
    h: Holdings, quotes: dict[str, float], fx: dict[str, float], warnings: list[str]
) -> list[PositionValuation]:
    vals: list[PositionValuation] = []

    for p in h.positions:
        rate = fx.get(p.currency)
        if rate is None:
            warnings.append(f"{p.symbol}: 為替 {p.currency}/{h.base_currency} 不明。評価不能")
            rate = float("nan")

        if p.is_cash:
            price, source, priced = 1.0, "現金（額面）", True
        elif p.price_override is not None:
            price = p.price_override
            source = f"手入力{f'（{p.price_as_of} 時点）' if p.price_as_of else ''}"
            priced = True
        elif p.symbol in quotes:
            price, source, priced = quotes[p.symbol], "yfinance", True
        else:
            price, source, priced = None, "取得失敗", False
            warnings.append(f"{p.symbol}: 価格が取れず評価に含められません")

        if not priced or price is None or np.isnan(rate):
            vals.append(
                PositionValuation(
                    position=p, priced=False, price_source=source, price_native=price,
                    fx_rate=rate, market_value_base=0.0, cost_base=0.0,
                    pnl_base=0.0, pnl_pct=0.0,
                )
            )
            continue

        mv_base = p.quantity * price * rate

        # 取得原価。cost_fx_rate があればその時のレートで、無ければ現在レートで換算する。
        # 後者の場合、損益に為替の影響が混ざる（分解もできない）。
        cost_rate = p.cost_fx_rate if p.cost_fx_rate is not None else rate
        cost_base = p.cost_basis_native * cost_rate

        pnl = mv_base - cost_base
        pnl_pct = (pnl / cost_base * 100.0) if cost_base else 0.0

        price_pnl = fx_pnl = None
        if p.cost_fx_rate is not None:
            # 値上がり益: 取得時レートで固定して価格変動分だけを見る
            price_pnl = p.quantity * (price - p.cost_basis_per_unit) * p.cost_fx_rate
            # 為替差益: 現在の native 評価額に対するレート変動分
            fx_pnl = p.quantity * price * (rate - p.cost_fx_rate)

        vals.append(
            PositionValuation(
                position=p, priced=True, price_source=source, price_native=price,
                fx_rate=rate, market_value_base=mv_base, cost_base=cost_base,
                pnl_base=pnl, pnl_pct=pnl_pct,
                price_pnl_base=price_pnl, fx_pnl_base=fx_pnl,
            )
        )

    return vals


def _group(vals: list[PositionValuation], key) -> pd.DataFrame:
    rows: dict[str, dict] = {}
    for v in vals:
        if not v.priced:
            continue
        k = key(v.position)
        r = rows.setdefault(k, {"評価額": 0.0, "取得原価": 0.0, "銘柄数": 0})
        r["評価額"] += v.market_value_base
        r["取得原価"] += v.cost_base
        r["銘柄数"] += 1

    if not rows:
        return pd.DataFrame(columns=["評価額", "取得原価", "銘柄数", "損益", "損益率%", "構成比%"])

    df = pd.DataFrame(rows).T
    df["損益"] = df["評価額"] - df["取得原価"]
    df["損益率%"] = np.where(df["取得原価"] != 0, df["損益"] / df["取得原価"] * 100.0, 0.0)
    total = df["評価額"].sum()
    df["構成比%"] = df["評価額"] / total * 100.0 if total else 0.0
    return df.sort_values("評価額", ascending=False)


def _concentration(vals: list[PositionValuation], total: float) -> dict:
    weights = np.array(
        [v.market_value_base / total for v in vals if v.priced and v.market_value_base > 0]
    )
    if weights.size == 0:
        return {}

    hhi = float((weights**2).sum())
    order = np.sort(weights)[::-1]
    return {
        "hhi": hhi,
        # 1/HHI。「実質何銘柄に分散できているか」。等ウェイト n 銘柄なら n になる。
        "実質銘柄数": 1.0 / hhi if hhi else 0.0,
        "実銘柄数": int(weights.size),
        "最大1銘柄%": float(order[0] * 100.0),
        "上位3銘柄%": float(order[:3].sum() * 100.0),
        "上位5銘柄%": float(order[:5].sum() * 100.0),
    }


def _rebalance(h: Holdings, by_class: pd.DataFrame, total: float) -> pd.DataFrame:
    if not h.target_allocation or total <= 0:
        return pd.DataFrame()

    classes = sorted(set(h.target_allocation) | set(by_class.index))
    rows = []
    for c in classes:
        cur_val = float(by_class.loc[c, "評価額"]) if c in by_class.index else 0.0
        cur_pct = cur_val / total * 100.0
        tgt_pct = float(h.target_allocation.get(c, 0.0))
        tgt_val = total * tgt_pct / 100.0
        rows.append(
            {
                "資産クラス": c,
                "現在%": cur_pct,
                "目標%": tgt_pct,
                "乖離pp": cur_pct - tgt_pct,
                "現在額": cur_val,
                "目標額": tgt_val,
                "調整額": tgt_val - cur_val,   # 正 = 買い増し、負 = 売却
            }
        )

    df = pd.DataFrame(rows).set_index("資産クラス")
    return df.reindex(df["乖離pp"].abs().sort_values(ascending=False).index)


# --------------------------------------------------------------- ガードレール

# 上限の何割に達したら「近い」として警告するか
GUARD_WARN_RATIO = 0.9
# 相関を見る対象の最小ウェイト。小さすぎる銘柄の相関を報告しても意味がない
CORR_MIN_WEIGHT_PCT = 2.0


def _check_guards(
    h: Holdings, vals: list[PositionValuation], total: float, risk: dict
) -> list[GuardViolation]:
    """設定した上限・下限を実際に検査する。

    設定値を置いてあるだけでは意味がないので、ここで必ず突き合わせる。
    """
    g = h.guards or {}
    out: list[GuardViolation] = []
    if total <= 0:
        return out

    # --- 個別銘柄の上限 ---
    #
    # 同じ銘柄を複数口座で持つと 1 行ずつでは上限内なのに合計で超えることがある
    # （特定口座 12% + NISA 13% = 25%）。銘柄単位で合算してから判定する。
    cap = g.get("max_single_position_pct")
    if cap is not None:
        cap = float(cap)
        # 上限は個別銘柄のためのもの。コアのインデックスファンドや債券まで縛ると、
        # 目標配分どおりに持っただけで違反になる。除外する資産クラスは台帳で指定する。
        exempt = {str(c) for c in (g.get("single_position_exempt_classes") or [])}
        by_symbol: dict[str, float] = {}
        names: dict[str, str] = {}
        for v in vals:
            if not v.priced or v.position.is_cash or v.position.asset_class in exempt:
                continue
            s = v.position.symbol
            by_symbol[s] = by_symbol.get(s, 0.0) + v.market_value_base
            names[s] = v.position.name

        for sym, mv in sorted(by_symbol.items(), key=lambda kv: -kv[1]):
            w = mv / total * 100.0
            if w > cap:
                out.append(
                    GuardViolation(
                        guard="max_single_position_pct",
                        severity="violation",
                        subject=sym,
                        detail=f"{names.get(sym, sym)} が上限 {cap:.0f}% を超えています",
                        current=w,
                        limit=cap,
                    )
                )
            elif w > cap * GUARD_WARN_RATIO:
                out.append(
                    GuardViolation(
                        guard="max_single_position_pct",
                        severity="warning",
                        subject=sym,
                        detail=f"{names.get(sym, sym)} が上限 {cap:.0f}% に近づいています",
                        current=w,
                        limit=cap,
                    )
                )

    # --- 現金の下限 ---
    floor = g.get("min_cash_pct")
    if floor is not None:
        floor = float(floor)
        cash = sum(v.market_value_base for v in vals if v.priced and v.position.is_cash)
        w = cash / total * 100.0
        if w < floor:
            out.append(
                GuardViolation(
                    guard="min_cash_pct",
                    severity="violation",
                    subject="現金",
                    detail=f"現金比率が下限 {floor:.0f}% を下回っています",
                    current=w,
                    limit=floor,
                )
            )

    # --- 相関が高いペア ---
    thr = g.get("high_correlation_threshold")
    corr = risk.get("相関")
    if thr is not None and isinstance(corr, pd.DataFrame) and not corr.empty:
        thr = float(thr)
        per = risk.get("銘柄別")
        weights = per["ウェイト%"].to_dict() if per is not None else {}
        cols = list(corr.columns)
        for i, a in enumerate(cols):
            for b in cols[i + 1 :]:
                # 両方それなりの重みがあるペアだけ見る。
                # 微小な保有どうしの相関を並べても判断材料にならない。
                if (weights.get(a, 0.0) < CORR_MIN_WEIGHT_PCT
                        or weights.get(b, 0.0) < CORR_MIN_WEIGHT_PCT):
                    continue
                c = corr.loc[a, b]
                if pd.notna(c) and abs(c) >= thr:
                    out.append(
                        GuardViolation(
                            guard="high_correlation_threshold",
                            severity="warning",
                            subject=f"{a} / {b}",
                            detail=(
                                f"相関 {c:.2f}。分散しているつもりで"
                                f"同じリスクを重ねている可能性があります"
                                f"（ウェイト {weights.get(a, 0):.1f}% / "
                                f"{weights.get(b, 0):.1f}%）"
                            ),
                            current=float(c),
                            limit=thr,
                        )
                    )

    return out


# --------------------------------------------------------------- リスク

def _base_currency_series(
    h: Holdings, vals: list[PositionValuation], md: MarketData, years: float
) -> tuple[pd.DataFrame, list[str]]:
    """各銘柄の「基準通貨建て」価格系列を作る。

    外貨建て銘柄は native 価格 × 為替 にする。これをやらないと
    円建て投資家にとってのリスクを測れない（ドル建てのボラだけ見ても意味がない）。
    現金も含める — CASH-USD は価格一定だが為替でぶれるので、
    この掛け算だけで自動的に為替リスクとして現れる。

    価格は adj_close（配当再投資後）を使う。評価額には実際の価格を使うが、
    リターン・ボラ・相関は配当を含めたトータルリターンで測るのが正しい。
    BND のような債券 ETF は配当が収益の大半なので、ここを間違えると
    「債券はほぼ無リターン」という誤った結論になる。
    """
    notes: list[str] = []
    priced = [v for v in vals if v.priced]

    quote_syms = sorted({v.position.symbol for v in priced if v.position.needs_quote})
    fx_syms = sorted(
        {f"{c}{h.base_currency}=X" for c in {v.position.currency for v in priced}
         if c != h.base_currency}
    )

    hist = md.get_history(quote_syms + fx_syms, years=years)

    fx_series: dict[str, pd.Series] = {h.base_currency: None}  # type: ignore[dict-item]
    for c in {v.position.currency for v in priced}:
        if c == h.base_currency:
            continue
        pair = f"{c}{h.base_currency}=X"
        if pair in hist:
            fx_series[c] = hist[pair]["adj_close"]
        else:
            notes.append(f"為替 {pair} の履歴が取れず、{c} 建て資産をリスク計算から除外")

    cols: dict[str, pd.Series] = {}
    for v in priced:
        p = v.position
        if p.currency != h.base_currency and p.currency not in fx_series:
            continue

        if p.is_cash:
            # 為替の index に合わせた定数系列
            if p.currency == h.base_currency:
                continue  # 円現金は基準通貨建てで完全に無リスク。計算に入れても 0
            base_fx = fx_series[p.currency]
            cols[p.symbol] = pd.Series(1.0, index=base_fx.index) * base_fx
            continue

        if p.symbol not in hist:
            if p.price_override is not None:
                notes.append(f"{p.symbol}: 手入力価格のみで履歴が無く、リスク計算から除外")
            else:
                notes.append(f"{p.symbol}: 履歴が取れずリスク計算から除外")
            continue

        native = hist[p.symbol]["adj_close"]
        if p.currency == h.base_currency:
            cols[p.symbol] = native
        else:
            cols[p.symbol] = (native * fx_series[p.currency]).dropna()

    if not cols:
        return pd.DataFrame(), notes

    return pd.DataFrame(cols).sort_index().ffill().dropna(how="all"), notes


def _risk(
    h: Holdings, vals: list[PositionValuation], md: MarketData,
    years: float, benchmark: str,
) -> dict:
    prices, notes = _base_currency_series(h, vals, md, years)
    if prices.empty or len(prices) < 60:
        return {"notes": notes + ["履歴が不足しておりリスク指標を計算できません"]}

    rets = prices.pct_change().dropna(how="all")
    covered = [c for c in prices.columns]

    mv = {v.position.symbol: v.market_value_base for v in vals if v.priced}
    covered_total = sum(mv.get(c, 0.0) for c in covered)
    if covered_total <= 0:
        return {"notes": notes + ["リスク計算対象の評価額が 0 です"]}

    w = pd.Series({c: mv.get(c, 0.0) / covered_total for c in covered})

    port_ret = (rets[covered].fillna(0.0) * w).sum(axis=1)
    equity = (1.0 + port_ret).cumprod()
    dd = equity / equity.cummax() - 1.0

    per_symbol = pd.DataFrame(
        {
            "年率ボラ%": rets[covered].std(ddof=0) * np.sqrt(TRADING_DAYS) * 100.0,
            "ウェイト%": w * 100.0,
        }
    ).sort_values("ウェイト%", ascending=False)

    out: dict = {
        "対象期間": f"{prices.index[0].date()} 〜 {prices.index[-1].date()}",
        "営業日数": int(len(prices)),
        "カバー率%": covered_total / sum(mv.values()) * 100.0 if mv else 0.0,
        "年率ボラ%": float(port_ret.std(ddof=0) * np.sqrt(TRADING_DAYS) * 100.0),
        "最大DD%": float(dd.min() * 100.0),
        "銘柄別": per_symbol,
        "相関": rets[covered].corr() if len(covered) > 1 else pd.DataFrame(),
        "notes": notes,
    }

    # 分散効果: 個別ボラの加重平均に対して、実際のポートフォリオのボラがどれだけ低いか
    weighted_avg_vol = float((per_symbol["年率ボラ%"] * w * 100.0).sum() / 100.0)
    if weighted_avg_vol:
        out["加重平均ボラ%"] = weighted_avg_vol
        out["分散効果%"] = (1.0 - out["年率ボラ%"] / weighted_avg_vol) * 100.0

    # ベンチマークに対するベータ
    bench_hist = md.get_history([benchmark], years=years)
    if benchmark in bench_hist:
        b = bench_hist[benchmark]["adj_close"].pct_change().dropna()
        joined = pd.concat([port_ret.rename("p"), b.rename("b")], axis=1).dropna()
        if len(joined) > 60 and joined["b"].var() > 0:
            out["ベンチマーク"] = benchmark
            out["ベータ"] = float(joined["p"].cov(joined["b"]) / joined["b"].var())
            out["相関(対ベンチ)"] = float(joined["p"].corr(joined["b"]))
    else:
        notes.append(f"ベンチマーク {benchmark} の履歴が取れずベータを計算できません")

    return out


# --------------------------------------------------------------- 入口

def analyze(
    h: Holdings,
    md: MarketData,
    years: float = 3.0,
    benchmark: str = "VT",
    with_risk: bool = True,
) -> Analysis:
    warnings = list(h.warnings)

    quotes = md.get_quotes(h.symbols_needing_quote)
    fx = md.get_fx(h.currencies, h.base_currency)

    vals = _value_positions(h, quotes, fx, warnings)
    total = sum(v.market_value_base for v in vals if v.priced)
    total_cost = sum(v.cost_base for v in vals if v.priced)

    for v in vals:
        if v.priced and total:
            v.weight_pct = v.market_value_base / total * 100.0

    by_class = _group(vals, lambda p: p.asset_class)
    analysis = Analysis(
        as_of=datetime.now(),
        base_currency=h.base_currency,
        valuations=vals,
        total_value_base=total,
        total_cost_base=total_cost,
        unpriced_count=sum(1 for v in vals if not v.priced),
        by_asset_class=by_class,
        by_account=_group(vals, lambda p: p.account),
        by_currency=_group(vals, lambda p: p.currency),
        concentration=_concentration(vals, total),
        rebalance=_rebalance(h, by_class, total),
        guard_config=dict(h.guards or {}),
        warnings=warnings,
    )

    if with_risk:
        analysis.risk = _risk(h, vals, md, years, benchmark)
        analysis.warnings.extend(analysis.risk.get("notes", []))

    # ガードレールは相関（リスク計算の産物）も使うので、リスクの後に回す
    analysis.guards = _check_guards(h, vals, total, analysis.risk)

    # データ取得側の警告は最後にまとめて回収する。
    # 履歴取得（=異常バー検出）は _risk の中で走るので、先に集めると取りこぼす。
    # report 側で重複を潰すので、全部足してよい。
    analysis.warnings.extend(md.fetch_errors)

    return analysis
