"""持仓台帳の読み込みと検証。

金額の入るファイルなので config/holdings.yaml は .gitignore してある。
検証で落とすのは「後で静かに間違った数字を出す」のを防ぐため。
台帳の入力ミスは分析の全部を汚染するので、ここは厳しめに見る。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

CASH_PREFIX = "CASH-"
FUND_PREFIX = "FUND-"


class HoldingsError(ValueError):
    pass


@dataclass(frozen=True)
class Position:
    symbol: str
    name: str
    asset_class: str
    account: str
    quantity: float
    cost_basis_per_unit: float   # 取得単価。その銘柄の通貨建て
    currency: str
    # 取得時の為替（この通貨 1 単位 = 基準通貨いくら）。
    # 外貨建て資産で、値上がり益と為替差益を分離したい場合に指定する。
    cost_fx_rate: float | None = None
    price_override: float | None = None
    price_as_of: str | None = None

    @property
    def is_cash(self) -> bool:
        return self.symbol.startswith(CASH_PREFIX)

    @property
    def is_fund(self) -> bool:
        """投信など、市場から価格が取れず手入力に頼るもの。"""
        return self.symbol.startswith(FUND_PREFIX)

    @property
    def needs_quote(self) -> bool:
        """外部から価格を取りに行く必要があるか。"""
        return not self.is_cash and self.price_override is None

    @property
    def cost_basis_native(self) -> float:
        """取得原価（その銘柄の通貨建て）。"""
        return self.quantity * self.cost_basis_per_unit


@dataclass
class Account:
    name: str
    type: str


@dataclass
class Holdings:
    base_currency: str
    target_allocation: dict[str, float]
    accounts: list[Account]
    positions: list[Position]
    guards: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def symbols_needing_quote(self) -> list[str]:
        return sorted({p.symbol for p in self.positions if p.needs_quote})

    @property
    def symbols_needing_compliance(self) -> list[str]:
        """independence の確認対象となる銘柄。

        現金は証券ではないので対象外。ここを外さないと毎回 CASH-JPY が
        「DESC 未確認」として並び、本当に確認が必要な銘柄が埋もれる。
        """
        return sorted({p.symbol for p in self.positions if not p.is_cash})

    @property
    def currencies(self) -> list[str]:
        return sorted({p.currency for p in self.positions})

    @property
    def asset_classes(self) -> list[str]:
        return sorted({p.asset_class for p in self.positions})


def load(path: str | Path) -> Holdings:
    path = Path(path)
    if not path.exists():
        raise HoldingsError(
            f"{path} が見つかりません。"
            f"config/holdings.example.yaml をコピーして作成してください。"
        )

    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    warnings: list[str] = []

    base = cfg.get("base_currency")
    if not base:
        raise HoldingsError("base_currency が未設定です")

    target = {k: float(v) for k, v in (cfg.get("target_allocation") or {}).items()}
    if target:
        total = sum(target.values())
        if abs(total - 100.0) > 0.01:
            warnings.append(
                f"target_allocation の合計が {total:.2f}% です（100% になるべき）"
            )

    accounts = [Account(name=a["name"], type=a.get("type", "taxable"))
                for a in (cfg.get("accounts") or [])]
    known_accounts = {a.name for a in accounts}

    raw_positions = cfg.get("positions") or []
    if not raw_positions:
        raise HoldingsError("positions が空です")

    positions: list[Position] = []
    seen: set[tuple[str, str]] = set()

    for i, p in enumerate(raw_positions):
        where = f"positions[{i}]"
        for req in ("symbol", "asset_class", "account", "quantity", "currency"):
            if req not in p:
                raise HoldingsError(f"{where}: {req} が未設定です")

        symbol = str(p["symbol"])
        account = str(p["account"])

        # 同じ銘柄を同じ口座で 2 行に分けると集計が二重になる。
        # 分けたいなら口座を分けるか、1 行に平均取得単価でまとめる。
        key = (symbol, account)
        if key in seen:
            raise HoldingsError(
                f"{where}: {symbol} が口座 {account} で重複しています。"
                f"1 行にまとめて平均取得単価で記載してください"
            )
        seen.add(key)

        if account not in known_accounts:
            warnings.append(f"{where}: 口座 '{account}' が accounts に未定義です")

        qty = float(p["quantity"])
        if qty < 0:
            raise HoldingsError(f"{where}: quantity が負です（ショートは未対応）")
        if qty == 0:
            warnings.append(f"{where}: {symbol} の quantity が 0 です")

        cost = float(p.get("cost_basis_per_unit", 0.0))
        if cost <= 0 and qty > 0:
            warnings.append(
                f"{where}: {symbol} の cost_basis_per_unit が未設定または 0 です。"
                f"損益が正しく出ません"
            )

        override = p.get("price_override")
        cost_fx = p.get("cost_fx_rate")
        currency = str(p["currency"]).upper()

        pos = Position(
            symbol=symbol,
            name=str(p.get("name", symbol)),
            asset_class=str(p["asset_class"]),
            account=account,
            quantity=qty,
            cost_basis_per_unit=cost,
            currency=currency,
            cost_fx_rate=float(cost_fx) if cost_fx is not None else None,
            price_override=float(override) if override is not None else None,
            price_as_of=p.get("price_as_of"),
        )

        if currency != str(base).upper() and cost_fx is None:
            warnings.append(
                f"{where}: {symbol} は外貨建てですが cost_fx_rate が未設定です。"
                f"値上がり益と為替差益を分離できません"
            )

        if pos.is_fund and pos.price_override is None:
            warnings.append(
                f"{where}: {symbol} は投信扱いですが price_override がありません。"
                f"投信協会の CSV から基準価額を転記してください"
            )

        positions.append(pos)

    # 台帳にある資産クラスが target_allocation に無いと、
    # リバランス提案で「目標 0%」として扱われてしまう。
    if target:
        for ac in sorted({p.asset_class for p in positions}):
            if ac not in target:
                warnings.append(
                    f"資産クラス '{ac}' が target_allocation に未定義です（目標 0% 扱いになります）"
                )

    guards = {k: v for k, v in (cfg.get("guards") or {}).items()}

    return Holdings(
        base_currency=str(base).upper(),
        target_allocation=target,
        accounts=accounts,
        positions=positions,
        guards=guards,
        warnings=warnings,
    )
