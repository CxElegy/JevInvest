"""説明ページとシミュレーターを組み立て、データの埋め込み漏れとスクリプトの構文を確かめる（ネットワーク不要）。"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_build_pages_embed_everything(tmp_path):
    subprocess.run([sys.executable, str(ROOT / "tools" / "build_overview.py"), str(tmp_path)],
                   check=True, capture_output=True)
    for name in ("overview.html", "simulator.html"):
        html = (tmp_path / name).read_text(encoding="utf-8")
        assert "/*__DATA__*/" not in html and "/*__ENGINE__*/" not in html, name
        assert "<title>" in html[:8000], name
        if shutil.which("node") is None:
            continue
        for k, part in enumerate(html.split("<script>")[1:]):
            js = tmp_path / f"{name}.{k}.js"
            js.write_text(part.split("</script>")[0], encoding="utf-8")
            r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
            assert r.returncode == 0, f"{name} script {k}: {r.stderr}"
