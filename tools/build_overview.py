"""説明ページとシミュレーターを、データを埋め込んだ 1 枚ずつの HTML にする。

    python tools/build_overview.py [出力先ディレクトリ]   # 既定: reports/
        → overview.html   web/overview.html + web/papertrade.json（+ stress.json・税の例示）
        → simulator.html  web/simulator.html + web/simengine.js + web/simdata.json + ライブの状態

ページは外部からデータを取りに行かないので、埋め込んだ時点の数字が出る。
ペーパートレードを進めたら、作り直して公開し直す。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "web" / "overview.html"
DATA = ROOT / "web" / "papertrade.json"
MARK = "/*__DATA__*/null"


def tax_examples() -> list[dict]:
    """例示の年収での iDeCo の節税額とふるさと納税の上限（本人の年収ではない）。"""
    sys.path.insert(0, str(ROOT))
    from src.taxplan import compute, furusato_limit, ideco_saving

    out = []
    for salary in (5_000_000, 7_000_000, 9_000_000, 12_000_000):
        r0 = compute(salary)
        out.append({
            "salary": salary,
            "marginal": r0.marginal_rate * 1.021 + 0.10,
            "ideco_23k": ideco_saving(salary, 23_000 * 12)[2],
            "ideco_62k": ideco_saving(salary, 62_000 * 12)[2],
            "furusato": furusato_limit(r0),
            "furusato_62k": furusato_limit(compute(salary, ideco_annual=62_000 * 12)),
        })
    return out


def _blob(data) -> str:
    # </script> を含む文字列でスクリプトが閉じないようにする
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def fx_effect(sim: dict, start: str = "2009-07-01") -> dict:
    """期間中の円安が、円建てのリターンにどれだけ効いたか（VT のドル建てと円建ての年率の差）。"""
    dates = sim["dates"]
    i0 = next(i for i, d in enumerate(dates) if d >= start)
    fx = [v for v in sim["fx"][i0:] if v is not None]
    vt = [v for v in sim["adj"]["VT"][i0:] if v is not None]
    from datetime import date
    yrs = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[i0])).days / 365.25
    g = lambda a, b: (b / a) ** (1 / yrs) - 1   # noqa: E731
    return {"start": dates[i0], "end": dates[-1], "fx0": fx[0], "fx1": fx[-1], "fx_cagr": g(fx[0], fx[-1]),
            "vt_usd": g(vt[0], vt[-1]), "vt_jpy": g(vt[0] * fx[0], vt[-1] * fx[-1])}


def build_simulator(out: Path) -> None:
    html = (ROOT / "web" / "simulator.html").read_text(encoding="utf-8")
    engine = (ROOT / "web" / "simengine.js").read_text(encoding="utf-8")
    if "</script" in engine:
        raise SystemExit("simengine.js に </script が含まれています")
    pt = json.loads(DATA.read_text(encoding="utf-8"))
    sim = json.loads((ROOT / "web" / "simdata.json").read_text(encoding="utf-8"))
    lab_path = ROOT / "web" / "strategy_lab.json"
    lab = json.loads(lab_path.read_text(encoding="utf-8")) if lab_path.exists() else None
    if lab:
        for v in lab["strategies"].values():
            v.pop("nav", None)          # ページでは使わない（容量を抑える）
    sens_path = ROOT / "web" / "strategy_sensitivity.json"
    sens = json.loads(sens_path.read_text(encoding="utf-8")) if sens_path.exists() else None
    reb_path = ROOT / "web" / "rebalance_lab.json"
    reb = json.loads(reb_path.read_text(encoding="utf-8")) if reb_path.exists() else None
    hedge_path = ROOT / "web" / "hedge_lab.json"
    hedge = json.loads(hedge_path.read_text(encoding="utf-8")) if hedge_path.exists() else None
    front_path = ROOT / "web" / "frontload_lab.json"
    front = json.loads(front_path.read_text(encoding="utf-8")) if front_path.exists() else None
    html = html.replace("/*__ENGINE__*/", engine).replace(
        MARK, _blob({"sim": sim, "live": pt.get("live"), "lab": lab, "sens": sens, "reb": reb, "hedge": hedge, "front": front, "fx": fx_effect(sim)}))
    out.write_text(html, encoding="utf-8")
    print(f"→ {out}（{out.stat().st_size / 1024:.0f} KB）")


def main() -> None:
    arg = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "reports"
    # 互換: 以前はファイル名を渡していた
    outdir = arg.parent if arg.suffix == ".html" else arg
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / "overview.html"
    build_simulator(outdir / "simulator.html")
    html = TEMPLATE.read_text(encoding="utf-8")
    if MARK not in html:
        raise SystemExit(f"雛形に {MARK} がありません")
    data = json.loads(DATA.read_text(encoding="utf-8"))
    data["tax"] = tax_examples()
    stress = ROOT / "web" / "stress.json"
    if stress.exists():
        data["stress"] = json.loads(stress.read_text(encoding="utf-8"))
    out.write_text(html.replace(MARK, _blob(data)), encoding="utf-8")
    print(f"→ {out}（{out.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
