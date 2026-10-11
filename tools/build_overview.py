"""web/overview.html（説明ページの雛形）に web/papertrade.json を埋め込んで 1 枚の HTML にする。

    python tools/build_overview.py [出力先]     # 既定: reports/overview.html

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


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "reports" / "overview.html"
    html = TEMPLATE.read_text(encoding="utf-8")
    if MARK not in html:
        raise SystemExit(f"雛形に {MARK} がありません")
    data = json.loads(DATA.read_text(encoding="utf-8"))
    data["tax"] = tax_examples()
    stress = ROOT / "web" / "stress.json"
    if stress.exists():
        data["stress"] = json.loads(stress.read_text(encoding="utf-8"))
    # </script> を含む文字列でスクリプトが閉じないようにする
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html.replace(MARK, blob), encoding="utf-8")
    print(f"→ {out}（{out.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
