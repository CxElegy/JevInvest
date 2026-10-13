"""web/ledger.html を GitHub Pages 用の静的サイト（_site/）に組み立てる。標準ライブラリだけで動く。

    python tools/build_site.py                  # web/ → _site/
    python tools/build_site.py --strict         # 許可リストの JSON が欠けていたら止める（GitHub Actions）
    python tools/build_site.py && python -m http.server -d _site    # 手元で確かめる

公開先は https://cxelegy.github.io/JevInvest/ 。リポジトリが非公開でも、このサイトは
URL を知っている人なら誰でも見られる（noindex は検索に出さないだけで、アクセス制限ではない）。

作るもの:
  - index.html: web/ledger.html は claude.ai の Artifact 用の「断片」（<title>・<link>・<style> から始まり、
    <html>・<head>・<body> が無い）。先頭に並ぶ <title>・<link>・<meta>・<style> を <head> に移し、
    残りを <body> に入れた完全な HTML 文書にする。<meta charset>・viewport・robots（noindex）と、空のアイコン
    （無いとブラウザがサイトの外の /favicon.ico を探して 404 を出す）を付ける
  - 許可リスト（DATA_FILES）にあるデータだけをコピーする。web/ に他のファイルがあっても公開しない。
    seed.json（個人の明細）は許可リストに入れない
  - .nojekyll と 404.html

公開してはいけない文面（PRIVATE_TEXT: 勤務先の社内の仕組みの名前、独立性の確認の結果、年齢、使っている
カードなど）がページか JSON にあれば止まる。ledger.html は claude.ai の Artifact と同じファイルなので、
個人の事情は文面に書かず、公開しない seed.json から読む。

黙って直さない（CLAUDE.md）:
  - ledger.html の形が想定と違う（<title> が無い、すでに <html>・<body> がある）ときは止まる。
    ページの作りが変わったことに気づけるようにするため
  - 許可リストのファイルが無いときは警告を出して続ける（ページ側が読み込みエラーを表示する）。
    --strict（GitHub Actions で使う）なら止まる。JSON として読めないときは止まる
  - 公開しなかったファイルは名前を出す

出力先は毎回消して作り直す。消してよいのは、空のディレクトリか、前回このスクリプトが作ったもの
（.nojekyll の中身で見分ける）だけ。リポジトリの直下・web/・それらを含む場所・.git のある場所は
--force でも消さない。リポジトリの外に出すときは --force が要る。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from html.parser import HTMLParser
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "web"
OUT = ROOT / "_site"
PAGE = "ledger.html"

# 公開してよいデータ。ここに無いものは web/ にあっても公開しない（seed.json は絶対に入れない）
DATA_FILES = (
    "investors.json",          # tools/export_13f_json.py
    "evidence.json",           # tools/backtest_habits.py
    "strategy.json",           # tools/strategy_data.py
    "deepdive.json",           # tools/fund_deepdive.py
    "copycheck.json",          # tools/backtest_13f_copy.py --web
)
# serenity_history.json はページが読まない（投稿の抜粋と、持ち分の比率が入っている）ので公開しない。
# 使うタブを作るときに、抜粋を除いた形で加える
PERSONAL = ("seed.json",)      # 許可リストに入っていても公開しない

# 公開してはいけない文面。当たったらビルドを止める（大文字と小文字は区別する。日本語の前後は \b の境目に
# ならない（「DESCで」）ので、英数字の前後だけを見る）
PRIVATE_TEXT = (
    (r"(?<![A-Za-z0-9])DESC(?![A-Za-z0-9])", "勤務先の独立性のシステムの名前（DESC）"),
    (r"(?<![A-Za-z0-9])GIMS(?![A-Za-z0-9])", "勤務先の保有登録のシステムの名前（GIMS）"),
    (r"Pre-?Clear", "勤務先の事前承認の呼び名（Pre-Clear）"),
    (r"Independence\s*チーム", "勤務先の部署の名前"),
    (r"監査先|審査先|制限先", "勤務先の監査・制限の対象"),
    (r"不可だった", "独立性の確認の結果"),
    (r"[0-9０-９]+\s*歳で", "年齢"),
    (r"三井住友カード", "使っているカード"),
)

# 出力先の目印。.nojekyll にこの文字列が入っていれば、前回このスクリプトが作ったものとして消してよい
MARKER = "tools/build_site.py が作った公開用のディレクトリ。ビルドのたびに消して作り直す。\n"

DESCRIPTION = "持仓の配分・積立の計画・13F の参考データを見る個人用の台帳。入力した金額はこのブラウザ内だけに保存されます。"

HEAD_TAGS = ("title", "link", "meta", "style")
TEXT_TAGS = ("title", "style")     # 中にタグを持たず、閉じタグまでが中身
DOC_TAGS = ("html", "head", "body")

NOT_FOUND = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>ページがありません</title>
<style>
:root { --bg: #f2f5f3; --fg: #15201b; --muted: #5b6b64; --accent: #0e6b5b; color-scheme: light; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1311; --fg: #e5ece8; --muted: #93a49c; --accent: #52c2a6; color-scheme: dark; }
}
body { background: var(--bg); color: var(--fg); font-family: system-ui, -apple-system, 'Hiragino Sans', 'Yu Gothic', sans-serif;
  margin: 0; padding: 48px 16px; line-height: 1.65; }
main { max-width: 640px; margin: 0 auto; }
p { color: var(--muted); }
a { color: var(--accent); }
</style>
</head>
<body>
<main>
<h1>ページがありません</h1>
<p>このサイトにあるのは台帳のページだけです。</p>
<p><a id="home" href="./">台帳のページへ戻る</a></p>
</main>
<script>
// プロジェクトサイト（ユーザー名.github.io/リポジトリ名/）では、深い階層の 404 からも先頭へ戻す
(function () {
  var p = location.pathname.split('/');
  if (/\\.github\\.io$/.test(location.hostname) && p.length > 2 && p[1]) document.getElementById('home').href = '/' + p[1] + '/';
})();
</script>
</body>
</html>
"""


class BuildError(Exception):
    """公開物を作れない（作ると危ない）とき。"""


# ---- ledger.html（断片）の解析

class _Attrs(HTMLParser):
    """タグ 1 つ分の属性を取り出す。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.attrs: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if not self.attrs:
            self.attrs = {k.lower(): (v or "") for k, v in attrs}

    handle_startendtag = handle_starttag


def _attrs(tag_text: str) -> dict[str, str]:
    p = _Attrs()
    p.feed(tag_text)
    p.close()
    return p.attrs


class _DocTags(HTMLParser):
    """文書全体を読んで、<!doctype>・<html>・<head>・<body> があるか調べる。
    <script>・<style> の中身はタグとして読まれないので、JS の文字列に '<body>' があっても拾わない。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in DOC_TAGS:
            self.found.append(f"<{tag}>")

    handle_startendtag = handle_starttag

    def handle_decl(self, decl):
        if decl.lower().startswith("doctype"):
            self.found.append("<!doctype>")


_OPEN = re.compile(r"<([A-Za-z][A-Za-z0-9-]*)")


def _tag_end(text: str, i: int) -> int:
    """i（タグ名の直後）から読み、引用符の外にある '>' の次の位置を返す。"""
    quote = None
    for j in range(i, len(text)):
        c = text[j]
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == ">":
            return j + 1
    raise BuildError(f"タグが閉じていません: {text[i - 20:i + 40]!r}")


def split_fragment(text: str) -> tuple[str, list[dict], str]:
    """断片の先頭に並ぶ head 向けの要素（<title>・<link>・<meta>・<style>、間のコメント）と、残りに分ける。

    戻り値は (head に入れる部分, 先頭の要素の一覧 [{name, attrs}], body に入れる部分)。
    ファイル全体を正規表現で切るのではなく、先頭から要素を 1 つずつ読み、head 向けでない要素
    （または地の文）が出たところで止める。"""
    text = text.lstrip("﻿")
    i, n = 0, len(text)
    elements: list[dict] = []
    while True:
        j = i
        while j < n and text[j].isspace():
            j += 1
        if text.startswith("<!--", j):
            end = text.find("-->", j + 4)
            if end < 0:
                raise BuildError("先頭のコメントが閉じていません")
            i = end + 3
            continue
        m = _OPEN.match(text, j)
        if not m or m.group(1).lower() not in HEAD_TAGS:
            break
        name = m.group(1).lower()
        end = _tag_end(text, m.end())
        attrs = _attrs(text[j:end])
        if name in TEXT_TAGS:
            close = re.compile(rf"</{name}\s*>", re.IGNORECASE).search(text, end)
            if not close:
                raise BuildError(f"<{name}> が閉じていません")
            end = close.end()
        elements.append({"name": name, "attrs": attrs})
        i = end
    return text[:i].strip(), elements, text[i:].strip()


def render_index(fragment: str, source: str = PAGE) -> str:
    """断片から、GitHub Pages で開く完全な HTML 文書を作る。"""
    found = _DocTags()
    found.feed(fragment)
    found.close()
    if found.found:
        raise BuildError(
            f"{source} に {'・'.join(dict.fromkeys(found.found))} があります。このファイルは Artifact 用の断片"
            "（<html>・<body> なし）のはずです。完全な文書に変えたのなら tools/build_site.py も合わせて直してください")
    head, elements, body = split_fragment(fragment)
    names = [e["name"] for e in elements]
    if "title" not in names:
        raise BuildError(f"{source} の先頭に <title> がありません（<head> に入れる要素の並びが変わった可能性）")
    if not body:
        raise BuildError(f"{source} に本文がありません")

    metas = [e["attrs"] for e in elements if e["name"] == "meta"]
    named = {a.get("name", "").lower(): a.get("content", "") for a in metas if a.get("name")}
    if "robots" in named and "noindex" not in named["robots"].lower():
        raise BuildError(f"{source} の robots の指定に noindex がありません（個人用のページは検索に出さない）")

    lines = ["<!doctype html>", '<html lang="ja">', "<head>"]
    links = [e["attrs"] for e in elements if e["name"] == "link"]
    if not any("charset" in a for a in metas):
        lines.append('<meta charset="utf-8">')
    if "viewport" not in named:
        lines.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    if "robots" not in named:
        lines.append('<meta name="robots" content="noindex, nofollow">')
    if "description" not in named:
        lines.append(f'<meta name="description" content="{html.escape(DESCRIPTION)}">')
    if not any("icon" in a.get("rel", "").lower().split() for a in links):
        # 無いとブラウザが https://<ユーザー名>.github.io/favicon.ico（このサイトの外）を探して 404 を出す
        lines.append('<link rel="icon" href="data:,">')
    lines.append(f"<!-- tools/build_site.py が web/{source} から作ったもの。ここは直接直さない -->")
    lines += [head, "</head>", "<body>", body, "</body>", "</html>", ""]
    return "\n".join(lines)


def find_private(text: str, source: str) -> list[str]:
    """公開してはいけない文面（PRIVATE_TEXT）を探し、「ファイル 行: 何か（前後の文）」の一覧を返す。"""
    found = []
    for pattern, what in PRIVATE_TEXT:
        for m in re.finditer(pattern, text):
            line = text.count("\n", 0, m.start()) + 1
            near = text[max(m.start() - 15, 0):m.end() + 15].replace("\n", " ")
            found.append(f"{source} {line} 行: {what}（…{near}…）")
    return found


# ---- 出力先の確認

def _inside(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def check_out_dir(out: Path, src: Path, *, root: Path = ROOT, force: bool = False) -> Path:
    """出力先を消してよいか確かめ、絶対パスを返す。消してはいけない場所なら BuildError。"""
    if out.is_symlink():
        raise BuildError(f"出力先 {out} はシンボリックリンクです。消さずに止めます")
    out = out.resolve()
    root, src = root.resolve(), src.resolve()
    if _inside(root, out):
        raise BuildError(f"出力先 {out} はリポジトリの直下かその上です。消さずに止めます")
    if _inside(src, out):
        raise BuildError(f"出力先 {out} は公開元（{src}）かそれを含む場所です。消さずに止めます")
    if out.exists() and not out.is_dir():
        raise BuildError(f"出力先 {out} はディレクトリではありません")
    if (out / ".git").exists():
        raise BuildError(f"出力先 {out} に .git があります。消さずに止めます")
    if not _inside(out, root) and not force:
        raise BuildError(f"出力先 {out} はリポジトリの外です。そこに作るなら --force を付けてください")
    if out.is_dir() and any(out.iterdir()):
        mark = out / ".nojekyll"
        try:
            ours = mark.is_file() and mark.read_text(encoding="utf-8") == MARKER
        except (OSError, UnicodeDecodeError):
            ours = False
        if not ours:
            raise BuildError(f"出力先 {out} は空でなく、このスクリプトが作ったものでもありません"
                             "（.nojekyll の目印が無い）。消さずに止めます。別の出力先を指定してください")
    return out


# ---- 組み立て

def build(src: Path = SRC, out: Path = OUT, *, root: Path = ROOT, force: bool = False,
          strict: bool = False) -> dict:
    """src（web/）から out（_site/）を作る。結果（コピーしたもの・無かったもの・公開しなかったもの）を返す。
    strict なら、許可リストのファイルが 1 つでも無いときに止まる（公開先でタブが読み込みエラーになるため）。"""
    src = Path(src)
    page = src / PAGE
    if not page.is_file():
        raise BuildError(f"{page} がありません")
    index = render_index(page.read_text(encoding="utf-8"), PAGE)
    private = find_private(index, PAGE)

    leaked = sorted(set(DATA_FILES) & set(PERSONAL))
    if leaked:
        raise BuildError(f"許可リストに個人のファイル {'、'.join(leaked)} が入っています。公開しません")

    copied, missing, invalid = [], [], []
    for name in DATA_FILES:
        f = src / name
        if f.is_symlink():
            raise BuildError(f"{f} はシンボリックリンクです。中身を確かめられないので公開しません")
        if not f.is_file():
            missing.append(name)
            continue
        try:
            text = f.read_text(encoding="utf-8")
            json.loads(text)
        except (UnicodeDecodeError, ValueError) as e:
            invalid.append(f"{name}（{e}）")
            continue
        private += find_private(text, name)
        copied.append(name)
    if invalid:
        raise BuildError("JSON として読めないファイルがあります: " + "、".join(invalid))
    if private:
        raise BuildError("公開してはいけない文面があります。一般的な言い方に直すか、seed.json（公開しない）に移してください:\n  "
                         + "\n  ".join(private))
    if strict and missing:
        raise BuildError("許可リストのファイルがありません（--strict）: " + "、".join(str(src / n) for n in missing))
    skipped = sorted(p.name + ("/" if p.is_dir() else "") for p in src.iterdir()
                     if p.name != PAGE and p.name not in copied)

    out = check_out_dir(Path(out), src, root=root, force=force)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / ".nojekyll").write_text(MARKER, encoding="utf-8")
    (out / "index.html").write_text(index, encoding="utf-8", newline="\n")
    (out / "404.html").write_text(NOT_FOUND, encoding="utf-8", newline="\n")
    for name in copied:
        shutil.copyfile(src / name, out / name)

    print(f"公開する: index.html（{PAGE} から）・404.html・" + "・".join(copied))
    if skipped:
        notes = [n + ("（個人の明細。公開しない）" if n in PERSONAL else "") for n in skipped]
        print("公開しない（許可リストの外）: " + "、".join(notes))
    for name in missing:
        print(f"警告: {src / name} がありません。そのデータを使うタブは、ページ上で読み込みエラーを表示します",
              file=sys.stderr)
    print(f"→ {out}")
    return {"out": out, "copied": copied, "missing": missing, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=str(SRC), help="公開元（既定 web）")
    ap.add_argument("--out", default=str(OUT), help="出力先（既定 _site）。毎回消して作り直す")
    ap.add_argument("--force", action="store_true", help="リポジトリの外にも出力する")
    ap.add_argument("--strict", action="store_true", help="許可リストのファイルが無ければ止まる（GitHub Actions 用）")
    a = ap.parse_args(argv)
    try:
        build(Path(a.src), Path(a.out), force=a.force, strict=a.strict)
    except BuildError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
