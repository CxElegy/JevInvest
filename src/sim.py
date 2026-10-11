"""紙上の売買（ペーパートレード）とバックテストの共通エンジン。

実際の発注はしない。価格は Yahoo Finance の実際の日足だけを使う。

決まり（ライブとバックテストで同じ。片方だけ有利にしない）:
  - 判断は「その日の終値まで」のデータで行い、約定は銘柄ごとに「判断日より後の最初の終値」。
    判断した瞬間の価格では約定させない（後知恵を入れないため）。
  - 費用: 売買手数料 0.10%、スリッページ 0.05%、為替スプレッド 1 ドルあたり 0.25 円（片道）。
  - 単元: 米国 ETF は 1 株、東証の ETF は銘柄ごとの単元（1348.T は 1 口）。端数は買わない。
  - ライブは配当調整なしの終値で約定・評価し、配当は権利落ち日に現金で受け取る
    （米国籍 ETF は米国の源泉税 10% を引く。NISA 口座を想定し、日本の税は 0）。
    バックテストは配当込みの調整後終値を使う（源泉税の差は入らない）。
  - 課税口座だった場合の税（譲渡益の 20.315%）は、実現益から別に試算して出す。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import pandas as pd

FX_SYMBOL = "USDJPY=X"
LOT: dict[str, int] = {}   # 東証 ETF の単元（1348.T は 1 口）
TAX_RATE = 0.20315
BUY_MARGIN = 1.005          # 買うだけの調整で、手数料・スプレッド・翌日の値動きに残す余裕
DRIFT_BAND = 0.05           # freq="band": どれかが目標から 5 ポイント以上ずれたら売って戻す

# 日本株は 1348.T（MAXIS トピックス上場投信）。1306.T は Yahoo の過去データに
# 未調整の分割（2015-01 に -90% のまま戻らない）があり、バックテストが壊れるため使わない。
UNIVERSE = ["VT", "QQQ", "SPY", "VEU", "AGG", "BIL", "GLD", "1348.T", "TLT", "IEF", "DBC", "VNQ"]


def is_jpy(symbol: str) -> bool:
    return symbol.endswith(".T")


def lot_of(symbol: str) -> int:
    return LOT.get(symbol, 1)


@dataclass(frozen=True)
class Costs:
    fee_rate: float = 0.0010
    slippage: float = 0.0005
    fx_spread: float = 0.25          # 円 / ドル、片道
    us_withholding: float = 0.10     # 米国籍 ETF の分配金にかかる米国の源泉税


# ------------------------------------------------------------------ 価格の表

class Prices:
    """銘柄ごとの日足（ネイティブ通貨）と USDJPY。引くときは「その日以前で最新」。"""

    def __init__(self, series: dict[str, pd.Series], fx: pd.Series,
                 dividends: dict[str, pd.Series] | None = None,
                 signals: dict[str, pd.Series] | None = None):
        """series は約定・評価に使う価格、signals は戦略の判断に使う価格。

        ライブは約定を配当調整なしの終値、判断を調整後終値で行う（配当落ちを下落と見ないため）。
        バックテストはどちらも調整後終値。
        """
        self.series = {s: v.dropna().sort_index() for s, v in series.items()}
        self.fx = fx.dropna().sort_index()
        self.dividends = {s: v.sort_index() for s, v in (dividends or {}).items()}
        self.signals = ({s: v.dropna().sort_index() for s, v in signals.items()}
                        if signals else self.series)

    def truncate(self, last: pd.Timestamp) -> "Prices":
        """last より後の価格を捨てた写し。確定していない足で判断・約定しないために使う。"""
        cut = lambda d: {k: v.loc[:last] for k, v in d.items()}  # noqa: E731
        return Prices(cut(self.series), self.fx.loc[:last], cut(self.dividends), cut(self.signals))

    def common_last(self, symbols: list[str]) -> pd.Timestamp | None:
        """全銘柄と為替の足が揃っている最後の日（どれかが遅れていれば、その手前まで）。"""
        lasts = [self.series[s].index[-1] for s in symbols if s in self.series and len(self.series[s])]
        if len(lasts) < len(symbols) or not len(self.fx):
            return None
        return min(min(lasts), self.fx.index[-1])

    def calendar(self) -> list[pd.Timestamp]:
        days: set[pd.Timestamp] = set()
        for s in self.series.values():
            days.update(s.index)
        return sorted(days)

    def has_bar(self, symbol: str, day: pd.Timestamp) -> bool:
        s = self.series.get(symbol)
        return s is not None and day in s.index

    def last(self, symbol: str, day: pd.Timestamp) -> float | None:
        s = self.series.get(symbol)
        if s is None:
            return None
        s = s.loc[:day]
        return float(s.iloc[-1]) if len(s) else None

    def fx_at(self, day: pd.Timestamp) -> float | None:
        s = self.fx.loc[:day]
        return float(s.iloc[-1]) if len(s) else None

    def jpy(self, symbol: str, day: pd.Timestamp) -> float | None:
        p = self.last(symbol, day)
        if p is None:
            return None
        if is_jpy(symbol):
            return p
        fx = self.fx_at(day)
        return None if fx is None else p * fx

    def upto(self, day: pd.Timestamp) -> dict[str, pd.Series]:
        return {s: v.loc[:day] for s, v in self.signals.items()}


# ------------------------------------------------------------------ 口座

@dataclass
class Book:
    name: str
    cash: float
    qty: dict[str, float] = field(default_factory=dict)
    cost: dict[str, float] = field(default_factory=dict)     # 保有分の取得原価（円・総平均）
    pending: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    nav: list[list] = field(default_factory=list)            # [日付, 評価額]
    income: list[dict] = field(default_factory=list)         # 受け取った分配金
    notes: list[str] = field(default_factory=list)           # 約定できなかった等の記録
    realized: float = 0.0
    fees: float = 0.0
    last_day: str | None = None
    last_decision: str | None = None
    # 課税口座（特定口座・源泉徴収あり）として扱うか。売却益にその場で 20.315% を課し、
    # 同じ年のうちに損が出たら、その年に払った分を上限に還付する（年をまたいだ繰越はしない）
    taxable: bool = False
    tax_paid: float = 0.0
    tax_year: int | None = None
    year_gain: float = 0.0
    year_tax: float = 0.0

    # ---- 評価

    def value(self, prices: Prices, day: pd.Timestamp) -> float:
        total = self.cash
        for s, q in self.qty.items():
            if q:
                px = prices.jpy(s, day)
                if px is None:
                    raise ValueError(f"{self.name}: {s} の {day.date()} 時点の価格がありません")
                total += q * px
        return total

    def weights(self, prices: Prices, day: pd.Timestamp) -> dict[str, float]:
        v = self.value(prices, day)
        out = {s: q * prices.jpy(s, day) / v for s, q in self.qty.items() if q}
        out["CASH"] = self.cash / v
        return out

    # ---- 判断 → 注文

    def decide(self, prices: Prices, day: pd.Timestamp, target: dict[str, float],
               band: float = 0.005, reason: str = "", buy_only: bool = False) -> None:
        """目標ウェイトに寄せる注文を作る。約定は次の営業日の終値。

        buy_only: 売らずに、足りない銘柄を手元の現金の範囲で買い足すだけ（積立で寄せる）。
        現金が足りなければ、各銘柄の不足額に比例して減らす（記号順に先着で使い切らない）。
        """
        nav = self.value(prices, day)
        self.pending = []
        orders = []
        pxs: dict[str, float] = {}
        for s in sorted(set(target) | set(self.qty)):
            if s == "CASH":
                continue
            w = target.get(s, 0.0)
            px = prices.jpy(s, day)
            if px is None:
                self.notes.append(f"{day.date()} {s}: 価格が無いため注文を作りませんでした")
                continue
            lot = lot_of(s)
            want = math.floor(w * nav / px / lot) * lot
            have = self.qty.get(s, 0.0)
            diff = want - have
            if diff == 0:
                continue
            # 小さすぎる調整は売買コストのほうが大きいので見送る。全部売るときは除く
            if want != 0 and abs(diff) * px < band * nav:
                continue
            pxs[s] = px
            orders.append({"symbol": s, "side": "buy" if diff > 0 else "sell",
                           "qty": abs(diff), "decided": day.date().isoformat(),
                           "reason": reason})
        if buy_only:
            orders = [o for o in orders if o["side"] == "buy"]
            need = sum(o["qty"] * pxs[o["symbol"]] for o in orders) * BUY_MARGIN
            if need > self.cash:
                f = max(self.cash, 0.0) / need
                for o in orders:
                    lot = lot_of(o["symbol"])
                    o["qty"] = math.floor(o["qty"] * f / lot) * lot
                orders = [o for o in orders if o["qty"] > 0]
        # 売りを先に置く（同じ日に約定するとき、売った代金で買えるように）
        self.pending = sorted(orders, key=lambda o: o["side"] != "sell")
        self.last_decision = day.date().isoformat()

    # ---- 約定・分配金

    def process_day(self, prices: Prices, day: pd.Timestamp, costs: Costs) -> None:
        self._dividends(prices, day, costs)
        remaining = []
        for o in self.pending:
            decided = pd.Timestamp(o["decided"])
            if day > decided and prices.has_bar(o["symbol"], day):
                self._fill(o, prices, day, costs)
            else:
                remaining.append(o)
        self.pending = remaining
        self.nav.append([day.date().isoformat(), round(self.value(prices, day), 2)])
        self.last_day = day.date().isoformat()

    def _fill(self, o: dict, prices: Prices, day: pd.Timestamp, costs: Costs) -> None:
        s, side, qty = o["symbol"], o["side"], float(o["qty"])
        px = float(prices.series[s].loc[day])
        fx = 1.0 if is_jpy(s) else prices.fx_at(day)
        if fx is None:
            self.notes.append(f"{day.date()} {s}: 為替が無いため約定を見送りました")
            return
        if side == "buy":
            unit = px * (1 + costs.slippage) * (fx + (0 if is_jpy(s) else costs.fx_spread))
            lot = lot_of(s)
            afford = math.floor(self.cash / (unit * (1 + costs.fee_rate)) / lot) * lot
            if afford < qty:
                self.notes.append(
                    f"{day.date()} {s}: 現金不足で買付を {qty:g} → {afford:g} に減らしました")
                qty = afford
            if qty <= 0:
                return
            gross = qty * unit
            fee = gross * costs.fee_rate
            self.cash -= gross + fee
            self.qty[s] = self.qty.get(s, 0.0) + qty
            self.cost[s] = self.cost.get(s, 0.0) + gross + fee
        else:
            have = self.qty.get(s, 0.0)
            qty = min(qty, have)
            if qty <= 0:
                return
            unit = px * (1 - costs.slippage) * (fx - (0 if is_jpy(s) else costs.fx_spread))
            gross = qty * unit
            fee = gross * costs.fee_rate
            basis = self.cost.get(s, 0.0) * qty / have
            self.cash += gross - fee
            self.realized += gross - fee - basis
            if self.taxable:
                self._withhold(day.year, gross - fee - basis)
            self.qty[s] = have - qty
            self.cost[s] = self.cost.get(s, 0.0) - basis
            if self.qty[s] == 0:
                del self.qty[s]
                self.cost.pop(s, None)
        self.fees += fee
        self.trades.append({
            "date": day.date().isoformat(), "decided": o["decided"], "symbol": s, "side": side,
            "qty": qty, "price": round(px, 4), "fx": None if is_jpy(s) else round(fx, 4),
            "jpy": round(gross, 0), "fee": round(fee, 0), "reason": o.get("reason", ""),
        })

    def _withhold(self, year: int, gain: float) -> None:
        """源泉徴収ありの特定口座と同じ考え方: その年の通算益に対する税を、売るたびに精算する。"""
        if self.tax_year != year:
            self.tax_year, self.year_gain, self.year_tax = year, 0.0, 0.0
        self.year_gain += gain
        due = max(self.year_gain, 0.0) * TAX_RATE
        delta = due - self.year_tax          # 負なら還付
        self.cash -= delta
        self.year_tax += delta
        self.tax_paid += delta

    def _dividends(self, prices: Prices, day: pd.Timestamp, costs: Costs) -> None:
        for s, q in list(self.qty.items()):
            divs = prices.dividends.get(s)
            if divs is None or not q or day not in divs.index:
                continue
            per = float(divs.loc[day])
            fx = 1.0 if is_jpy(s) else prices.fx_at(day)
            if fx is None:
                raise ValueError(f"{self.name}: {day.date()} の為替が無く、{s} の分配金を円にできません")
            tax = 0.0 if is_jpy(s) else costs.us_withholding
            amount = q * per * fx * (1 - tax)
            self.cash += amount
            self.income.append({"date": day.date().isoformat(), "symbol": s,
                                "per_share": per, "jpy": round(amount, 0)})

    # ---- 保存

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Book":
        return cls(**d)


# ------------------------------------------------------------------ 戦略
#
# 戦略は「その日までの価格（配当調整済み・ネイティブ通貨）」だけを見て目標ウェイトを返す。
# 目標に CASH を入れなくてよい（足りない分が現金になる）。


def _vals(hist: dict[str, pd.Series], s: str) -> list[float]:
    """その日までの実際の足（古い順）。計算は Python の素朴な合計で行い、JS 版と同じ順序で足す。"""
    v = hist.get(s)
    return [] if v is None else [float(x) for x in v.dropna().values]


def _ret(s, days: int) -> float | None:
    a = s if isinstance(s, list) else [float(x) for x in s.dropna().values]
    if len(a) <= days:
        return None
    return a[-1] / a[-1 - days] - 1


def _above_sma(s, days: int = 200) -> bool | None:
    a = s if isinstance(s, list) else [float(x) for x in s.dropna().values]
    if len(a) < days:
        return None
    total = 0.0
    for x in a[-days:]:
        total += x
    return a[-1] > total / days


def _vol(a: list[float], days: int) -> float | None:
    """直近 days 本の日次リターンの標準偏差（不偏）× √252。"""
    if len(a) <= days:
        return None
    r = [a[k] / a[k - 1] - 1 for k in range(len(a) - days, len(a))]
    m = 0.0
    for x in r:
        m += x
    m /= len(r)
    ss = 0.0
    for x in r:
        ss += (x - m) * (x - m)
    return math.sqrt(ss / (len(r) - 1)) * math.sqrt(252)


# 戦略は fn(hist, day) → 目標ウェイト（足りない分は現金）。None を返すと「今のまま（注文しない）」。


def allcountry(hist, day=None):
    """全世界株を 1 本で持ち続ける。NISA の積立と同じ考え方の基準。"""
    return {"VT": 1.0}


def core_satellite(hist, day=None):
    """ユーザーの目標配分（docs/PLAN.md）。個別株 20% は QQQ で代用する。

    今の大型株を過去に遡って使うと、勝ち残った銘柄を後から選ぶことになる（生存バイアス）。
    だから個別株の代わりに指数で近似する。
    """
    return {"VT": 0.55, "QQQ": 0.20, "1348.T": 0.05, "AGG": 0.15}


TREND_ASSETS = ["VT", "QQQ", "1348.T", "AGG", "GLD"]


def trend(hist, day=None):
    """5 資産を均等に持ち、200 日移動平均を下回った資産だけ現金にする（Faber 型）。"""
    out = {}
    for s in TREND_ASSETS:
        if _above_sma(_vals(hist, s)):
            out[s] = 1.0 / len(TREND_ASSETS)
    return out


def dual_momentum(hist, day=None):
    """デュアルモメンタム（Antonacci 型）。12 か月リターンで米国株・米国外株・短期債を比べる。

    米国株と米国外株の強いほうが短期国債より強ければそれを 100%、弱ければ債券 100%。
    """
    r = {s: _ret(_vals(hist, s), 252) for s in ("SPY", "VEU", "BIL")}
    if any(v is None for v in r.values()):
        return {"AGG": 1.0}
    best = "SPY" if r["SPY"] >= r["VEU"] else "VEU"
    return {best: 1.0} if r[best] > r["BIL"] else {"AGG": 1.0}


# ---- ここから追加の戦略（出典は STRATEGY_INFO）


def sp500(hist, day=None):
    return {"SPY": 1.0}


def sixty_forty(hist, day=None):
    return {"VT": 0.6, "AGG": 0.4}


def permanent(hist, day=None):
    return {"SPY": 0.25, "TLT": 0.25, "GLD": 0.25, "BIL": 0.25}


def all_weather(hist, day=None):
    return {"SPY": 0.30, "TLT": 0.40, "IEF": 0.15, "GLD": 0.075, "DBC": 0.075}


IVY_ASSETS = ["SPY", "VEU", "IEF", "VNQ", "DBC"]


def ivy(hist, day=None):
    return {s: 0.2 for s in IVY_ASSETS}


def ivy_timing(hist, day=None):
    return {s: 0.2 for s in IVY_ASSETS if _above_sma(_vals(hist, s))}


RP_ASSETS = ["VT", "TLT", "GLD", "DBC"]


def risk_parity(hist, day=None):
    """直近 126 日の変動率の逆数で配分する（素朴なリスクパリティ）。"""
    inv = {}
    for s in RP_ASSETS:
        v = _vol(_vals(hist, s), 126)
        if v is None or v <= 0:
            return {"VT": 0.6, "AGG": 0.4}
        inv[s] = 1.0 / v
    tot = 0.0
    for s in RP_ASSETS:
        tot += inv[s]
    return {s: inv[s] / tot for s in RP_ASSETS}


MOM_ASSETS = ["VT", "QQQ", "1348.T", "AGG", "GLD", "TLT", "VNQ", "DBC"]


def momentum_top3(hist, day=None):
    """1・3・6・12 か月リターンの平均で上位 3 つ。200 日線を下回るものは現金（Faber GTAA Aggressive）。"""
    scores = []
    for s in MOM_ASSETS:
        a = _vals(hist, s)
        rs = [_ret(a, d) for d in (21, 63, 126, 252)]
        if any(r is None for r in rs):
            continue
        scores.append((-(rs[0] + rs[1] + rs[2] + rs[3]) / 4, s))
    scores.sort()
    out = {}
    for _, s in scores[:3]:
        if _above_sma(_vals(hist, s)):
            out[s] = 1.0 / 3
    return out


def vol_target(hist, day=None):
    """全世界株の直近 60 日の変動率が年 12% を超えたら、その分だけ株を減らして短期国債へ。"""
    v = _vol(_vals(hist, "VT"), 60)
    if v is None or v <= 0:
        return {"VT": 1.0}
    w = min(1.0, 0.12 / v)
    return {"VT": w, "BIL": 1.0 - w} if w < 1.0 else {"VT": 1.0}


def spy_timing(hist, day=None):
    a = _vals(hist, "SPY")
    return {"SPY": 1.0} if _above_sma(a) is not False else {"BIL": 1.0}


def buy_the_dip(hist, day=None):
    """高値から 10% 下げるまで現金で待ち、下げたら全額買う。買った後は持ち続ける（None = 何もしない）。"""
    a = _vals(hist, "VT")
    if len(a) < 252:
        return None
    hi = max(a[-252:])
    return {"VT": 1.0} if a[-1] <= hi * 0.9 else None


def sell_in_may(hist, day=None):
    """5〜10 月は短期国債、11〜4 月は全世界株（格言の検証）。"""
    m = day.month if day is not None else 1
    return {"BIL": 1.0} if 5 <= m <= 10 else {"VT": 1.0}


def panic_sell(hist, day=None, start=None):
    """よくある行動の誤り: 最高値から 30% 下げたら全部売り、元の最高値に戻ったら買い直す。

    状態を持たずに履歴から判定する: 直近の最高値 P の後に 0.7P 以下まで下げたことがあり、
    いまも P を下回っていれば「売って待っている」状態。
    """
    v = hist.get("VT")
    if v is not None and start is not None:
        v = v.loc[pd.Timestamp(start):]        # 持ち始める前の高値は知らない（その人の最高値は持ってからのもの）
    a = [] if v is None else [float(x) for x in v.dropna().values]
    if not a:
        return {"VT": 1.0}
    peak, peak_i = a[0], 0
    for k, x in enumerate(a):
        if x >= peak:
            peak, peak_i = x, k
    crashed = min(a[peak_i:]) <= peak * 0.7
    return {"BIL": 1.0} if crashed and a[-1] < peak else {"VT": 1.0}


def home_bias(hist, day=None):
    return {"1348.T": 0.5, "VT": 0.5}


STRATEGIES = {
    "allcountry": (allcountry, "once"),
    "core_satellite": (core_satellite, "monthly"),
    "trend": (trend, "monthly"),
    "dual_momentum": (dual_momentum, "monthly"),
}

# 比較用の追加戦略。出典と規則は docs/STRATEGIES.md
MORE_STRATEGIES = {
    "sp500": (sp500, "once"),
    "sixty_forty": (sixty_forty, "monthly"),
    "permanent": (permanent, "yearly"),
    "all_weather": (all_weather, "monthly"),
    "ivy": (ivy, "monthly"),
    "ivy_timing": (ivy_timing, "monthly"),
    "risk_parity": (risk_parity, "monthly"),
    "momentum_top3": (momentum_top3, "monthly"),
    "vol_target": (vol_target, "monthly"),
    "spy_timing": (spy_timing, "monthly"),
    "buy_the_dip": (buy_the_dip, "monthly"),
    "sell_in_may": (sell_in_may, "monthly"),
    "home_bias": (home_bias, "monthly"),
    "panic_sell": (panic_sell, "monthly"),
}
ALL_STRATEGIES = {**STRATEGIES, **MORE_STRATEGIES}
panic_sell.needs_start = True     # 開始日を受け取る戦略（backtest・実盘が start= で渡す）


def call_strategy(fn, hist, day, start):
    return fn(hist, day, start=start) if getattr(fn, "needs_start", False) else fn(hist, day)

STRATEGY_LABELS = {
    "allcountry": "全世界株 1 本・ほったらかし",
    "core_satellite": "目標配分（コア・サテライト）・毎月リバランス",
    "trend": "トレンドフォロー（200 日線）",
    "dual_momentum": "デュアルモメンタム",
    "claude": "Claude の裁量",
    "sp500": "S&P500 1 本・ほったらかし",
    "sixty_forty": "株 60 / 債券 40",
    "permanent": "パーマネント・ポートフォリオ",
    "all_weather": "オールウェザー（近似）",
    "ivy": "アイビー 5 資産・均等",
    "ivy_timing": "アイビー 5 資産・200 日線",
    "risk_parity": "リスクパリティ（逆ボラ）",
    "momentum_top3": "相対モメンタム上位 3",
    "vol_target": "ボラティリティ・ターゲット",
    "spy_timing": "S&P500・200 日線",
    "buy_the_dip": "押し目待ち（-10% で買う）",
    "sell_in_may": "セル・イン・メイ",
    "home_bias": "日本株 50 / 全世界 50",
    "panic_sell": "狼狽売り（-30% で売り、高値回復で買い戻し）",
}


def is_rebalance_day(day: pd.Timestamp, prev_day: pd.Timestamp | None) -> bool:
    """月の最初の営業日。"""
    return prev_day is None or day.month != prev_day.month


def is_due(freq: str, day: pd.Timestamp, prev_day: pd.Timestamp | None, decided: bool,
           contributing: bool = False) -> bool:
    """判断する日か。once は最初だけ（積立中は毎月）、monthly は月初、yearly は年初（積立中は毎月）。

    cashflow は once と同じ日に判断するが、売らずに買い足すだけ。band は毎月見て、
    ずれが DRIFT_BAND 未満なら買い足すだけ、超えたら売って戻す。
    """
    new_month = is_rebalance_day(day, prev_day)
    if freq in ("once", "cashflow"):
        return new_month if contributing else not decided
    if freq == "yearly":
        return new_month if contributing else (prev_day is None or day.year != prev_day.year)
    return new_month


# ------------------------------------------------------------------ バックテスト

@dataclass
class BacktestResult:
    name: str
    nav: pd.Series
    trades: int
    fees: float
    realized: float
    flows: list = field(default_factory=list)   # 投資家から見た入金（負）。積立の金額加重リターン用
    tax_paid: float = 0.0                       # 課税口座で実際に差し引いた税（還付後）
    unrealized: float = 0.0                     # 最終日の含み益（円）。課税口座で全部売ったときの税の計算用

    def liquidated(self) -> float:
        """最終日に全部売って、含み益にも 20.315% を払った後の額（課税口座の比較用）。"""
        return float(self.nav.iloc[-1]) - max(self.unrealized, 0.0) * TAX_RATE

    def money_weighted(self) -> float | None:
        """積立の年率（XIRR）。入金と最終評価額から出す。"""
        from .transactions import xirr

        if not self.flows:
            return None
        return xirr(self.flows + [(self.nav.index[-1].date(), float(self.nav.iloc[-1]))])

    def stats(self) -> dict:
        nav = self.nav
        years = (nav.index[-1] - nav.index[0]).days / 365.25
        rets = nav.pct_change().dropna()
        dd = nav / nav.cummax() - 1
        return {
            "final": float(nav.iloc[-1]),
            "cagr": float((nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1) if years > 0 else 0.0,
            "vol": float(rets.std() * math.sqrt(252)),
            "max_dd": float(dd.min()),
            "trades": self.trades,
            "fees": self.fees,
            "tax_if_taxable": max(self.realized, 0.0) * TAX_RATE,
        }


def drift(book: Book, prices: Prices, day: pd.Timestamp, target: dict[str, float]) -> float:
    """目標ウェイトからの最大のずれ（現金は除く。価格の無い銘柄は見ない）。"""
    nav = book.value(prices, day)
    if nav <= 0:
        return 0.0
    worst = 0.0
    for s in sorted(set(target) | set(book.qty)):
        if s == "CASH":
            continue
        px = prices.jpy(s, day)
        if px is None:
            continue
        worst = max(worst, abs(book.qty.get(s, 0.0) * px / nav - target.get(s, 0.0)))
    return worst


def buy_only_now(freq: str, book: Book, prices: Prices, day: pd.Timestamp,
                 prev_day: pd.Timestamp | None, target: dict[str, float]) -> bool:
    """この判断を「買い足すだけ」にするか。"""
    if freq == "cashflow":
        return True
    if freq == "yearly":
        # 年 1 回の戦略は、年の途中の積立月には買い増しだけ（売って戻すのは年初）
        return prev_day is not None and day.year == prev_day.year
    if freq == "band":
        return drift(book, prices, day, target) < DRIFT_BAND
    return False


def backtest(prices: Prices, start: pd.Timestamp, capital: float,
             strategies: dict | None = None, costs: Costs = Costs(),
             end: pd.Timestamp | None = None,
             monthly: float = 0.0, taxable: bool = False,
             on_day=None) -> dict[str, BacktestResult]:
    """monthly > 0 なら、毎月最初の営業日にその額を現金で足してから判断する（積立）。

    on_day(name, book, day) は毎日の処理の後に呼ぶ（ウェイトの推移を見る実験用）。

    積立のときは「買って持つだけ」の戦略も毎月判断する（新しい現金を目標どおりに買う）。
    """
    strategies = strategies or STRATEGIES
    cal = [d for d in prices.calendar() if d >= start and (end is None or d <= end)]
    books = {n: Book(name=n, cash=capital, taxable=taxable) for n in strategies}
    flows: dict[str, list[tuple]] = {n: [(cal[0].date(), -capital)] for n in strategies} if cal else {}
    prev = None
    for day in cal:
        new_month = is_rebalance_day(day, prev)
        for name, (fn, freq) in strategies.items():
            b = books[name]
            if monthly and new_month and prev is not None:
                b.cash += monthly
                flows[name].append((day.date(), -monthly))
            b.process_day(prices, day, costs)
            if is_due(freq, day, prev, b.last_decision is not None, bool(monthly)):
                target = call_strategy(fn, prices.upto(day), day, start)
                if target is not None:          # None は「今のまま」（押し目待ちなど）
                    b.decide(prices, day, target, reason=name,
                             buy_only=buy_only_now(freq, b, prices, day, prev, target))
            if on_day is not None:
                on_day(name, b, day)
        prev = day
    out = {}
    for name, b in books.items():
        nav = pd.Series({pd.Timestamp(d): v for d, v in b.nav})
        r = BacktestResult(name, nav, len(b.trades), b.fees, b.realized)
        r.flows = flows.get(name, [])
        r.tax_paid = b.tax_paid
        if cal:
            last = cal[-1]
            r.unrealized = sum(q * prices.jpy(sym, last) - b.cost.get(sym, 0.0) for sym, q in b.qty.items() if q)
        out[name] = r
    return out


def window_returns(nav: pd.Series, days: int = 21) -> pd.Series:
    """長さ days 営業日の期間リターンを、開始日を 1 日ずつずらして全部取る。"""
    return (nav.shift(-days) / nav - 1).dropna()
