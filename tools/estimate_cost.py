"""Jev のトークン消費量とコストの見積り。単体で動く（LLM も API キーも不要）。

    python tools/estimate_cost.py

config/strategies.yaml を実際にパースして「1 リクエストで何トークンか」を数える。
戦略を書き換えたら、再実行すれば数字も変わる。

トークン数は推定値。Jev のトークナイザは非公開なので、
日本語（非 ASCII）1 文字 = 1 トークン、ASCII 4 文字 = 1 トークンで換算している。
実測とは ±30% ほどずれうる。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.jev import USD_PER_1M_INPUT  # noqa: E402
from src.state_schema import sample_state  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

JPY_PER_USD = 150.0
ENVELOPE_TOKENS = 15          # model / state / questions のキー等
SYMBOLS = 4
TRADING_DAYS_PER_MONTH = 21


def tokens(text: str) -> int:
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return non_ascii + math.ceil((len(text) - non_ascii) / 4)


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def yen(t: float) -> float:
    return t / 1_000_000 * USD_PER_1M_INPUT * JPY_PER_USD


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "strategies.yaml").read_text(encoding="utf-8"))
    strategies = cfg["strategies"]

    questions: dict[str, dict] = {}
    per_strategy: dict[str, int] = {}
    for sname, s in strategies.items():
        qs = {f"{sname}.{qid}": q for qid, q in s["questions"].items()}
        questions.update(qs)
        per_strategy[sname] = tokens(dump(qs))

    state_t = tokens(dump(sample_state(anonymize=cfg.get("anonymize", True))))
    q_t = tokens(dump(questions))
    total = state_t + q_t + ENVELOPE_TOKENS

    print("== 1 リクエストの内訳（1 銘柄 × 1 時点。全戦略の全質問を含む）==")
    print(f"  戦略 {len(strategies)} / 質問 {len(questions)}")
    print(f"  state      : {state_t:>6,} tok")
    print(f"  questions  : {q_t:>6,} tok  ← 毎回同じ内容。支配項（{q_t / total:.0%}）")
    print(f"  envelope   : {ENVELOPE_TOKENS:>6,} tok")
    print(f"  合計       : {total:>6,} tok   出力 0（Jev は出力無料）")
    print(f"  単価       : {yen(total):.4f} 円 / リクエスト "
          f"(${total / 1_000_000 * USD_PER_1M_INPUT:.6f}、{JPY_PER_USD:.0f} 円/ドル換算)")
    print()
    print("  戦略別の questions:")
    for name, t in per_strategy.items():
        print(f"    {name:<16} {t:>5,} tok")
    print()

    scenarios = [
        ("日足 2 年 / 4 銘柄", 500 * 4),
        ("日足 10 年 / 10 銘柄", 2500 * 10),
        ("1 時間足 2 年 / 4 銘柄", 3276 * 4),
        ("5 分足 1 年 / 4 銘柄", 19656 * 4),
        ("5 分足 1 年 / 20 銘柄", 19656 * 20),
    ]
    print("== バックテスト ==")
    print(f"  {'シナリオ':<24}{'リクエスト':>12}{'トークン':>16}{'費用(円)':>12}")
    for label, n in scenarios:
        t = n * total
        print(f"  {label:<20}{n:>12,}{t:>16,}{yen(t):>12,.0f}")
    print()

    live = [
        ("日足・引け後に 1 回", TRADING_DAYS_PER_MONTH * SYMBOLS),
        ("1 時間足・足確定ごと", TRADING_DAYS_PER_MONTH * 6 * SYMBOLS),
        ("5 分足・足確定ごと", TRADING_DAYS_PER_MONTH * 78 * SYMBOLS),
        ("60 秒ポーリング（非推奨）", TRADING_DAYS_PER_MONTH * 390 * SYMBOLS),
    ]
    print(f"== リアルタイム運用（月額・{SYMBOLS} 銘柄）==")
    print(f"  {'頻度':<24}{'リクエスト/月':>14}{'費用/月(円)':>14}")
    for label, n in live:
        print(f"  {label:<20}{n:>14,}{yen(n * total):>14,.0f}")
    print()
    print("注: state が変わらなければ判断も変わらない。state のハッシュが変わったときだけ")
    print("    呼べば、日足戦略は実質「引け後に 1 回」に収束する（src/jev.py のキャッシュ）。")


if __name__ == "__main__":
    main()
