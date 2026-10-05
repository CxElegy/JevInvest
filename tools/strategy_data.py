"""戦略ページの数字と、その導き方（途中の値）を web/strategy.json に書く。

ページは結論だけでなく、どの価格からどう計算したかを表示する。ここで出す値はすべて公開の市場データで、
個人の金額は含まない。

  ファンド（全体）: 連動する指数を円に直した週次の値から、年率・値動き・最大下落・相関を出す
  組み合わせ      : 比率 w と共分散行列 Σ から、全体の値動き √(wᵀΣw) と各ファンドの寄与 wᵢ(Σw)ᵢ/σ を出す
  1 株ずつ        : 各ファンドの上位銘柄（S&P500・日経半導体は上位 10、FANG+ は 10 銘柄均等）の 1 年の値動き
  全体の株構造    : ファンドの比率 × 銘柄の比率で、ポートフォリオ全体で各銘柄をどれだけ持つかを合算

    vendor/.venv/bin/python tools/strategy_data.py
"""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "strategy.json"

# 比率は「道筋」タブの戦略と揃える
WEIGHTS = {"sp": 0.65, "fang": 0.25, "semi": 0.10}
ALT = {"sp": 0.60, "fang": 0.25, "semi": 0.15}
FUNDS = {
    "sp": {"name": "たわらノーロード S&P500", "index": "S&P 500（配当込み）", "proxy": "SPY", "usd": True, "fee": 0.09372},
    "fang": {"name": "iFreeNEXT FANG+インデックス", "index": "NYSE FANG+ 指数", "proxy": "^NYFANG", "usd": True, "fee": 0.7755},
    "semi": {"name": "野村インデックスファンド・日経半導体株", "index": "日経半導体株指数", "proxy": "200A.T", "usd": False, "fee": 0.33},
}
# FANG+ は 10 銘柄を同じ金額ずつ持つ（四半期ごとに入れ替え）。2026 年時点の構成
FANG_MEMBERS = ["META", "AAPL", "AMZN", "NFLX", "MSFT", "GOOGL", "MU", "NVDA", "PLTR", "AVGO"]


def holdings() -> dict[str, list[dict]]:
    out = {}
    for key, etf in (("sp", "SPY"), ("semi", "200A.T")):
        th = yf.Ticker(etf).funds_data.top_holdings
        rows = [{"ticker": t, "name": r["Name"], "w": float(r["Holding Percent"])} for t, r in th.iterrows()]
        out[key] = rows
    out["fang"] = [{"ticker": t, "name": t, "w": 0.10} for t in FANG_MEMBERS]
    return out


def weekly(s: pd.Series) -> pd.Series:
    """週の最後の取引日の値。ラベルは実際の日付にする（resample の既定は週末の金曜日）。"""
    g = s.groupby(pd.Grouper(freq="W-FRI"))
    return pd.Series(g.last().values, index=g.apply(lambda x: x.index[-1] if len(x) else pd.NaT).values).dropna()


def stats(s: pd.Series, freq: int) -> dict:
    """s は価格。年率・値動き・最大下落と、その計算に使った値を返す。"""
    s = s.dropna()
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    r = s.pct_change().dropna()
    cagr = (s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1
    sd = float(r.std())
    vol = sd * math.sqrt(freq)
    dd = s / s.cummax() - 1
    t_i = dd.idxmin()
    p_i = s.loc[:t_i].idxmax()
    return {
        "start": str(s.index[0].date()), "end": str(s.index[-1].date()), "years": round(yrs, 2),
        "p0": round(float(s.iloc[0]), 2), "p1": round(float(s.iloc[-1]), 2),
        "cagr": round(cagr, 4), "n": int(len(r)), "sd": round(sd, 5), "freq": freq, "vol": round(vol, 4),
        "peak_date": str(p_i.date()), "peak": round(float(s.loc[p_i]), 2),
        "trough_date": str(t_i.date()), "trough": round(float(s.loc[t_i]), 2), "mdd": round(float(dd.min()), 4),
        "from_high": round(float(s.iloc[-1] / s.max() - 1), 4),
    }


def main() -> None:
    hold = holdings()
    tickers = {f["proxy"] for f in FUNDS.values()} | {"USDJPY=X"}
    for rows in hold.values():
        tickers |= {h["ticker"] for h in rows}
    px = yf.download(sorted(tickers), start="2015-09-01", auto_adjust=True, progress=False)["Close"]
    fx = px["USDJPY=X"].ffill()

    def jpy(t: str, usd: bool) -> pd.Series:
        s = px[t].dropna()                       # 取引のあった日だけ（週末や祝日の行を作らない）
        return s * fx.reindex(s.index).ffill() if usd else s

    lv = {k: jpy(f["proxy"], f["usd"]) for k, f in FUNDS.items()}
    common_start = max(s.index[0] for s in lv.values())        # 日経半導体の ETF が上場した 2024-06
    W = pd.DataFrame({k: weekly(s.loc[common_start:]) for k, s in lv.items()}).dropna()
    wr = W.pct_change().dropna()

    funds = {}
    for k, f in FUNDS.items():
        long = weekly(lv[k].loc[lv[k].index[-1] - pd.Timedelta(days=3652):])
        funds[k] = {**{x: f[x] for x in ("name", "index", "proxy", "fee")}, "weight": WEIGHTS[k],
                    "common": stats(W[k], 52), "long": stats(long, 52) if long.index[0] < common_start - pd.Timedelta(days=365) else None}

    cov = wr.cov() * 52
    corr_w = wr.corr()
    mr = W.resample("ME").last().pct_change().dropna()
    corr_m = mr.corr()

    def risk(weights: dict) -> dict:
        keys = list(FUNDS)
        w = np.array([weights[k] for k in keys])
        S = cov.loc[keys, keys].values
        sw = S @ w
        pv = math.sqrt(float(w @ sw))
        rc = w * sw / pv
        return {"w": dict(zip(keys, w.round(4).tolist())), "sigma_w": dict(zip(keys, sw.round(5).tolist())),
                "pvar": round(float(w @ sw), 5), "pvol": round(pv, 4),
                "rc": dict(zip(keys, rc.round(5).tolist())), "share": dict(zip(keys, (rc / pv).round(4).tolist()))}

    # 組み合わせの週次の値（毎週比率を戻す近似）
    port = (1 + (wr * pd.Series(WEIGHTS)).sum(axis=1)).cumprod()
    series = {"dates": [str(d.date()) for d in W.index],
              **{k: (W[k] / W[k].iloc[0] * 100).round(2).tolist() for k in FUNDS},
              "port": ([100.0] + (port * 100).round(2).tolist())}

    # 1 株ずつ（直近 1 年、円建て）
    one_year = lv["sp"].index[-1] - pd.Timedelta(days=365)
    stocks = {}
    for k, rows in hold.items():
        usd = FUNDS[k]["usd"]
        out = []
        for h in rows:
            s = jpy(h["ticker"], usd).loc[one_year:]
            if len(s) < 30:
                continue
            st = stats(weekly(s), 52)
            ret = float(s.iloc[-1] / s.iloc[0] - 1)
            out.append({**h, "w": round(h["w"], 4), "ret1y": round(ret, 4), "vol1y": st["vol"], "mdd1y": st["mdd"],
                        "contrib": round(h["w"] * ret, 4)})
        covered = sum(x["w"] for x in out)
        stocks[k] = {"rows": out, "covered": round(covered, 4)}

    # 全体の株構造: ファンドの比率 × 銘柄の比率（Alphabet の 2 種類はまとめる）
    look = {}
    for k, v in stocks.items():
        for x in v["rows"]:
            key = "GOOGL" if x["ticker"] in ("GOOG", "GOOGL") else x["ticker"]
            e = look.setdefault(key, {"ticker": key, "name": x["name"], "parts": {}, "total": 0.0})
            e["parts"][k] = round(e["parts"].get(k, 0) + WEIGHTS[k] * x["w"], 5)
            e["total"] = round(e["total"] + WEIGHTS[k] * x["w"], 5)
    look_rows = sorted(look.values(), key=lambda e: -e["total"])
    named = sum(e["total"] for e in look_rows)

    out = {
        "as_of": str(lv["sp"].index[-1].date()), "built": date.today().isoformat(), "usdjpy": round(float(fx.iloc[-1]), 2),
        "weights": WEIGHTS, "funds": funds,
        "corr_weekly": corr_w.round(3).to_dict(), "corr_monthly": corr_m.round(3).to_dict(),
        "n_weekly": int(len(wr)), "n_monthly": int(len(mr)),
        "cov": cov.round(5).to_dict(),
        "risk": risk(WEIGHTS), "risk_alt": risk(ALT), "alt_weights": ALT,
        "series": series, "stocks": stocks,
        "lookthrough": {"rows": look_rows[:15], "named_total": round(named, 4),
                        "usd_share": round(WEIGHTS["sp"] + WEIGHTS["fang"], 4), "jpy_share": WEIGHTS["semi"]},
        "notes": "指数を円に直した値で計算（信託報酬は含まない）。S&P500 は SPY、FANG+ は NYSE FANG+ 指数、日経半導体は 200A（同じ指数の ETF）を使用。"
                 "組み合わせの数字は 3 本がそろう期間（日経半導体 ETF の上場以降）の週次データ。",
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print("wrote", OUT, "as of", out["as_of"])
    r = out["risk"]
    print("pvol", r["pvol"], "share", r["share"])
    print("top look-through", [(e["ticker"], round(e["total"] * 100, 2)) for e in look_rows[:8]])


if __name__ == "__main__":
    main()
