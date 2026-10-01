"""積立の「続け方」で結果がどれだけ変わるかを、米国株 150 年分の月次データで検証する（backtrader 使用）。

データ: Shiller の S&P 500 月次（配当・CPI 込み）。github.com/datasets/s-and-p-500 の CSV。
配当を再投資した実質（インフレ調整後）の指数を作り、毎月同じ実質額を積み立てる。

比べる行動（すべて手数料・税は 0。現金の実質金利は 0％＝日本の普通預金に近い）:
  keep    毎月買い続ける
  panic   高値から 20％下げたら全部売って積立も止め、高値を取り戻してから再開する
  wait    下げを待つ。高値から 10％下げるまで現金でためて、下げたらまとめて買う
  cash    投資せず預金のまま

20 年間の積立を、開始月を 1 年ずつずらして全期間で回し、積立額に対する最終額の倍率を比べる。

    vendor/.venv/bin/python tools/backtest_habits.py            # web/evidence.json を書く
    vendor/.venv/bin/python tools/backtest_habits.py --years 30
"""
from __future__ import annotations

import argparse
import io
import json
import statistics
import urllib.request
from datetime import date
from pathlib import Path

import backtrader as bt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
URL = "https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv"
CACHE = ROOT / "data" / "shiller_sp500.csv"
OUT = ROOT / "web" / "evidence.json"
STRATS = ["keep", "panic", "wait", "cash"]


def load() -> pd.DataFrame:
    if not CACHE.exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(URL, timeout=60) as r:
            CACHE.write_bytes(r.read())
    df = pd.read_csv(CACHE, parse_dates=["Date"])
    # 末尾の月は配当・CPI が未確定で 0 になっている。確定している月だけ使う
    df = df[(df["Dividend"] > 0) & (df["Consumer Price Index"] > 0)].reset_index(drop=True)
    ret = (df["SP500"] + df["Dividend"] / 12) / df["SP500"].shift(1)
    infl = df["Consumer Price Index"] / df["Consumer Price Index"].shift(1)
    real = (ret / infl).fillna(1.0).cumprod()
    out = pd.DataFrame({"close": real.values}, index=df["Date"])
    for c in ("open", "high", "low"):
        out[c] = out["close"]
    out["volume"] = 0
    return out


class Habit(bt.Strategy):
    params = dict(mode="keep", monthly=1.0, drop_exit=0.20, drop_wait=0.10)

    def __init__(self):
        self.peak = 0.0
        self.out = False      # panic: 売って止めている間
        self.paid = 0.0

    def next(self):
        px = self.data.close[0]
        self.peak = max(self.peak, px)
        self.broker.add_cash(self.p.monthly)
        self.paid += self.p.monthly
        dd = 1 - px / self.peak
        m = self.p.mode
        if m == "cash":
            return
        if m == "panic":
            if not self.out and dd >= self.p.drop_exit:
                if self.position:
                    self.close()
                self.out = True
                return
            if self.out:
                if px >= self.peak * 0.999:
                    self.out = False
                else:
                    return
        if m == "wait" and dd < self.p.drop_wait:
            return
        size = self.broker.get_cash() / px
        if size > 0:
            self.buy(size=size)


def run_window(feed_df: pd.DataFrame, mode: str) -> tuple[float, float]:
    c = bt.Cerebro(stdstats=False)
    c.broker.set_cash(1e-9)  # 0 だと backtrader の内部計算が 0 除算になる
    c.broker.set_coc(True)            # 月末の終値で約定（月次データのため）
    c.broker.set_checksubmit(False)
    c.adddata(bt.feeds.PandasData(dataname=feed_df))
    c.addstrategy(Habit, mode=mode)
    s = c.run()[0]
    # add_cash は次の月から使える。最後の月に入れた分（まだ口座に反映されていない）を足す
    return c.broker.getvalue() + s.p.monthly, s.paid


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=20)
    a = ap.parse_args()
    df = load()
    n = a.years * 12
    starts = list(range(0, len(df) - n, 12))
    res: dict[str, list[float]] = {k: [] for k in STRATS}
    for i in starts:
        w = df.iloc[i:i + n]
        for k in STRATS:
            v, paid = run_window(w, k)
            res[k].append(v / paid)
    years = [int(df.index[i].year) for i in starts]
    keep = res["keep"]
    summary = {}
    for k in STRATS:
        xs = res[k]
        summary[k] = {
            "median": round(statistics.median(xs), 3),
            "p10": round(sorted(xs)[len(xs) // 10], 3),
            "worst": round(min(xs), 3),
            "best": round(max(xs), 3),
            "worst_start": years[xs.index(min(xs))],
            "beat_keep": sum(1 for x, y in zip(xs, keep) if x > y + 1e-9) if k != "keep" else None,
        }
    out = {
        "as_of": date.today().isoformat(),
        "source": "Shiller S&P 500 monthly (datasets/s-and-p-500), dividends reinvested, CPI-adjusted",
        "data_from": str(df.index[0].date()), "data_to": str(df.index[-1].date()),
        "years": a.years, "windows": len(starts), "start_years": years,
        "multiple": {k: [round(x, 3) for x in res[k]] for k in STRATS},
        "summary": summary,
        "assumptions": "毎月同じ実質額を積立。手数料・税 0、現金の実質金利 0%。panic=高値から 20% 下落で全売却・積立停止、高値回復で再開。wait=高値から 10% 下落まで現金で待機。",
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(starts)} windows of {a.years}y, {out['data_from']}..{out['data_to']}")
    for k in STRATS:
        s = summary[k]
        print(f"{k:6s} median x{s['median']:.2f}  p10 x{s['p10']:.2f}  worst x{s['worst']:.2f} ({s['worst_start']})  beat_keep {s['beat_keep']}")


if __name__ == "__main__":
    main()
