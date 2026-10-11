"""取引履歴から、保有・実現損益・実績リターン（XIRR）を出す。

config/transactions.yaml（.gitignore 済み）:

    transactions:
      - date: 2026-04-01
        type: buy                  # buy / sell / dividend / deposit / withdraw
        symbol: VT
        name: Vanguard Total World
        asset_class: developed_equity
        account: NISA
        quantity: 10
        price: 120.5               # 約定単価（その銘柄の通貨建て）
        currency: USD
        fx_rate: 150.2             # 約定時の為替（1 単位 = 基準通貨いくら）。外貨建ては必須
        fee: 0                     # 手数料（基準通貨建て）

      - date: 2026-06-30
        type: dividend
        symbol: VT
        account: NISA
        amount: 12.3               # 受取額（その銘柄の通貨建て）
        currency: USD
        fx_rate: 158.0

      - date: 2026-04-01
        type: deposit              # 口座への入金。amount は基準通貨建て
        amount: 1000000

取得原価は総平均法。外貨建ての為替が無い取引は、黙って 1 倍にせず警告して飛ばす。

XIRR のキャッシュフローの取り方:
  - deposit / withdraw がある → それを外部との出入りとして使う（評価額は現金込み）
  - 無い → 売買・配当を出入りとして使う（評価額は現金を除いた有価証券のみ）
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

import yaml

EPS = 1e-9
TYPES = ("buy", "sell", "dividend", "deposit", "withdraw")


@dataclass(frozen=True)
class Transaction:
    date: date
    type: str
    symbol: str = ""
    name: str = ""
    asset_class: str = ""
    account: str = ""
    quantity: float = 0.0
    price: float = 0.0
    currency: str = ""
    fx_rate: float = 1.0
    fee: float = 0.0
    amount: float = 0.0
    # 実際に台帳へ反映した数量。売却の切り詰め・飛ばしがあると quantity と食い違う。
    # XIRR のキャッシュフローはこちらを使う（None は quantity どおり反映した意味）。
    applied_quantity: float | None = None

    @property
    def effective_quantity(self) -> float:
        return self.quantity if self.applied_quantity is None else self.applied_quantity


@dataclass
class Lot:
    symbol: str
    name: str
    asset_class: str
    account: str
    currency: str
    quantity: float = 0.0
    cost_native: float = 0.0        # 取得原価（銘柄の通貨建て・手数料なし）
    cost_base_gross: float = 0.0    # 取得原価（基準通貨建て・手数料なし）
    fees_base: float = 0.0          # 保有分にかかった手数料（基準通貨建て）

    @property
    def avg_price_native(self) -> float:
        return self.cost_native / self.quantity if self.quantity > EPS else 0.0

    @property
    def avg_fx(self) -> float:
        """平均取得為替。holdings.yaml の cost_fx_rate にそのまま使える。"""
        return self.cost_base_gross / self.cost_native if self.cost_native > EPS else 1.0

    @property
    def cost_base(self) -> float:
        return self.cost_base_gross + self.fees_base


@dataclass
class Ledger:
    transactions: list[Transaction] = field(default_factory=list)
    lots: dict[tuple[str, str], Lot] = field(default_factory=dict)
    realized_total: float = 0.0
    dividends_total: float = 0.0
    fees_total: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def open_lots(self) -> list[Lot]:
        return [l for l in self.lots.values() if l.quantity > EPS]

    @property
    def flow_mode(self) -> str:
        has_external = any(t.type in ("deposit", "withdraw") for t in self.transactions)
        return "external" if has_external else "trade"


def _to_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def load(path: str | Path, base_currency: str = "JPY") -> Ledger:
    path = Path(path)
    led = Ledger()
    if not path.exists():
        led.warnings.append(f"{path.name} がありません")
        return led

    base = base_currency.upper()
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    parsed: list[Transaction] = []

    for i, raw in enumerate(cfg.get("transactions") or []):
        where = f"transactions[{i}]"
        raw = raw or {}
        d = _to_date(raw.get("date"))
        if d is None:
            led.warnings.append(f"{where}: date を読めないため飛ばしました")
            continue
        typ = str(raw.get("type", "")).lower()
        if typ not in TYPES:
            led.warnings.append(f"{where}: type '{typ}' が不正です（{'/'.join(TYPES)}）。飛ばしました")
            continue

        symbol = str(raw.get("symbol", "")).strip()
        currency = str(raw.get("currency") or base).upper()
        needs_symbol = typ in ("buy", "sell", "dividend")
        if needs_symbol and not symbol:
            led.warnings.append(f"{where}: symbol が未設定のため飛ばしました")
            continue

        fx = raw.get("fx_rate")
        if typ in ("deposit", "withdraw"):
            fx_rate = 1.0
        elif currency == base:
            fx_rate = 1.0
        elif fx is None:
            led.warnings.append(
                f"{where}: {symbol} は外貨建て（{currency}）ですが fx_rate がありません。"
                f"1 倍とみなすと損益が狂うため、この取引は飛ばしました"
            )
            continue
        else:
            fx_rate = float(fx)

        try:
            qty = float(raw.get("quantity", 0) or 0)
            price = float(raw.get("price", 0) or 0)
            amount = float(raw.get("amount", 0) or 0)
            fee = float(raw.get("fee", 0) or 0)
        except (TypeError, ValueError):
            led.warnings.append(f"{where}: 数値を読めないため飛ばしました")
            continue

        if typ in ("buy", "sell") and (qty <= 0 or price <= 0):
            led.warnings.append(f"{where}: {symbol} の quantity / price が 0 以下のため飛ばしました")
            continue
        if typ in ("dividend", "deposit", "withdraw") and amount <= 0:
            led.warnings.append(f"{where}: amount が 0 以下のため飛ばしました")
            continue

        parsed.append(Transaction(
            date=d, type=typ, symbol=symbol, name=str(raw.get("name", symbol)),
            asset_class=str(raw.get("asset_class", "")), account=str(raw.get("account", "")),
            quantity=qty, price=price, currency=currency, fx_rate=fx_rate, fee=fee, amount=amount,
        ))

    parsed.sort(key=lambda t: t.date)   # 安定ソート。同日はファイルの順

    for i, t in enumerate(parsed):
        key = (t.symbol, t.account)
        if t.type == "buy":
            lot = led.lots.setdefault(key, Lot(t.symbol, t.name, t.asset_class,
                                               t.account, t.currency))
            native = t.quantity * t.price
            lot.quantity += t.quantity
            lot.cost_native += native
            lot.cost_base_gross += native * t.fx_rate
            lot.fees_base += t.fee
            led.fees_total += t.fee
        elif t.type == "sell":
            lot = led.lots.get(key)
            if lot is None or lot.quantity <= EPS:
                led.warnings.append(f"{t.date} {t.symbol}: 保有が無いのに売却があります。飛ばしました")
                parsed[i] = replace(t, applied_quantity=0.0)
                continue
            qty = t.quantity
            if qty > lot.quantity + EPS:
                led.warnings.append(
                    f"{t.date} {t.symbol}: 売却数量 {qty:g} が保有 {lot.quantity:g} を超えています。"
                    f"保有数量までに切り詰めました"
                )
                qty = lot.quantity
                parsed[i] = replace(t, applied_quantity=qty)
            frac = qty / lot.quantity
            cost_gross = lot.cost_base_gross * frac
            cost_fee = lot.fees_base * frac
            proceeds = qty * t.price * t.fx_rate
            led.realized_total += proceeds - cost_gross - cost_fee - t.fee
            led.fees_total += t.fee
            lot.quantity -= qty
            lot.cost_native *= (1 - frac)
            lot.cost_base_gross -= cost_gross
            lot.fees_base -= cost_fee
        elif t.type == "dividend":
            led.dividends_total += t.amount * t.fx_rate

    led.transactions = parsed
    return led


def cash_flows(led: Ledger, current_value: float, as_of: date | None = None) -> list[tuple[date, float]]:
    """投資家から見たキャッシュフロー（入れる = 負、戻る = 正）＋現在の評価額。"""
    flows: list[tuple[date, float]] = []
    external = led.flow_mode == "external"

    for t in led.transactions:
        if external:
            if t.type == "deposit":
                flows.append((t.date, -t.amount))
            elif t.type == "withdraw":
                flows.append((t.date, t.amount))
        else:
            if t.type == "buy":
                flows.append((t.date, -(t.quantity * t.price * t.fx_rate + t.fee)))
            elif t.type == "sell":
                # 飛ばした売却は台帳に何も起きていないので、手数料も含めて出入りにしない
                if t.effective_quantity <= EPS:
                    continue
                flows.append((t.date, t.effective_quantity * t.price * t.fx_rate - t.fee))
            elif t.type == "dividend":
                flows.append((t.date, t.amount * t.fx_rate))

    flows.append((as_of or date.today(), current_value))
    return sorted(flows, key=lambda f: f[0])


def xirr(flows: list[tuple[date, float]]) -> float | None:
    """年率の内部収益率。符号が揃っていて解が無いときは None。"""
    if len(flows) < 2:
        return None
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None

    d0 = flows[0][0]

    def npv(rate: float) -> float:
        return sum(a / (1.0 + rate) ** ((d - d0).days / 365.0) for d, a in flows)

    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0
