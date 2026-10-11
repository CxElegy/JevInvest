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


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "reports" / "overview.html"
    html = TEMPLATE.read_text(encoding="utf-8")
    if MARK not in html:
        raise SystemExit(f"雛形に {MARK} がありません")
    data = json.loads(DATA.read_text(encoding="utf-8"))
    # </script> を含む文字列でスクリプトが閉じないようにする
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html.replace(MARK, blob), encoding="utf-8")
    print(f"→ {out}（{out.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
