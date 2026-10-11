"""証券会社の取引履歴 CSV を transactions.yaml の書式に変換する。

    python -m src.cli import-csv ~/Downloads/tradehistory(JP)_20261011.csv
    python -m src.cli import-csv a.csv b.csv --broker rakuten_us

対応している書式（列名で自動判定。--broker で明示もできる）:

    sbi_jp        SBI 証券  国内株式 約定履歴
    rakuten_jp    楽天証券  国内株式 取引履歴（tradehistory(JP)）
    rakuten_us    楽天証券  米国株式 取引履歴（tradehistory(US)）
    rakuten_fund  楽天証券  投資信託 取引履歴（tradehistory(INVST)）

列名は各社が公開している CSV の見出しに合わせてあるが、証券会社は予告なく
列を変えることがある。必要な列が見つからなければ、どの列が無いかを出して止める
（列の位置で推測して読み進めない）。

黙って直さないための決まり:
  - 買付・売付以外の行（信用取引、入出庫、分割、分配金の再投資など）は取り込まず、
    種類ごとの件数を警告に出す。
  - 外貨建てで為替レートが空の行は取り込まない（transactions.py と同じ方針）。
  - 資産クラスは銘柄だけでは決まらない。import_map に無い銘柄は既定値
    （国内株 → jp_equity、米国株 → us_equity、投信 → 空）を入れ、推測したことを警告に出す。
  - 既存の transactions.yaml と同じ取引は二重に取り込まない。

出力は config/transactions.imported.yaml（.gitignore 済み）。transactions.yaml は書き換えない。
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

# ------------------------------------------------------------------ 書式の定義

# canonical 名 → 見出しの候補（NFKC 正規化と空白除去の後で比べる）
Columns = dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    columns: Columns
    required: tuple[str, ...]
    kind: str               # jp_stock / us_stock / fund
    currency: str           # 約定単価の通貨
    fee_in_native: bool = False   # 手数料が約定通貨建てか（True なら為替で円に直す）


PRESETS: dict[str, Preset] = {
    p.key: p
    for p in (
        Preset(
            key="sbi_jp", label="SBI 証券 国内株式 約定履歴", kind="jp_stock", currency="JPY",
            columns={
                "date": ("約定日",),
                "code": ("銘柄コード",),
                "name": ("銘柄",),
                "side": ("取引",),
                "account": ("預り",),
                "quantity": ("約定数量",),
                "price": ("約定単価",),
                "fee": ("手数料/諸経費等",),
            },
            required=("date", "code", "side", "account", "quantity", "price"),
        ),
        Preset(
            key="rakuten_jp", label="楽天証券 国内株式 取引履歴", kind="jp_stock", currency="JPY",
            columns={
                "date": ("約定日",),
                "code": ("銘柄コード",),
                "name": ("銘柄名",),
                "side": ("売買区分",),
                "trade_kind": ("取引区分",),
                "account": ("口座区分",),
                "quantity": ("数量[株]",),
                "price": ("単価[円]",),
                "fee": ("手数料[円]",),
                "fee2": ("諸費用[円]",),
            },
            required=("date", "code", "side", "account", "quantity", "price"),
        ),
        Preset(
            key="rakuten_us", label="楽天証券 米国株式 取引履歴", kind="us_stock", currency="USD",
            fee_in_native=True,
            columns={
                "date": ("約定日",),
                "code": ("ティッカー",),
                "name": ("銘柄名",),
                "side": ("売買区分",),
                "trade_kind": ("取引区分",),
                "account": ("口座",),
                "quantity": ("数量[株]",),
                "price": ("単価[USドル]",),
                "fee": ("手数料[USドル]",),
                "fx": ("為替レート",),
            },
            required=("date", "code", "side", "account", "quantity", "price", "fx"),
        ),
        Preset(
            key="rakuten_fund", label="楽天証券 投資信託 取引履歴", kind="fund", currency="JPY",
            columns={
                "date": ("約定日",),
                "name": ("ファンド名",),
                "side": ("取引",),
                "account": ("口座",),
                "quantity": ("数量[口]",),
                "price": ("単価", "単価[円]"),
                "fee": ("経費", "経費[円]"),
            },
            required=("date", "name", "side", "account", "quantity", "price"),
        ),
    )
}

HEADER_SCAN_ROWS = 40
FUND_UNIT = 10_000   # 投信の基準価額は 1 万口あたり


# ------------------------------------------------------------------ 結果

@dataclass
class ImportResult:
    preset: Preset | None = None
    transactions: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)      # 取り込まなかった行の種類 → 件数
    assumed_class: set[str] = field(default_factory=set)   # 資産クラスを既定値で埋めた銘柄
    unmapped_funds: set[str] = field(default_factory=set)  # import_map に無い投信


# ------------------------------------------------------------------ 読み取りの部品

def norm(text) -> str:
    """全角・半角の揺れと空白を消す。見出しの照合とセルの読み取りに使う。"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    return re.sub(r"\s+", "", s)


def decode(raw: bytes) -> str:
    """日本の証券会社の CSV は Shift_JIS（cp932）が多い。UTF-8 を先に試す。"""
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("文字コードを判定できません（UTF-8 / Shift_JIS のどちらでもない）")


def parse_number(text) -> float | None:
    s = norm(text).replace(",", "").replace("円", "").replace("$", "")
    if s in ("", "-", "--", "ー", "―"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(text) -> date | None:
    s = norm(text)
    m = re.match(r"^(\d{4})[/\-.年](\d{1,2})[/\-.月](\d{1,2})", s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _find_header(rows: list[list[str]], preset: Preset) -> tuple[int, dict[str, int]] | None:
    """必要な列がすべて揃う見出し行を探す。前置きの行（口座番号など）は飛ばす。"""
    for i, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        cells = [norm(c) for c in row]
        idx: dict[str, int] = {}
        for canon, aliases in preset.columns.items():
            for a in aliases:
                if norm(a) in cells:
                    idx[canon] = cells.index(norm(a))
                    break
        if all(r in idx for r in preset.required):
            return i, idx
    return None


def detect(rows: list[list[str]]) -> tuple[Preset, int, dict[str, int]] | None:
    """列名から書式を判定する。必須列の多い（=特定的な）書式を先に試す。"""
    for p in sorted(PRESETS.values(), key=lambda p: -len(p.required)):
        hit = _find_header(rows, p)
        if hit:
            return p, hit[0], hit[1]
    return None


# ------------------------------------------------------------------ 行の解釈

def side_of(text: str) -> str | None:
    """取引の向き。買付・売付以外（信用・入出庫・再投資など）は None。"""
    s = norm(text)
    if any(k in s for k in ("信用", "返済", "現渡", "現引", "入庫", "出庫", "分割", "再投資")):
        return None
    if "買" in s:
        return "buy"
    if "売" in s or "解約" in s:
        return "sell"
    return None


def account_of(text: str, account_map: dict[str, str]) -> str:
    s = norm(text)
    for k, v in account_map.items():
        if norm(k) and norm(k) in s:
            return v
    if "NISA" in s.upper():
        return "NISA"
    return "特定口座"


DEFAULT_ACCOUNT_MAP: dict[str, str] = {}


def load_map(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _cell(row: list[str], idx: dict[str, int], key: str) -> str:
    i = idx.get(key)
    return row[i] if i is not None and i < len(row) else ""


def convert(text: str, preset_key: str | None = None, mapping: dict | None = None,
            base_currency: str = "JPY") -> ImportResult:
    """CSV の本文を取引のリストに変換する。"""
    mapping = mapping or {}
    symbols_map: dict = mapping.get("symbols") or {}
    # 投信名は全角・半角や空白の揺れがあるので、正規化した名前で引く
    funds_map: dict = {norm(k): v for k, v in (mapping.get("funds") or {}).items()}
    account_map: dict = mapping.get("accounts") or DEFAULT_ACCOUNT_MAP

    res = ImportResult()
    rows = list(csv.reader(io.StringIO(text)))

    if preset_key:
        preset = PRESETS.get(preset_key)
        if preset is None:
            res.warnings.append(f"書式 '{preset_key}' は未対応です（{', '.join(PRESETS)}）")
            return res
        hit = _find_header(rows, preset)
        if hit is None:
            missing = _missing_columns(rows, preset)
            res.warnings.append(
                f"{preset.label} の見出し行が見つかりません。無い列: {', '.join(missing)}。"
                "証券会社が書式を変えた可能性があります"
            )
            return res
        header_row, idx = hit
    else:
        found = detect(rows)
        if found is None:
            res.warnings.append(
                "対応している書式の見出し行が見つかりません。"
                f"対応: {', '.join(p.label for p in PRESETS.values())}。"
                "--broker で書式を指定すると、どの列が足りないかを表示します"
            )
            return res
        preset, header_row, idx = found
    res.preset = preset

    for n, row in enumerate(rows[header_row + 1:], start=header_row + 2):
        if not any(norm(c) for c in row):
            continue
        d = parse_date(_cell(row, idx, "date"))
        if d is None:
            # 合計行や注記行。日付の無い行は取引ではない
            continue

        side_text = _cell(row, idx, "side") + _cell(row, idx, "trade_kind")
        side = side_of(side_text)
        if side is None:
            res.skipped[norm(side_text) or "（取引種別なし）"] += 1
            continue

        qty = parse_number(_cell(row, idx, "quantity"))
        price = parse_number(_cell(row, idx, "price"))
        where = f"{n} 行目"
        if not qty or not price or qty <= 0 or price <= 0:
            res.warnings.append(f"{where}: 数量または単価を読めないため飛ばしました")
            continue

        # 約定単価の通貨。楽天の「決済通貨」は円貨決済か外貨決済かであって、
        # 単価の通貨ではない（円貨決済でも単価は USD）。だから見ない。
        currency = preset.currency
        fx = parse_number(_cell(row, idx, "fx")) if "fx" in idx else None
        if currency != base_currency and fx is None:
            res.warnings.append(
                f"{where}: 外貨建て（{currency}）ですが為替レートが空です。"
                "1 倍とみなすと損益が狂うため飛ばしました"
            )
            continue

        fee = (parse_number(_cell(row, idx, "fee")) or 0.0) + \
              (parse_number(_cell(row, idx, "fee2")) or 0.0)
        if preset.fee_in_native and currency != base_currency:
            fee *= fx   # transactions.yaml の fee は基準通貨建て

        name = norm(_cell(row, idx, "name")) or ""
        account = account_of(_cell(row, idx, "account"), account_map)

        if preset.kind == "fund":
            entry = funds_map.get(name) or {}
            symbol = entry.get("symbol") or f"FUND-{name}"
            asset_class = entry.get("asset_class", "")
            if not entry:
                res.unmapped_funds.add(name)
            price = price / FUND_UNIT   # 1 口あたりに直す（holdings.yaml と同じ単位）
        else:
            code = norm(_cell(row, idx, "code")).upper()
            if preset.kind == "jp_stock" and not code.endswith(".T"):
                code = f"{code}.T"
            symbol = code
            entry = symbols_map.get(symbol) or {}
            default_class = "jp_equity" if preset.kind == "jp_stock" else "us_equity"
            asset_class = entry.get("asset_class") or default_class
            if "asset_class" not in entry:
                res.assumed_class.add(symbol)
            name = entry.get("name") or name

        tx = {
            "date": d.isoformat(),
            "type": side,
            "symbol": symbol,
            "name": name,
            "asset_class": asset_class,
            "account": account,
            "quantity": qty,
            "price": round(price, 6),
            "currency": currency,
        }
        if currency != base_currency:
            tx["fx_rate"] = fx
        tx["fee"] = round(fee, 2)
        res.transactions.append(tx)

    if res.skipped:
        detail = ", ".join(f"{k} {v} 件" for k, v in res.skipped.most_common())
        res.warnings.append(f"買付・売付以外の行は取り込んでいません: {detail}")
    if res.assumed_class:
        res.warnings.append(
            "資産クラスを既定値で入れた銘柄があります（ETF などは違う可能性あり）: "
            + ", ".join(sorted(res.assumed_class))
            + "。config/import_map.yaml の symbols に書くと次回から使われます"
        )
    if res.unmapped_funds:
        res.warnings.append(
            "import_map に無い投信は symbol を FUND-<ファンド名>、資産クラスを空にしました: "
            + ", ".join(sorted(res.unmapped_funds))
            + "。config/import_map.yaml の funds に書いてください"
        )
    return res


def _missing_columns(rows: list[list[str]], preset: Preset) -> list[str]:
    """見出しらしい行（必須列が最も多く揃う行）で、足りない列名を返す。"""
    best: set[str] = set()
    for row in rows[:HEADER_SCAN_ROWS]:
        cells = {norm(c) for c in row}
        hit = {k for k in preset.required
               if any(norm(a) in cells for a in preset.columns[k])}
        if len(hit) > len(best):
            best = hit
    return [preset.columns[k][0] for k in preset.required if k not in best]


# ------------------------------------------------------------------ 重複と書き出し

def tx_key(t: dict) -> tuple:
    return (
        str(t.get("date")), str(t.get("type")), str(t.get("symbol")), str(t.get("account")),
        round(float(t.get("quantity", 0) or 0), 6), round(float(t.get("price", 0) or 0), 6),
    )


def drop_existing(new: list[dict], existing_path: Path) -> tuple[list[dict], int]:
    """既に transactions.yaml にある取引と、取り込み内の重複を落とす。

    同じ日に同じ銘柄を同じ数量・単価で 2 回約定することはありうる。
    そのため件数で照合する（既存 1 件 + CSV 2 件 なら 1 件だけ追加する）。
    """
    seen: Counter = Counter()
    if existing_path.exists():
        cfg = yaml.safe_load(existing_path.read_text(encoding="utf-8")) or {}
        for t in cfg.get("transactions") or []:
            if t:
                seen[tx_key(t)] += 1
    out: list[dict] = []
    dropped = 0
    for t in new:
        k = tx_key(t)
        if seen[k] > 0:
            seen[k] -= 1
            dropped += 1
        else:
            out.append(t)
    return out, dropped


def dump(transactions: list[dict]) -> str:
    head = (
        "# 証券会社の CSV から取り込んだ取引（python -m src.cli import-csv）。\n"
        "# 内容を確認して config/transactions.yaml の transactions に追記する。\n"
        "# このファイルは毎回上書きされる。金額が入るのでコミットしない。\n\n"
    )
    body = yaml.safe_dump({"transactions": sorted(transactions, key=lambda t: t["date"])},
                          allow_unicode=True, sort_keys=False, default_flow_style=False)
    return head + body
