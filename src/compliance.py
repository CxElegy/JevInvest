"""独立性（independence）の確認状況の進行管理。

**このツールは、ある銘柄が制限対象かどうかを判定しない。**
唯一の正は DESC と Independence & Conflicts Network で、そのリストは機密かつ更新される。
ここで持つのは「確認したか・いつ確認したか・事前承認は取れたか」の記録だけ。

記録は config/compliance.yaml（.gitignore 済み）:

    recheck_after_days: 90
    securities:
      - symbol: VT
        desc_result: clear           # clear / restricted / escalate / unchecked
        desc_checked: 2026-09-01     # DESC で確認した日
        preclearance: approved       # approved / not_required / pending / not_submitted
        preclearance_expires: 2026-12-31   # 任意。過ぎたら承認待ちに戻す
        note: 任意のメモ

状態（state）:
    blocked    DESC で制限事業体と確認済み。保有も購入も不可
    escalate   要照会。可否が分からないので保有も購入も不可扱い
    unchecked  DESC 未確認（記録なし・確認日なしを含む）
    stale      確認が古い。再確認するまで購入不可
    pending    DESC は問題なしだが、事前承認が未申請・申請中・期限切れ
    ok         DESC 確認済みで事前承認も済んでいる。購入可
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml

DEFAULT_RECHECK_DAYS = 90
STATES = ("blocked", "escalate", "unchecked", "stale", "pending", "ok")

_PRECLEARED = {"approved", "not_required"}


@dataclass(frozen=True)
class Verdict:
    symbol: str
    state: str
    can_hold: bool
    can_buy: bool
    reason: str


def _to_date(value, warnings: list[str], where: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        warnings.append(f"{where}: 日付 '{value}' を読めません（YYYY-MM-DD で記入）")
        return None


@dataclass
class ComplianceRegistry:
    records: dict[str, dict] = field(default_factory=dict)
    recheck_after_days: int = DEFAULT_RECHECK_DAYS
    warnings: list[str] = field(default_factory=list)
    today: date | None = None          # テスト用に固定できる

    def _today(self) -> date:
        return self.today or date.today()

    def assess(self, symbol: str) -> Verdict:
        rec = self.records.get(symbol.upper())
        if rec is None:
            return Verdict(symbol, "unchecked", False, False, "DESC 未確認（記録なし）")

        where = f"{symbol}"
        result = str(rec.get("desc_result") or "unchecked").lower()

        if result == "restricted":
            return Verdict(symbol, "blocked", False, False, "DESC で制限事業体と確認済み")
        if result == "escalate":
            return Verdict(symbol, "escalate", False, False,
                           "要照会。Independence & Conflicts Network の回答待ち")
        if result != "clear":
            return Verdict(symbol, "unchecked", False, False, "DESC 未確認")

        checked = _to_date(rec.get("desc_checked"), self.warnings, where)
        if checked is None:
            self.warnings.append(f"{where}: desc_result が clear ですが desc_checked（確認日）が"
                                 f"ありません。未確認として扱います")
            return Verdict(symbol, "unchecked", False, False, "DESC 確認日が未記入")

        age = (self._today() - checked).days
        if age > self.recheck_after_days:
            return Verdict(symbol, "stale", True, False,
                           f"DESC 確認から {age} 日経過（しきい値 {self.recheck_after_days} 日）")

        pre = str(rec.get("preclearance") or "not_submitted").lower()
        if pre in _PRECLEARED:
            expires = _to_date(rec.get("preclearance_expires"), self.warnings, where)
            if pre == "approved" and expires is not None and expires < self._today():
                return Verdict(symbol, "pending", True, False,
                               f"事前承認の有効期限切れ（{expires}）")
            label = "事前承認は不要" if pre == "not_required" else "事前承認済み"
            return Verdict(symbol, "ok", True, True, f"DESC 確認済み・{label}")

        label = "申請中" if pre == "pending" else "未申請"
        return Verdict(symbol, "pending", True, False, f"DESC 確認済み・事前承認 {label}")

    def can_buy(self, symbol: str) -> bool:
        return self.assess(symbol).can_buy

    def can_hold(self, symbol: str) -> bool:
        return self.assess(symbol).can_hold


def load(path: str | Path, today: date | None = None) -> ComplianceRegistry:
    path = Path(path)
    if not path.exists():
        return ComplianceRegistry(
            warnings=[f"確認記録 {path.name} がありません。すべて未確認として扱います"
                      f"（config/compliance.example.yaml をコピーして記録を始めてください）"],
            today=today,
        )

    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    warnings: list[str] = []
    records: dict[str, dict] = {}

    for i, rec in enumerate(cfg.get("securities") or []):
        sym = str((rec or {}).get("symbol") or "").strip()
        if not sym:
            warnings.append(f"securities[{i}]: symbol が未設定です")
            continue
        if sym.upper() in records:
            warnings.append(f"{sym}: 記録が重複しています。後のものを採用します")
        records[sym.upper()] = rec

    return ComplianceRegistry(
        records=records,
        recheck_after_days=int(cfg.get("recheck_after_days", DEFAULT_RECHECK_DAYS)),
        warnings=warnings,
        today=today,
    )


def summarize(reg: ComplianceRegistry, symbols: list[str]) -> dict:
    """銘柄を状態ごとに振り分ける。次に何を申請すべきかの元データ。"""
    buckets: dict[str, list[tuple[str, Verdict]]] = {}
    for sym in symbols:
        v = reg.assess(sym)
        buckets.setdefault(v.state, []).append((sym, v))
    return {"buckets": buckets, "counts": {s: len(buckets.get(s, [])) for s in STATES}}
