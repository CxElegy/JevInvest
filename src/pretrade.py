"""売買する前の点検。「やる理由」ではなく「やめる理由」を先に全部出す。

    python -m src.cli check-trade buy NVDA --amount 300000 --asset-class us_equity --account NISA
    python -m src.cli check-trade sell 7203.T --pct 2 --account 特定口座

見るもの（重い順）:
  1. independence  購入可と記録されていない銘柄は買わない（判定はしない。記録を見るだけ）
  2. 目標配分      その売買で目標から離れるか近づくか
  3. ガードレール  1 銘柄の上限・現金の下限
  4. 税と口座      NISA の年間枠の残り、課税口座で売るときの概算税、NISA で損失を出す売却
  5. 行動          初めて考えた売買か（24 時間置く）、直近 30 日の売買回数、買ってすぐの売却

「やめる」が 1 つでもあれば実行しない。「注意」は理由を読んで本人が決める。
発注はしない。結果を見て本人が証券会社で操作する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

TAX_RATE = 0.20315
COOLING_HOURS = 24
RECENT_DAYS = 30
RECENT_TRADES_WARN = 4
SHORT_HOLD_DAYS = 30

STOP, CAUTION, OK, INFO = "stop", "caution", "ok", "info"
MARK = {STOP: "⛔ やめる", CAUTION: "⚠ 注意", OK: "✅", INFO: "ℹ"}


@dataclass(frozen=True)
class Check:
    level: str
    title: str
    detail: str


@dataclass(frozen=True)
class Intent:
    side: str            # buy / sell
    symbol: str
    amount: float        # 基準通貨建て
    asset_class: str
    account: str


def verdict(checks: list[Check]) -> str:
    if any(c.level == STOP for c in checks):
        return "実行しない（やめる理由があります）"
    if any(c.level == CAUTION for c in checks):
        return "注意点を読んでから本人が決める"
    return "計画と矛盾しない"


# ------------------------------------------------------------------ 個別の点検

def check_independence(intent: Intent, reg) -> Check:
    v = reg.assess(intent.symbol)
    if intent.side == "buy":
        if v.can_buy:
            return Check(OK, "independence", f"{intent.symbol}: {v.reason}")
        return Check(STOP, "independence",
                     f"{intent.symbol}: {v.reason}。購入可と記録されるまで買わない"
                     "（判定の正は DESC と Independence & Conflicts Network）")
    if v.state in ("blocked", "escalate"):
        return Check(CAUTION, "independence",
                     f"{intent.symbol}: {v.reason}。売却の手順も Independence に確認する")
    return Check(INFO, "independence", f"{intent.symbol}: {v.reason}")


def _class_pct(a, cls: str) -> float:
    if a.by_asset_class is None or a.by_asset_class.empty or cls not in a.by_asset_class.index:
        return 0.0
    return float(a.by_asset_class.loc[cls, "評価額"]) / a.total_value_base * 100.0


def check_allocation(intent: Intent, a, h) -> list[Check]:
    out: list[Check] = []
    total = a.total_value_base
    if a.unpriced_count:
        out.append(Check(CAUTION, "評価",
                         f"評価不能の保有が {a.unpriced_count} 件あり、比率は不正確です"))
    if total <= 0:
        out.append(Check(STOP, "評価", "評価額が 0 です。台帳を先に埋める"))
        return out

    sign = 1 if intent.side == "buy" else -1
    cls = intent.asset_class
    cur = _class_pct(a, cls)
    # 買付は現金から、売却は現金へ。合計額は変わらない
    after = (cur * total / 100 + sign * intent.amount) / total * 100
    tgt = h.target_allocation.get(cls)
    if tgt is None:
        out.append(Check(CAUTION, "目標配分",
                         f"{cls} は目標配分にありません（{cur:.1f}% → {after:.1f}%）"))
    else:
        before_gap, after_gap = abs(cur - tgt), abs(after - tgt)
        if after_gap > before_gap + 1e-9:
            out.append(Check(CAUTION, "目標配分",
                             f"{cls} が目標 {tgt:.0f}% から離れる（{cur:.1f}% → {after:.1f}%）"))
        else:
            out.append(Check(OK, "目標配分",
                             f"{cls} が目標 {tgt:.0f}% に近づく（{cur:.1f}% → {after:.1f}%）"))

    # 現金の下限（買付は現金が減る）
    floor = (h.guards or {}).get("min_cash_pct")
    cash_now = sum(v.market_value_base for v in a.valuations if v.priced and v.position.is_cash)
    cash_after = cash_now - sign * intent.amount
    if intent.side == "buy" and cash_after < 0:
        out.append(Check(STOP, "現金", "現金が足りません（台帳の CASH- の合計を超える）"))
    elif floor is not None and intent.side == "buy":
        pct = cash_after / total * 100
        level = STOP if pct < float(floor) else OK
        out.append(Check(level, "現金の下限",
                         f"買付後の現金 {pct:.1f}%（下限 {float(floor):.0f}%）"))

    # 1 銘柄の上限（複数口座は合算）
    cap = (h.guards or {}).get("max_single_position_pct")
    exempt = {str(c) for c in ((h.guards or {}).get("single_position_exempt_classes") or [])}
    if cap is not None and intent.side == "buy" and cls not in exempt:
        held = sum(v.market_value_base for v in a.valuations
                   if v.priced and v.position.symbol == intent.symbol)
        pct = (held + intent.amount) / total * 100
        level = STOP if pct > float(cap) else (CAUTION if pct > float(cap) * 0.9 else OK)
        out.append(Check(level, "1 銘柄の上限",
                         f"{intent.symbol} が全体の {pct:.1f}%（上限 {float(cap):.0f}%）"))
    return out


def check_tax(intent: Intent, a, h, cfg: dict) -> list[Check]:
    out: list[Check] = []
    acct_type = next((x.type for x in h.accounts if x.name == intent.account), None)
    if acct_type is None:
        out.append(Check(CAUTION, "口座", f"口座 '{intent.account}' が台帳の accounts にありません"))
        return out

    held = [v for v in a.valuations
            if v.priced and v.position.symbol == intent.symbol
            and v.position.account == intent.account]
    mv = sum(v.market_value_base for v in held)
    pnl = sum(v.pnl_base for v in held)

    if acct_type == "nisa" and intent.side == "buy":
        nisa = ((cfg.get("contributions") or {}).get("nisa") or {})
        elig = (cfg.get("nisa_eligibility") or {}).get(intent.asset_class, "both")
        growth_left = float(nisa.get("growth_annual", 2_400_000)) - float(nisa.get("used_growth_this_year", 0))
        tsumi_left = float(nisa.get("tsumitate_annual", 1_200_000)) - float(nisa.get("used_tsumitate_this_year", 0))
        if elig == "none":
            out.append(Check(STOP, "NISA", f"{intent.asset_class} は NISA の対象外（nisa_eligibility）"))
        else:
            room = growth_left + (tsumi_left if elig == "both" else 0.0)
            level = OK if intent.amount <= room else CAUTION
            out.append(Check(level, "NISA の年間枠",
                             f"今年の残り枠 {room:,.0f}（成長 {growth_left:,.0f}"
                             + (f"、つみたて {tsumi_left:,.0f}" if elig == "both" else "") + "）"
                             + ("" if level == OK else "。超えた分は買えない")))
        out.append(Check(INFO, "NISA",
                         "NISA は売っても枠が戻るのは翌年。短期の売買に使うと非課税枠を無駄にする"))

    if intent.side == "sell" and mv > 0:
        gain = pnl * min(intent.amount / mv, 1.0)
        if acct_type == "nisa":
            if gain < 0:
                out.append(Check(CAUTION, "NISA の損失",
                                 f"NISA 内の損失 {gain:,.0f} は他の利益と相殺できず、繰越控除もできない"))
        elif gain > 0:
            out.append(Check(CAUTION, "税",
                             f"課税口座の売却。概算の譲渡益 {gain:,.0f} に約 20.315% → "
                             f"税 {gain * TAX_RATE:,.0f}。乖離を直すだけなら新規資金の配分で寄せる手もある"))
        else:
            out.append(Check(INFO, "税", f"概算の譲渡損 {gain:,.0f}（同じ年の他の利益と損益通算できる）"))
    elif intent.side == "sell":
        out.append(Check(CAUTION, "保有", f"{intent.account} に {intent.symbol} の評価済みの保有がありません"))
    return out


def check_behavior(intent: Intent, transactions: list[dict], intents_path: Path,
                   now: datetime) -> list[Check]:
    out: list[Check] = []

    # 同じ売買を初めて考えたのはいつか。初回なら記録して 24 時間置く
    log = {}
    if intents_path.exists():
        log = yaml.safe_load(intents_path.read_text(encoding="utf-8")) or {}
    key = f"{intent.side} {intent.symbol}"
    first = log.get(key)
    if first is None:
        log[key] = now.isoformat(timespec="minutes")
        intents_path.write_text(yaml.safe_dump(log, allow_unicode=True), encoding="utf-8")
        out.append(Check(CAUTION, "冷却期間",
                         f"この売買を考えたのは初めて。{COOLING_HOURS} 時間後にもう一度点検する"
                         "（それでもやりたいなら、その時に実行する）"))
    else:
        first_dt = datetime.fromisoformat(str(first))
        hours = (now - first_dt).total_seconds() / 3600
        if hours < COOLING_HOURS:
            out.append(Check(CAUTION, "冷却期間",
                             f"初めて考えてから {hours:.0f} 時間。あと {COOLING_HOURS - hours:.0f} 時間置く"))
        else:
            out.append(Check(OK, "冷却期間", f"初めて考えてから {hours / 24:.1f} 日経過"))

    today = now.date()
    recent = [t for t in transactions
              if t and t.get("type") in ("buy", "sell") and _d(t.get("date"))
              and today - _d(t.get("date")) <= timedelta(days=RECENT_DAYS)]
    if len(recent) >= RECENT_TRADES_WARN:
        out.append(Check(CAUTION, "売買の頻度",
                         f"直近 {RECENT_DAYS} 日で {len(recent)} 回売買している。"
                         "売買が多い人ほど成績が落ちる（docs/PLAYBOOK.md の Barber & Odean）"))

    if intent.side == "sell":
        buys = [_d(t.get("date")) for t in transactions
                if t and t.get("type") == "buy" and t.get("symbol") == intent.symbol]
        buys = [d for d in buys if d]
        if buys and (today - max(buys)).days < SHORT_HOLD_DAYS:
            out.append(Check(CAUTION, "保有期間",
                             f"{intent.symbol} は {(today - max(buys)).days} 日前に買ったばかり。"
                             "買った理由が崩れたのか、値動きに反応しているだけかを書き出す"))
    return out


def _d(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def run_checks(intent: Intent, a, h, cfg: dict, reg, transactions: list[dict],
               intents_path: Path, now: datetime | None = None) -> list[Check]:
    now = now or datetime.now()
    checks = [check_independence(intent, reg)]
    checks += check_allocation(intent, a, h)
    checks += check_tax(intent, a, h, cfg)
    checks += check_behavior(intent, transactions, intents_path, now)
    return checks
