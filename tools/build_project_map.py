"""プロジェクト全体マップ（project_map.html）を、リポジトリの中身から作る。

    python tools/build_project_map.py [出力先ディレクトリ]   # 既定は reports/

プログラムの一覧・行数・説明（各ファイルの先頭の docstring / description）・書き出すファイル・
テストの数は、毎回リポジトリを読んで数える（手で書いた一覧は古くなるため）。
分類と「いつ使うか」だけは下の META に手で書く。META に無いファイルは「未分類」として出す。
金額の入る台帳（config/holdings.yaml など）は読まない。
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "web" / "project_map.html"
MARK = "/*__MAP__*/null"

# 分類（表示順）
GROUPS = {
    "core": "本体の計算",
    "plan": "計画・積立",
    "guard": "売買前・独立性",
    "tax": "税",
    "record": "記録・取り込み",
    "risk": "振り返り・リスク",
    "sim": "模擬売買エンジン",
    "lab": "実データの実験",
    "info": "外部の情報",
    "page": "ページ",
    "cmd": "スラッシュコマンド",
    "jev": "切り離し中",
    "setup": "準備用",
}

# path → (分類, いつ使うか)
META = {
    "src/cli.py": ("core", "すべての python -m src.cli … の入口"),
    "src/holdings.py": ("core", "台帳を読むとき（全コマンドの最初）"),
    "src/marketdata.py": ("core", "価格・為替が要るとき（Yahoo Finance から取得し SQLite に保存）"),
    "src/analytics.py": ("core", "評価額・配分・集中度・相関を出すとき"),
    "src/report.py": ("core", "/review でレポートを作るとき"),
    "src/dashboard.py": ("core", "HTML のダッシュボードを作るとき"),
    "src/features.py": ("core", "変動率などの指標（Jev 用の状態を作るときにも使う）"),
    "src/state_schema.py": ("jev", "Jev に渡す状態の項目定義"),
    "src/plan.py": ("plan", "/plan で毎月の積立と NISA 枠の割り当てを見るとき"),
    "src/nextbuy.py": ("plan", "毎月: 今月の積立をどのクラスに入れるか"),
    "src/checkup.py": ("plan", "目標配分を見直すとき（下落・外貨・NISA 枠）"),
    "src/pretrade.py": ("guard", "売買の前（/before-trade）"),
    "src/compliance.py": ("guard", "独立性の確認状況を見るとき（/compliance）"),
    "src/harvest.py": ("tax", "11 月末〜12 月: 課税口座の損出し・益出し"),
    "src/taxplan.py": ("tax", "年 1 回: iDeCo とふるさと納税（/taxplan）"),
    "src/transactions.py": ("record", "取引履歴から保有・実現損益・XIRR を出すとき"),
    "src/importers.py": ("record", "証券会社の CSV を取り込むとき（/import-csv）"),
    "src/stress.py": ("risk", "過去の急落・円高を今の配分に当てはめるとき"),
    "src/scoreboard.py": ("risk", "1 月: 遊び枠を全世界株と比べるとき"),
    "src/sim.py": ("sim", "ペーパートレードと全バックテストの共通エンジン"),
    "src/jev.py": ("jev", "接続していない（docs/REPORT.md の検証が先）"),
    "web/simengine.js": ("sim", "シミュレーター（ブラウザ）で同じ計算をする。Python 版と一致をテストで確認"),
    "tools/papertrade.py": ("sim", "毎朝 7:07 の自動処理で、仮想 500 万円 × 19 口座を進める"),
    "tools/export_sim_data.py": ("sim", "毎朝: シミュレーター用の実データ（2008 年〜）を更新"),
    "tools/strategy_lab.py": ("lab", "17 戦略の成績表を作り直すとき"),
    "tools/strategy_sensitivity.py": ("lab", "戦略の設定を少し動かしたときの差を見るとき"),
    "tools/rebalance_lab.py": ("lab", "再バランスの方法を比べるとき"),
    "tools/hedge_lab.py": ("lab", "債券を為替ヘッジするかを比べるとき"),
    "tools/frontload_lab.py": ("lab", "NISA の年枠を年初にまとめるかを比べるとき"),
    "tools/wait_lab.py": ("lab", "NISA 枠を待つか課税口座で先に投資するかを比べるとき"),
    "tools/backtest_habits.py": ("lab", "積立の続け方の違いを米国株 150 年分で比べるとき"),
    "tools/strategy_data.py": ("lab", "台帳ページの戦略タブの数字を作るとき"),
    "tools/fund_deepdive.py": ("info", "候補ファンド 3 本を 8 つの視点で比べるとき"),
    "tools/fetch_13f.py": ("info", "著名投資家の 13F を SEC から取得するとき"),
    "tools/analyze_13f.py": ("info", "13F を集計して読むとき"),
    "tools/export_13f_json.py": ("info", "13F を台帳ページの「市場の参考」タブ用にするとき"),
    "tools/figi.py": ("info", "13F の CUSIP をティッカーに直すとき"),
    "tools/sectors.py": ("info", "13F の銘柄に業種を付けるとき"),
    "tools/holdings_mix.py": ("info", "13F の持ち方の特徴を文章にするとき"),
    "tools/market_view.py": ("info", "13F から資金の流れ・市場心理を出すとき"),
    "tools/screen_candidates.py": ("info", "13F から候補リストを作るとき（買う前に独立性の確認が要る）"),
    "tools/influencers.py": ("info", "/influencers: 注目アカウントの投稿を取り込むとき"),
    "tools/influencer_history.py": ("info", "注目アカウントの過去の投稿を月ごとにまとめるとき"),
    "tools/build_overview.py": ("page", "説明ページとシミュレーターを作り直すとき（毎朝の自動処理でも）"),
    "tools/build_project_map.py": ("page", "このページを作り直すとき"),
    "web/overview.html": ("page", "説明ページ（中国語）の雛形。「待你决定」もここ"),
    "web/simulator.html": ("page", "シミュレーター（中国語）の雛形"),
    "web/ledger.html": ("page", "台帳ページ。金額はブラウザの中だけに保存し、比率だけを Claude に渡す"),
    "web/project_map.html": ("page", "このページの雛形"),
    "tools/estimate_cost.py": ("jev", "Jev を使った場合のコストの見積り"),
    "tools/install_skills.sh": ("setup", "他の AI 投資ツール 16 個を読むために取得する（評価は docs/TOOLS.md）"),
    "tools/install_x_timeline.sh": ("setup", "X の投稿を取り込む道具を入れる"),
}

NAME_RE = re.compile(r'(?:"web" / "|"reports" / "|web/|paper/|reports/|PAPER / "|OUT_DIR / "|\bfetch\(")([A-Za-z_0-9]+\.(?:json|md|yaml))')
# 書き出し先の定数（OUT = ROOT / "web" / "x.json" など）が書かれた行
WRITE_LINE = re.compile(r"^\s*(?:OUT|out|REPORT|BT_JSON|BT_MD|STATE)\s*=\s*(?:ROOT|PAPER|OUT_DIR|Path)")
# 雛形からページを作るものは、書き出すページ名を手で書く
WRITES_EXTRA = {"tools/build_overview.py": ["overview.html", "simulator.html"],
                "src/cli.py": ["stress.json"],            # python -m src.cli stress --json web/stress.json
                "tools/build_project_map.py": ["project_map.html"]}


def first_doc(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".py":
        try:
            doc = ast.get_docstring(ast.parse(text)) or ""
        except SyntaxError:
            doc = ""
        return doc.strip().splitlines()[0] if doc.strip() else ""
    if path.suffix == ".md":
        m = re.search(r"^description:\s*(.+)$", text, re.M)
        return m.group(1).strip() if m else ""
    if path.suffix == ".sh":
        for line in text.splitlines()[1:6]:
            if line.startswith("#") and line.strip("# ").strip():
                return line.strip("# ").strip()
        return ""
    if path.suffix == ".html":
        m = re.search(r"<title>([^<]+)</title>", text)
        return m.group(1).strip() if m else ""
    if path.suffix == ".js":
        m = re.search(r"/\*\*?\s*(.+?)\s*(?:\*/|\n)", text) or re.search(r"^//\s*(.+)$", text, re.M)
        return m.group(1).strip() if m else ""
    return ""


def io(path: Path, rel: str) -> tuple[list[str], list[str]]:
    """（書き出すファイル, 読むファイル）。docstring の中の言及は数えない。"""
    if path.suffix == ".html":
        text = path.read_text(encoding="utf-8", errors="replace")
        return [], sorted(set(re.findall(r'["\'/]([a-z_0-9]+\.json)["\']', text)) - {"seed.json", "r.json"})
    if path.suffix != ".py" or rel == "tools/build_project_map.py":
        return WRITES_EXTRA.get(rel, []), []
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        doc = ast.get_docstring(ast.parse(text)) or ""
    except SyntaxError:
        doc = ""
    body = text.replace(doc, "", 1) if doc else text
    writes, reads = set(WRITES_EXTRA.get(rel, [])), set()
    for line in body.splitlines():
        for m in NAME_RE.finditer(line):
            (writes if WRITE_LINE.search(line) else reads).add(m.group(1))
    return sorted(writes), sorted(reads - writes)


def test_counts() -> dict[str, int]:
    """モジュール名 → それを import しているテストの関数の数。"""
    out: dict[str, int] = {}
    for t in (ROOT / "tests").glob("test_*.py"):
        text = t.read_text(encoding="utf-8")
        n = len(re.findall(r"^def test_", text, re.M))
        mods = set(re.findall(r"from (src|tools)\.([a-z_0-9]+) import", text)) | \
            set(re.findall(r"from (src|tools) import ([a-z_0-9, ]+)", text))
        names = set()
        for pkg, mod in mods:
            for m in mod.split(","):
                names.add(f"{pkg}/{m.strip()}.py")
        # JS エンジンは node のランナー経由
        if "sim_parity_runner" in text or "future_runner" in text:
            names.add("web/simengine.js")
        for nm in names:
            out[nm] = out.get(nm, 0) + n
    return out


def cli_commands() -> list[dict]:
    text = (ROOT / "src" / "cli.py").read_text(encoding="utf-8")
    return [{"name": n, "help": h} for n, h in re.findall(r'add_parser\("([a-z-]+)", help="([^"]+)"', text)]


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def collect() -> dict:
    tests = test_counts()
    files = sorted(
        [*ROOT.glob("src/*.py"), *ROOT.glob("tools/*.py"), *ROOT.glob("tools/*.sh"),
         *(ROOT / "web").glob("*.html"), *(ROOT / "web").glob("*.js"), *(ROOT / ".claude" / "commands").glob("*.md")])
    programs = []
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        if p.name == "__init__.py":
            continue
        lines = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        if rel.startswith(".claude/commands/"):
            group, use = "cmd", ""
            name = "/" + p.stem
        else:
            group, use = META.get(rel, ("other", ""))
            name = rel
        writes, reads = io(p, rel)
        programs.append({"path": rel, "name": name, "group": group, "doc": first_doc(p), "use": use,
                         "lines": lines, "writes": writes, "reads": reads, "tests": tests.get(rel, 0)})
    # 他の tools のモジュールを import して読み込みを任せているもの（例: from tools.hedge_lab import load）
    by_path = {q["path"]: q for q in programs}
    for q in programs:
        if not q["path"].endswith(".py") or q["path"] == "tools/build_project_map.py":
            continue
        text = (ROOT / q["path"]).read_text(encoding="utf-8", errors="replace")
        for mod in re.findall(r"from tools\.([a-z_0-9]+) import", text):
            dep = by_path.get(f"tools/{mod}.py")
            if dep:
                q["reads"] = sorted(set(q["reads"]) | (set(dep["reads"]) - set(q["writes"])))
    data_files = []
    for j in sorted((ROOT / "web").glob("*.json")):
        makers = [q["path"] for q in programs if j.name in q["writes"]]
        users = [q["path"] for q in programs if j.name in q["reads"]]
        data_files.append({"name": j.name, "kb": round(j.stat().st_size / 1024), "made_by": makers, "read_by": users})
    n_tests = sum(len(re.findall(r"^def test_", t.read_text(encoding="utf-8"), re.M))
                  for t in (ROOT / "tests").glob("test_*.py"))
    py_lines = sum(q["lines"] for q in programs if q["path"].endswith(".py"))
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "groups": GROUPS,
        "programs": programs,
        "cli": cli_commands(),
        "data_files": data_files,
        "docs": sorted(p.name for p in (ROOT / "docs").glob("*.md")),
        "stats": {
            "py_files": sum(1 for q in programs if q["path"].endswith(".py")),
            "py_lines": py_lines,
            "web_lines": sum(q["lines"] for q in programs if q["path"].startswith("web/")),
            "tests": n_tests,
            "test_files": len(list((ROOT / "tests").glob("test_*.py"))),
            "commands": sum(1 for q in programs if q["group"] == "cmd"),
            "cli": len(cli_commands()),
            "commits": git("rev-list", "--count", "HEAD"),
            "last_commit": git("log", "-1", "--format=%cd", "--date=format:%Y-%m-%d"),
            "unclassified": [q["path"] for q in programs if q["group"] == "other"],
        },
    }


def build(outdir: Path) -> Path:
    html = TEMPLATE.read_text(encoding="utf-8")
    if MARK not in html:
        raise SystemExit(f"雛形に {MARK} がありません")
    blob = json.dumps(collect(), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / "project_map.html"
    out.write_text(html.replace(MARK, blob), encoding="utf-8")
    return out


def main() -> None:
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "reports"
    data = collect()
    if data["stats"]["unclassified"]:
        print("未分類（META に追加すると分類される）:", ", ".join(data["stats"]["unclassified"]))
    out = build(outdir)
    print(f"→ {out}（{out.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
