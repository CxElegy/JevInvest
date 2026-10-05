"""選んだ 3 本のファンドを 8 つの視点で分析するための数字を web/deepdive.json に書く。

ファンドは指数に連動するので、会社ごとの分析（決算・内部者の売買・企業価値）は各ファンドの上位銘柄で行い、
ファンドとしての数字は「ファンド内の比率」で重みを付けて合算する。すべて公開データで、個人の金額は含まない。

  ① 機関投資家     : 著名投資家（13F）の保有と増減、アナリストの評価、保有の集中（混雑）
  ② 強気と弱気     : 下の数字を材料にページ側で両論を並べる
  ③ 決算           : 直近 5 四半期の売上と営業利益、前年同期比、利益率の変化
  ④ リスク         : 指標の現在値（PER・集中度・値動き）をページ側の早期警戒サインと照らす
  ⑤ バフェット流   : ROE・営業利益率・手元資金・売上成長で「堀」を点数化、FCF 利回り（オーナー収益）
  ⑥ 決算説明会     : 書き起こしは取得できないため、数字の変化（強調された点）だけを出す
  ⑦ 内部者         : 過去 6 か月の役員の買い・売り（米国株のみ取得できる）
  ⑧ DCF            : ファンド全体の 3 シナリオと感応度表、銘柄ごとの「必要な利益成長率」（逆算 DCF）

    vendor/.venv/bin/python tools/fund_deepdive.py     # 先に tools/strategy_data.py を実行しておく
"""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
STRAT = ROOT / "web" / "strategy.json"
INVESTORS = ROOT / "web" / "investors.json"
OUT = ROOT / "web" / "deepdive.json"

TARGET = 0.09        # 逆算 DCF で求める年率（株式に期待する標準的なリターン）
EXIT_PE = 20.0       # 10 年後に落ち着くと仮定する PER
YEARS = 10
# 10 年シナリオ（利益の年成長 g、10 年後の PER、配当利回り）。確率は 25・50・25
SCEN = {
    "sp": {"bear": (0.03, 15), "base": (0.07, 19), "bull": (0.11, 24), "dy": 0.012},
    "fang": {"bear": (0.04, 18), "base": (0.10, 22), "bull": (0.15, 28), "dy": 0.004},
    "semi": {"bear": (0.00, 12), "base": (0.08, 16), "bull": (0.15, 22), "dy": 0.010},
}
PROB = {"bear": 0.25, "base": 0.50, "bull": 0.25}
GRID_G = [0.0, 0.04, 0.08, 0.12, 0.16]
GRID_PE = [12, 16, 20, 24, 28]
INFO_KEYS = ["shortName", "trailingPE", "forwardPE", "revenueGrowth", "earningsGrowth", "grossMargins", "operatingMargins",
             "returnOnEquity", "freeCashflow", "marketCap", "totalDebt", "totalCash", "heldPercentInstitutions",
             "heldPercentInsiders", "recommendationMean", "numberOfAnalystOpinions", "targetMeanPrice", "currentPrice", "dividendYield"]


def fnum(v):
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def annual(pe0: float, g: float, pe1: float, dy: float) -> float:
    """今の PER pe0 で買い、利益が年 g で伸び、10 年後に PER pe1 になったときの年率（配当込みの概算）。"""
    return ((1 + g) ** YEARS * pe1 / pe0) ** (1 / YEARS) - 1 + dy


def required_growth(pe0: float) -> float:
    """年 TARGET のリターンを得るために必要な利益の年成長率（10 年後の PER を EXIT_PE と仮定）。"""
    return (pe0 / EXIT_PE) ** (1 / YEARS) * (1 + TARGET) - 1


def quarters(t: yf.Ticker) -> list[dict]:
    try:
        q = t.quarterly_income_stmt
        rows = []
        for col in list(q.columns)[:5]:
            rev = fnum(q.at["Total Revenue", col]) if "Total Revenue" in q.index else None
            op = fnum(q.at["Operating Income", col]) if "Operating Income" in q.index else None
            rows.append({"q": str(pd.Timestamp(col).date()), "rev": rev, "op": op})
        return rows
    except Exception:
        return []


def insider(t: yf.Ticker) -> dict | None:
    try:
        ip = t.insider_purchases
        if ip is None or ip.empty:
            return None
        m = {str(r.iloc[0]): (fnum(r.get("Shares")), fnum(r.get("Trans"))) for _, r in ip.iterrows()}
        return {"buy_n": m.get("Purchases", (None, None))[1], "buy_sh": m.get("Purchases", (None, None))[0],
                "sell_n": m.get("Sales", (None, None))[1], "sell_sh": m.get("Sales", (None, None))[0],
                "net_pct": m.get("% Net Shares Purchased (Sold)", (None, None))[0]}
    except Exception:
        return None


def main() -> None:
    S = json.loads(STRAT.read_text())
    inv = json.loads(INVESTORS.read_text()) if INVESTORS.exists() else {"tickers": {}}
    tick13 = inv.get("tickers", {})

    names = sorted({x["ticker"] for v in S["stocks"].values() for x in v["rows"]})
    stocks = {}
    for tk in names:
        t = yf.Ticker(tk)
        try:
            info = t.info
        except Exception:
            info = {}
        d = {k: (info.get(k) if k == "shortName" else fnum(info.get(k))) for k in INFO_KEYS}
        pe_use = d["forwardPE"] if d["forwardPE"] and 0 < d["forwardPE"] < 120 else d["trailingPE"]
        d["pe_used"] = pe_use
        d["req_growth"] = round(required_growth(pe_use), 4) if pe_use and pe_use > 0 else None
        d["fcf_yield"] = round(d["freeCashflow"] / d["marketCap"], 4) if d["freeCashflow"] and d["marketCap"] else None
        d["net_cash"] = (d["totalCash"] or 0) - (d["totalDebt"] or 0) if d["totalCash"] is not None else None
        d["upside"] = round(d["targetMeanPrice"] / d["currentPrice"] - 1, 4) if d["targetMeanPrice"] and d["currentPrice"] else None
        moat = [("ROE 15% 以上", (d["returnOnEquity"] or 0) >= 0.15), ("営業利益率 20% 以上", (d["operatingMargins"] or 0) >= 0.20),
                ("借入より手元資金が多い", (d["net_cash"] or -1) > 0), ("売上が年 10% 以上伸びている", (d["revenueGrowth"] or 0) >= 0.10)]
        d["moat"] = [[m, bool(ok)] for m, ok in moat]
        d["moat_score"] = sum(1 for _, ok in moat if ok)
        d["quarters"] = quarters(t)
        d["insider"] = insider(t) if not tk.endswith(".T") else None
        base = "GOOGL" if tk == "GOOG" else tk.replace(".T", "")
        h13 = tick13.get(base) or tick13.get(tk)
        if h13:
            hs = h13.get("holders", [])
            d["f13"] = {"holders": len(hs), "add": sum(1 for h in hs if h.get("chg") in ("add", "new")),
                        "trim": sum(1 for h in hs if h.get("chg") in ("trim", "exit")),
                        "top": [h["p"] for h in hs[:4]]}
        stocks[tk] = d

    funds = {}
    for k, v in S["stocks"].items():
        rows = v["rows"]
        wsum = sum(x["w"] for x in rows)

        def wavg(field):
            pairs = [(x["w"], stocks[x["ticker"]].get(field)) for x in rows if stocks[x["ticker"]].get(field) is not None]
            tw = sum(w for w, _ in pairs)
            return round(sum(w * val for w, val in pairs) / tw, 4) if tw else None

        def hpe(field):
            # 合算の PER は利益利回り（1/PER）の加重平均の逆数で出す（赤字や極端な値は除く）
            pairs = [(x["w"], stocks[x["ticker"]].get(field)) for x in rows
                     if stocks[x["ticker"]].get(field) and 0 < stocks[x["ticker"]][field] < 500]
            tw = sum(w for w, _ in pairs)
            return round(tw / sum(w / pe for w, pe in pairs), 2) if tw else None

        pe_trailing = hpe("trailingPE")
        if k == "sp":
            try:
                spy_pe = fnum(yf.Ticker("SPY").info.get("trailingPE"))
            except Exception:
                spy_pe = None
            pe0 = spy_pe or pe_trailing
            pe_note = "S&P500 全体の PER（SPY）"
        else:
            pe0 = pe_trailing
            pe_note = "上位銘柄の PER を比率で合算"
        sc = SCEN[k]
        scen = {}
        for name in ("bear", "base", "bull"):
            g, pe1 = sc[name]
            scen[name] = {"g": g, "pe1": pe1, "p": PROB[name], "ret": round(annual(pe0, g, pe1, sc["dy"]), 4)}
        weighted = round(sum(s["p"] * s["ret"] for s in scen.values()), 4)
        grid = [[round(annual(pe0, g, pe1, sc["dy"]), 4) for pe1 in GRID_PE] for g in GRID_G]
        adds = sum((stocks[x["ticker"]].get("f13") or {}).get("add", 0) for x in rows)
        trims = sum((stocks[x["ticker"]].get("f13") or {}).get("trim", 0) for x in rows)
        funds[k] = {
            "covered": round(wsum, 4), "pe0": round(pe0, 2), "pe_note": pe_note, "pe_trailing_top": pe_trailing,
            "pe_forward_top": hpe("forwardPE"), "rev_growth": wavg("revenueGrowth"), "op_margin": wavg("operatingMargins"),
            "roe": wavg("returnOnEquity"), "fcf_yield": wavg("fcf_yield"), "inst": wavg("heldPercentInstitutions"),
            "rec": wavg("recommendationMean"), "upside": wavg("upside"), "moat": wavg("moat_score"),
            "req_growth": round(required_growth(pe0), 4), "scen": scen, "weighted": weighted, "dy": sc["dy"],
            "grid": {"g": GRID_G, "pe": GRID_PE, "ret": grid}, "f13_add": adds, "f13_trim": trims,
            "top": [{"ticker": x["ticker"], "w": x["w"]} for x in rows],
        }

    out = {"as_of": S["as_of"], "built": date.today().isoformat(), "target": TARGET, "exit_pe": EXIT_PE, "years": YEARS,
           "f13_report": inv.get("report_date"), "funds": funds, "stocks": stocks}
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print("wrote", OUT)
    for k, f in funds.items():
        print(k, "PE", f["pe0"], "req g", f["req_growth"], "scen", {n: s["ret"] for n, s in f["scen"].items()}, "weighted", f["weighted"],
              "rev g", f["rev_growth"], "opm", f["op_margin"], "13F +/-", f["f13_add"], f["f13_trim"])


if __name__ == "__main__":
    main()
