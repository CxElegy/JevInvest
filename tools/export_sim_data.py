"""ブラウザ版シミュレーター用に、実際の日足を 1 つの JSON に書き出す。

    python tools/export_sim_data.py            # web/simdata.json

中身は src/sim.py の UNIVERSE と USDJPY の日足（Yahoo Finance）。
  - adj: 配当調整後終値（バックテストと同じ価格。分配金は再投資扱い）
  - 日付はすべての銘柄の和集合。その銘柄の休場日は null（約定はその次の足に持ち越す）
バックテストの Python 版（tools/papertrade.py backtest）と同じ MarketData から取るので、
同じ数字になる（tests/test_sim_parity.py で確かめる）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from papertrade import fetch_bars  # noqa: E402
from src.sim import FX_SYMBOL, UNIVERSE, Prices  # noqa: E402

OUT = ROOT / "web" / "simdata.json"


def to_payload(prices: Prices, source: str) -> dict:
    """Prices を、日付の和集合 + 銘柄ごとの配列（休場は null）にする。"""
    cal = prices.calendar()
    sym = {}
    for s in UNIVERSE:
        ser = prices.series[s]
        sym[s] = [round(float(ser[d]), 4) if d in ser.index else None for d in cal]
    # 為替は「その日以前で最新」（Python 版の fx_at と同じ。為替だけ動いた日も取りこぼさない）
    fx = [prices.fx_at(d) for d in cal]
    return {
        "source": source,
        "dates": [d.strftime("%Y-%m-%d") for d in cal],
        "symbols": UNIVERSE,
        "adj": sym,
        "fx": [None if v is None else round(float(v), 4) for v in fx],
    }


def main() -> None:
    hist = fetch_bars(years=18.5)
    prices = Prices({s: hist[s]["adj_close"] for s in UNIVERSE}, hist[FX_SYMBOL]["close"])
    data = to_payload(prices, "Yahoo Finance 日足（配当調整後終値・USDJPY 終値）")
    OUT.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"→ {OUT.relative_to(ROOT)}（{len(data['dates'])} 営業日、{OUT.stat().st_size / 1024:.0f} KB）"
          f" {data['dates'][0]} 〜 {data['dates'][-1]}")


if __name__ == "__main__":
    main()
