"""著名投資家の 13F から「資金の流れ」「市場心理」「ETF の種類」「注目が集まり始めた兆候」を出す。

どれも**前の四半期末からの変化**で、1〜4 か月前の姿。予測ではない。

資金の流れ（推定売買額）
    株数の変化 × 今期末の 1 株あたり評価額。株価の値動きで評価額が増減した分は除く。
    全売却した銘柄は前期末の 1 株あたり評価額を使う（今期の値が無いため）。
    オプションと元本表記（PRN）の社債は株数を持たないので含めない。

市場心理
    - プット÷コール（想定元本）: 上がれば下落への警戒が強まっている
    - プットの想定元本 ÷ 株式ロング: ヘッジの厚さ
    - 守りの資産（債券 ETF・金・商品 ETF）の比率
    - 広がり: 集中投資型の投資家による「新規・買い増し」と「減少・全売却」の件数
"""

from __future__ import annotations

from dataclasses import dataclass, field

import holdings_mix as hm

ETF_KINDS = ("idx_etf", "bond_etf", "gold_etf", "crypto_etf", "other_etf")
DEFENSIVE = ("bond_etf", "gold_etf")


def _usd(v: float) -> str:
    a = abs(v)
    s = f"${a / 1e9:,.1f}B" if a >= 1e9 else f"${a / 1e6:,.0f}M"
    return ("−" if v < 0 else "+") + s


@dataclass
class MarketAccumulator:
    sector_flow: dict = field(default_factory=dict)       # 業種 -> {all, sel, buyers:set, sellers:set}
    kind_flow: dict = field(default_factory=dict)         # 資産の種類 -> 推定売買額
    etfs: dict = field(default_factory=dict)              # ティッカー -> 集計
    new_by_sector: dict = field(default_factory=dict)     # 業種 -> 新規購入した投資家（選別型）
    new_by_ticker: dict = field(default_factory=dict)     # 銘柄 -> 新規購入した投資家（選別型）
    opts: dict = field(default_factory=lambda: {"put": 0.0, "call": 0.0, "put_prev": 0.0,
                                                "call_prev": 0.0, "long": 0.0, "long_prev": 0.0})
    defensive: dict = field(default_factory=lambda: {"cur": 0.0, "prev": 0.0, "n": 0})
    breadth: dict = field(default_factory=lambda: {"new": 0, "add": 0, "trim": 0, "exit": 0})
    n_filers: int = 0

    def add_filer(self, person: str, cur, prev, selective: bool, change_fn,
                  label_fn, puts: float, calls: float, puts_prev: float, calls_prev: float,
                  long_cur: float, long_prev: float, mix: dict, mix_prev: dict) -> None:
        """前期と比べられる投資家 1 人分を足し込む。cur/prev は _grouped の DataFrame。"""
        self.n_filers += 1
        self.opts["put"] += puts
        self.opts["call"] += calls
        self.opts["put_prev"] += puts_prev
        self.opts["call_prev"] += calls_prev
        self.opts["long"] += long_cur
        self.opts["long_prev"] += long_prev
        self.defensive["cur"] += sum(mix.get(k, 0.0) for k in DEFENSIVE)
        self.defensive["prev"] += sum(mix_prev.get(k, 0.0) for k in DEFENSIVE)
        self.defensive["n"] += 1

        net_by_sector: dict[str, float] = {}
        for key in set(cur.index) | set(prev.index):
            src = cur.loc[key] if key in cur.index else prev.loc[key]
            cs = float(cur.loc[key, "shares"]) if key in cur.index else 0.0
            ps = float(prev.loc[key, "shares"]) if key in prev.index else 0.0
            if cs > 0:
                px = float(cur.loc[key, "value"]) / cs
            elif ps > 0:
                px = float(prev.loc[key, "value"]) / ps
            else:
                continue
            flow = (cs - ps) * px
            kind = src.get("kind") or "unknown"
            self.kind_flow[kind] = self.kind_flow.get(kind, 0.0) + flow

            sector = src["sector"]
            e = self.sector_flow.setdefault(sector, {"all": 0.0, "sel": 0.0,
                                                     "buyers": set(), "sellers": set(), "by": {}})
            e["by"][person] = e["by"].get(person, 0.0) + flow
            e["all"] += flow
            if selective:
                e["sel"] += flow
            net_by_sector[sector] = net_by_sector.get(sector, 0.0) + flow

            c, _ = change_fn(cs, ps)
            if selective and c in self.breadth:
                self.breadth[c] += 1
            if selective and c == "new":
                self.new_by_sector.setdefault(sector, set()).add(person)
                lab = label_fn(src)
                if lab.get("t"):
                    t = self.new_by_ticker.setdefault(lab["t"], {**lab, "names": set()})
                    t["names"].add(person)

            if kind in ETF_KINDS:
                lab = label_fn(src)
                if not lab.get("t"):
                    continue
                t = self.etfs.setdefault(lab["t"], {**lab, "kind": kind, "value": 0.0,
                                                   "flow": 0.0, "holders": {},
                                                   "sub": hm.etf_subtype(lab["t"], lab["c"], kind)})
                cv = float(cur.loc[key, "value"]) if key in cur.index else 0.0
                t["value"] += cv
                t["flow"] += flow
                if cv > 0:
                    t["holders"][person] = t["holders"].get(person, 0.0) + cv

        # 業種ごとに、その投資家が差し引きで買ったか売ったか（金額の大きさに左右されない人数）
        for sector, v in net_by_sector.items():
            if abs(v) < 1:
                continue
            (self.sector_flow[sector]["buyers"] if v > 0 else self.sector_flow[sector]["sellers"]).add(person)

    def result(self) -> dict:
        o = self.opts
        pc = o["put"] / o["call"] if o["call"] else None
        pc_prev = o["put_prev"] / o["call_prev"] if o["call_prev"] else None
        hedge = o["put"] / o["long"] * 100 if o["long"] else 0.0
        hedge_prev = o["put_prev"] / o["long_prev"] * 100 if o["long_prev"] else 0.0
        n = self.defensive["n"] or 1
        deff, deff_prev = self.defensive["cur"] / n, self.defensive["prev"] / n
        b = self.breadth
        ups, downs = b["new"] + b["add"], b["trim"] + b["exit"]

        def biggest(e):
            if not e["by"]:
                return None
            who, v = max(e["by"].items(), key=lambda kv: abs(kv[1]))
            share = abs(v) / abs(e["all"]) * 100 if e["all"] else None
            return {"p": who, "flow": round(v), "share": None if share is None else round(share)}

        sectors = sorted(
            ({"s": s, "flow": round(e["all"]), "flow_sel": round(e["sel"]),
              "buyers": len(e["buyers"]), "sellers": len(e["sellers"]), "top": biggest(e)}
             for s, e in self.sector_flow.items()),
            key=lambda d: -d["flow"])
        kinds = [{"k": k, "label": hm.KIND_JP[k], "flow": round(v)}
                 for k, v in sorted(self.kind_flow.items(), key=lambda kv: -kv[1])
                 if k != "unknown" and abs(v) >= 1]
        etfs = sorted(
            ({"t": t, "c": e["c"], "kind": e["kind"], "kind_jp": hm.KIND_JP[e["kind"]], "sub": e["sub"],
              "value": round(e["value"]), "flow": round(e["flow"]),
              "holders": [p for p, _ in sorted(e["holders"].items(), key=lambda kv: -kv[1])]}
             for t, e in self.etfs.items() if e["value"] > 0 or abs(e["flow"]) > 0),
            key=lambda d: -d["value"])
        early = sorted(
            ({"t": t, "c": e["c"], "s": e["s"], "names": sorted(e["names"])}
             for t, e in self.new_by_ticker.items() if len(e["names"]) >= 2),
            key=lambda d: -len(d["names"]))
        new_sectors = sorted(((s, sorted(p)) for s, p in self.new_by_sector.items()),
                             key=lambda kv: -len(kv[1]))

        sentiment = {
            "pc_ratio": None if pc is None else round(pc, 2),
            "pc_ratio_prev": None if pc_prev is None else round(pc_prev, 2),
            "hedge_pct": round(hedge, 1), "hedge_pct_prev": round(hedge_prev, 1),
            "defensive_pct": round(deff, 2), "defensive_pct_prev": round(deff_prev, 2),
            "breadth": b, "breadth_ratio": round(ups / downs, 2) if downs else None,
        }
        return {
            "n_filers": self.n_filers, "sectors": sectors, "kinds": kinds, "etfs": etfs,
            "early": early[:15],
            "new_sectors": [{"s": s, "names": p} for s, p in new_sectors[:8]],
            "sentiment": sentiment,
            "notes": self._notes(sectors, kinds, etfs, early, new_sectors, sentiment),
        }

    def _notes(self, sectors, kinds, etfs, early, new_sectors, st) -> dict:
        flows, mood, outlook = [], [], []

        ins = [s for s in sectors if s["flow"] > 0][:3]
        outs = [s for s in reversed(sectors) if s["flow"] < 0][:3]
        if ins:
            flows.append("推定で資金が入った分野: " + "、".join(
                f"{s['s']}（{_usd(s['flow'])}、買い {s['buyers']} 人 / 売り {s['sellers']} 人）" for s in ins) + "。")
        lop = [s for s in sectors if s["top"] and (s["top"]["share"] or 0) >= 60 and abs(s["flow"]) >= 1e9]
        if lop:
            flows.append("1 社の売買が大半を占めた分野: " + "、".join(
                f"{s['s']}（{s['top']['p']} が {_usd(s['top']['flow'])}）" for s in lop[:3])
                + "。全体の流れというより、その投資家個別の判断として読みます。")
        if outs:
            flows.append("推定で資金が出た分野: " + "、".join(
                f"{s['s']}（{_usd(s['flow'])}、買い {s['buyers']} 人 / 売り {s['sellers']} 人）" for s in outs) + "。")
        wide = [s for s in sectors if s["buyers"] >= s["sellers"] + 3]
        if wide:
            flows.append("金額ではなく人数で見て買いが多かった分野: " + "、".join(
                f"{s['s']}（{s['buyers']} 対 {s['sellers']}）" for s in wide[:3])
                + "。一部の大口ではなく、幅広い投資家が買っている分野です。")
        etf_in = [k for k in kinds if k["k"] in ETF_KINDS]
        if etf_in:
            flows.append("ETF の種類別の推定売買額: " + "、".join(
                f"{k['label']} {_usd(k['flow'])}" for k in etf_in) + "。")
        flows.append("金額の合計は数千銘柄を持つクオンツ運用の売買に大きく左右されます。"
                     "人数の欄とあわせて読んでください。")

        pc, pcp = st["pc_ratio"], st["pc_ratio_prev"]
        if pc is not None and pcp is not None:
            if abs(pc - pcp) < 0.05:
                mood.append(f"プット÷コール（想定元本）は前期 {pcp:.2f} → 今期 {pc:.2f} でほぼ横ばい。"
                            "オプションで見た警戒感は前の四半期と変わっていません。")
            else:
                d = "上がり" if pc > pcp else "下がり"
                tone = "下落への警戒が強まった" if pc > pcp else "警戒が和らいだ"
                mood.append(f"プット÷コール（想定元本）は前期 {pcp:.2f} → 今期 {pc:.2f} に{d}ました。{tone}ことを示します。")
            if pc > 1:
                mood.append("水準としてはプットがコールを上回っており、上昇への賭けより下落への備えのほうが大きい状態です。")
        mood.append(f"プットの想定元本は株式ロング全体の {st['hedge_pct_prev']:.1f}% → {st['hedge_pct']:.1f}%。"
                    "ただし大半はマクロ型・マルチ戦略の少数の運用会社によるものです。")
        dd = st["defensive_pct"] - st["defensive_pct_prev"]
        mood.append(f"守りの資産（債券 ETF・金・商品 ETF）の平均比率は {st['defensive_pct_prev']:.2f}% → "
                    f"{st['defensive_pct']:.2f}%（{dd:+.2f}pp）。13F に国債・現金は出ないので、"
                    "守りの度合いの一部しか見えていません。")
        b = st["breadth"]
        if st["breadth_ratio"] is not None:
            tone = ("買いのほうが多く、強気寄り" if st["breadth_ratio"] > 1.1 else
                    "売りのほうが多く、慎重寄り" if st["breadth_ratio"] < 0.9 else "売り買いが拮抗")
            mood.append(f"集中投資型の売買件数: 新規 {b['new']}・買い増し {b['add']} に対し、"
                        f"減少 {b['trim']}・全売却 {b['exit']}（比 {st['breadth_ratio']:.2f}）。{tone}です。")

        if early:
            outlook.append("複数の投資家が同じ四半期に新しく買い始めた銘柄: " + "、".join(
                f"{e['t']}（{'、'.join(e['names'][:3])}）" for e in early[:5])
                + "。注目が集まり始めた兆候として見られます。")
        if new_sectors:
            s, p = new_sectors[0]
            outlook.append(f"新規購入が最も多かった分野は「{s}」（{len(p)} 人）。")
        top_etf = [e for e in etfs if e["kind"] == "idx_etf"][:3]
        if top_etf:
            outlook.append("株式インデックス ETF の大口: " + "、".join(
                f"{e['t']}（{_usd(e['flow'])}）" for e in top_etf)
                + "。指数 ETF は相場全体の売買やヘッジに使われるため、個別株の見方とは分けて読みます。")
        outlook.append("これは 1〜4 か月前に何をしていたかの記録で、これからの値動きの予測ではありません。"
                       "著名投資家が買った後に下がることもあり、13F からは売買の理由も分かりません。")
        return {"flows": flows, "mood": mood, "outlook": outlook}
