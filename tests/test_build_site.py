"""tools/build_site.py（GitHub Pages 用の組み立て）のテスト。ネットワークには出ない。"""

import json
import re
from pathlib import Path

import pytest

import build_site as bs
from build_site import BuildError, build, check_out_dir, render_index, split_fragment

ROOT = Path(__file__).resolve().parents[1]

FRAGMENT = """<title>テスト台帳</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP&display=swap">
<!-- 先頭のコメント -->
<style>
:root { --bg: #fff; }
body { background: var(--bg); }
</style>

<div class="wrap">
  <nav class="tabs" role="tablist" aria-label="表示の切り替え">
    <button type="button" role="tab" id="tabRoad" aria-controls="viewRoad" aria-selected="true">道筋</button>
  </nav>
  <div id="viewRoad" class="view" role="tabpanel" aria-labelledby="tabRoad"></div>
</div>
<script>
fetch('seed.json').catch(function () {});
var s = '<body>' + '<html>';    // JS の文字列にあるタグは文書のタグではない
</script>
"""


def make_repo(tmp_path, fragment=FRAGMENT, data=None):
    """tmp_path/repo/web/ に断片とデータを置き、リポジトリの直下（root）を返す。"""
    root = tmp_path / "repo"
    web = root / "web"
    web.mkdir(parents=True)
    (web / "ledger.html").write_text(fragment, encoding="utf-8")
    for name, body in (data if data is not None else {n: {"ok": True} for n in bs.DATA_FILES}).items():
        (web / name).write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return root


def head_and_body(page: str) -> tuple[str, str]:
    head = page[page.index("<head>"):page.index("</head>")]
    body = page[page.index("<body>"):page.index("</body>")]
    return head, body


def test_index_is_a_full_document(tmp_path):
    root = make_repo(tmp_path)
    build(root / "web", root / "_site", root=root)
    page = (root / "_site" / "index.html").read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>\n<html lang=\"ja\">\n<head>\n<meta charset=\"utf-8\">")
    head, body = head_and_body(page)
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in head
    assert '<meta name="robots" content="noindex, nofollow">' in head
    assert '<meta name="description"' in head
    assert '<link rel="icon" href="data:,">' in head     # host 直下の /favicon.ico（サイトの外）を探させない
    # 先頭の title・link・コメント・style は head へ、残りは body へ
    assert "<title>テスト台帳</title>" in head and "<title>" not in body
    assert "fonts.googleapis.com" in head and "<style>" in head and "先頭のコメント" in head
    assert '<div class="wrap">' in body and 'role="tablist"' in body and 'id="tabRoad"' in body
    assert "fetch('seed.json')" in body
    assert page.rstrip().endswith("</body>\n</html>")
    tags = bs._DocTags()
    tags.feed(page)
    assert tags.found == ["<!doctype>", "<html>", "<head>", "<body>"]   # script の中の '<body>' は数えない


def test_split_fragment_reads_only_leading_head_elements():
    head, elements, body = split_fragment(
        '﻿<title>a</title>\n<link rel="x" href="q?a=1&b=\'>\'">\n<meta name="theme-color" content="#000">\n'
        "<style>p::before { content: '<div>'; }</style>\n<p>本文</p>\n<style>.late {}</style>\n")
    assert [e["name"] for e in elements] == ["title", "link", "meta", "style"]
    assert elements[2]["attrs"] == {"name": "theme-color", "content": "#000"}
    assert head.endswith("</style>") and "<p>" not in head
    assert body.startswith("<p>本文</p>") and ".late" in body   # 本文より後の style は動かさない


def test_fragment_meta_is_not_duplicated():
    page = render_index('<meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                        '<link rel="shortcut icon" href="i.png"><title>t</title><div></div>')
    head, _ = head_and_body(page)
    assert head.lower().count("charset") == 1 and head.count('name="viewport"') == 1
    assert '<meta name="robots" content="noindex, nofollow">' in head
    assert head.count('rel="icon"') == 0 and 'href="i.png"' in head     # 断片にアイコンがあれば足さない


@pytest.mark.parametrize("fragment", [
    "<div>本文だけ</div>",                                   # title が無い
    "<div>本文</div>\n<title>後ろの title</title>",           # 先頭に無い
    "<!doctype html><title>t</title><div></div>",
    "<html><title>t</title><div></div></html>",
    "<title>t</title><body><div></div></body>",
    "<title>t</title>",                                        # 本文が無い
    '<title>t</title><meta name="robots" content="index"><div></div>',
])
def test_unexpected_fragment_fails_loudly(fragment):
    with pytest.raises(BuildError):
        render_index(fragment)


def test_tags_inside_script_strings_are_not_document_tags():
    page = render_index("<title>t</title><div></div><script>var s = '<html><body>';</script>")
    assert "var s = '<html><body>';" in page


def test_only_allowlisted_files_are_copied(tmp_path, capsys):
    root = make_repo(tmp_path)
    web = root / "web"
    (web / "seed.json").write_text('{"profile": {"name": "個人"}}', encoding="utf-8")
    (web / "serenity_history.json").write_text('{"holdings": [{"weight": 0.1}]}', encoding="utf-8")
    (web / "notes.json").write_text("{}", encoding="utf-8")
    (web / "memo.txt").write_text("メモ", encoding="utf-8")
    (web / "private").mkdir()
    (web / "private" / "x.json").write_text("{}", encoding="utf-8")
    res = build(web, root / "_site", root=root)
    site = root / "_site"
    assert sorted(p.name for p in site.iterdir()) == sorted(
        [".nojekyll", "404.html", "index.html", *bs.DATA_FILES])
    assert not (site / "seed.json").exists() and not list(site.rglob("seed*"))
    assert res["skipped"] == ["memo.txt", "notes.json", "private/", "seed.json", "serenity_history.json"]
    assert "serenity_history.json" not in bs.DATA_FILES     # ページが読まない（投稿の抜粋と持ち分の比率が入っている）
    out = capsys.readouterr().out
    assert "公開しない" in out and "seed.json（個人の明細。公開しない）" in out and "memo.txt" in out
    assert json.loads((site / "investors.json").read_text(encoding="utf-8")) == {"ok": True}


def test_seed_json_never_published_even_if_allowlisted(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    (root / "web" / "seed.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(bs, "DATA_FILES", bs.DATA_FILES + ("seed.json",))
    with pytest.raises(BuildError, match="seed.json"):
        build(root / "web", root / "_site", root=root)
    assert not (root / "_site").exists()


def test_missing_allowlisted_file_only_warns(tmp_path, capsys):
    data = {n: {"ok": True} for n in bs.DATA_FILES if n != "copycheck.json"}
    root = make_repo(tmp_path, data=data)
    res = build(root / "web", root / "_site", root=root)
    assert res["missing"] == ["copycheck.json"]
    assert "copycheck.json" in capsys.readouterr().err
    assert (root / "_site" / "index.html").is_file() and not (root / "_site" / "copycheck.json").exists()


def test_strict_fails_when_allowlisted_file_is_missing(tmp_path, capsys):
    data = {n: {"ok": True} for n in bs.DATA_FILES if n != "copycheck.json"}
    root = make_repo(tmp_path, data=data)
    with pytest.raises(BuildError, match="copycheck.json"):
        build(root / "web", root / "_site", root=root, strict=True)
    assert not (root / "_site").exists()
    assert bs.main(["--src", str(root / "web"), "--out", str(root / "_site"), "--strict", "--force"]) == 1
    assert "--strict" in capsys.readouterr().err
    (root / "web" / "copycheck.json").write_text("{}", encoding="utf-8")
    assert bs.main(["--src", str(root / "web"), "--out", str(root / "_site"), "--strict", "--force"]) == 0


def test_workflow_builds_strictly():
    wf = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert "python tools/build_site.py --out _site --strict" in wf
    assert "persist-credentials: false" in wf


@pytest.mark.parametrize("text", [
    "<li><b>29 歳で、使う予定まで 15 年以上ある。</b></li>",
    "<p>買う前に DESC で購入可を確認する</p>",
    "<p>DESCで確認</p>",
    "<p>約定後は GIMS に保有を登録する</p>",
    "<p>Pre-Clear が通った運用会社</p>",
    "<p>Independence チームに確認</p>",
    "<td>独立性で不可（ファンド自体が監査先）</td>",
    "<p>三菱UFJ 系は不可だったため</p>",
    "<li>三井住友カードのクレカ積立</li>",
])
def test_private_text_stops_the_build(tmp_path, text):
    root = make_repo(tmp_path, fragment=FRAGMENT.replace('<div class="wrap">', '<div class="wrap">' + text))
    with pytest.raises(BuildError, match="公開してはいけない"):
        build(root / "web", root / "_site", root=root)
    assert not (root / "_site").exists()


def test_private_text_in_json_stops_the_build(tmp_path):
    root = make_repo(tmp_path)
    (root / "web" / "copycheck.json").write_text('{"note": "DESC と事前承認"}', encoding="utf-8")
    with pytest.raises(BuildError, match="copycheck.json"):
        build(root / "web", root / "_site", root=root)
    assert not (root / "_site").exists()


def test_private_text_check_ignores_ordinary_words():
    page = render_index("<title>t</title><div>description・desc・DESCRIPTION・38 歳から守りへ・（' + age + ' 歳）</div>")
    assert bs.find_private(page, "x") == []


def test_broken_json_fails_before_touching_out(tmp_path):
    root = make_repo(tmp_path)
    (root / "web" / "strategy.json").write_text("{壊れている", encoding="utf-8")
    with pytest.raises(BuildError, match="strategy.json"):
        build(root / "web", root / "_site", root=root)
    assert not (root / "_site").exists()


def test_nojekyll_and_404_written(tmp_path):
    root = make_repo(tmp_path)
    build(root / "web", root / "_site", root=root)
    site = root / "_site"
    assert (site / ".nojekyll").read_text(encoding="utf-8") == bs.MARKER
    nf = (site / "404.html").read_text(encoding="utf-8")
    assert nf.startswith("<!doctype html>") and 'href="./"' in nf and "noindex" in nf


def test_rebuild_wipes_previous_output(tmp_path):
    root = make_repo(tmp_path)
    build(root / "web", root / "_site", root=root)
    (root / "_site" / "stale.json").write_text("{}", encoding="utf-8")
    (root / "web" / "evidence.json").unlink()
    build(root / "web", root / "_site", root=root)
    assert not (root / "_site" / "stale.json").exists()
    assert not (root / "_site" / "evidence.json").exists()


def test_refuses_dangerous_out_paths(tmp_path):
    root = make_repo(tmp_path)
    web = root / "web"
    (root / "tools").mkdir()
    (root / "tools" / "keep.py").write_text("x = 1\n", encoding="utf-8")
    (root / "withgit" / ".git").mkdir(parents=True)
    for out in (root, web, tmp_path, root / "tools", root / "withgit"):
        for force in (False, True):
            with pytest.raises(BuildError):
                check_out_dir(out, web, root=root, force=force)
    with pytest.raises(BuildError, match="--force"):
        check_out_dir(tmp_path / "elsewhere", web, root=root)
    assert check_out_dir(tmp_path / "elsewhere", web, root=root, force=True) == (tmp_path / "elsewhere").resolve()
    assert check_out_dir(root / "_site", web, root=root) == (root / "_site").resolve()
    (root / "empty").mkdir()
    assert check_out_dir(root / "empty", web, root=root) == (root / "empty").resolve()
    # 何も消されていない
    assert (root / "tools" / "keep.py").is_file() and (web / "ledger.html").is_file()


def _looks_built(d: Path) -> None:
    """前回のビルドの目印を置く。中身の判定では止まらず、場所の判定だけで止まるかを確かめるため。"""
    d.mkdir(parents=True, exist_ok=True)
    (d / ".nojekyll").write_text(bs.MARKER, encoding="utf-8")


def test_refuses_repo_root_even_if_it_looks_built(tmp_path):
    root = make_repo(tmp_path)
    other_src = tmp_path / "elsewhere_web"           # 公開元がリポジトリの外でも、リポジトリ直下は消さない
    other_src.mkdir()
    _looks_built(root)
    _looks_built(tmp_path)
    for out in (root, tmp_path):
        with pytest.raises(BuildError, match="リポジトリの直下"):
            check_out_dir(out, other_src, root=root, force=True)


def test_refuses_src_dir_even_if_it_looks_built(tmp_path):
    root = make_repo(tmp_path)
    web = root / "web"
    _looks_built(web)
    with pytest.raises(BuildError, match="公開元"):
        check_out_dir(web, web, root=root, force=True)
    nested = root / "pub" / "web"                      # 公開元を中に含む場所も消さない
    nested.mkdir(parents=True)
    _looks_built(root / "pub")
    with pytest.raises(BuildError, match="公開元"):
        check_out_dir(root / "pub", nested, root=root, force=True)


def test_refuses_dir_with_git_even_if_it_looks_built(tmp_path):
    root = make_repo(tmp_path)
    out = root / "withgit"
    _looks_built(out)
    (out / ".git").mkdir()
    with pytest.raises(BuildError, match=".git"):
        check_out_dir(out, root / "web", root=root, force=True)


def test_refuses_symlinked_data_file(tmp_path):
    root = make_repo(tmp_path)
    real = tmp_path / "outside.json"
    real.write_text('{"ok": true}', encoding="utf-8")
    (root / "web" / "investors.json").unlink()
    (root / "web" / "investors.json").symlink_to(real)
    with pytest.raises(BuildError, match="シンボリックリンク"):
        build(root / "web", root / "_site", root=root)
    assert not (root / "_site").exists()


def test_refuses_symlinked_out(tmp_path):
    root = make_repo(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (root / "_site").symlink_to(target, target_is_directory=True)
    with pytest.raises(BuildError):
        check_out_dir(root / "_site", root / "web", root=root, force=True)


def test_main_reports_error_without_traceback(tmp_path, capsys):
    root = make_repo(tmp_path, fragment="<div>title なし</div>")
    assert bs.main(["--src", str(root / "web"), "--out", str(tmp_path / "out"), "--force"]) == 1
    assert "エラー" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_build_real_web(tmp_path):
    res = build(ROOT / "web", tmp_path / "_site", force=True)
    site = tmp_path / "_site"
    page = (site / "index.html").read_text(encoding="utf-8")
    head, body = head_and_body(page)
    assert "<title>" in head and 'role="tablist"' in body and 'id="tabRoad"' in body
    assert "noindex" in head and '<link rel="icon"' in head
    # 公開ページの最初に見える位置に「投資助言ではない」がある（タブより前）
    notice = "個人の記録と検証です。投資助言ではなく、特定の商品の売買を勧めるものではありません。"
    assert notice in body and body.index(notice) < body.index('role="tablist"')
    assert bs.find_private(page, "index.html") == []
    assert not (site / "seed.json").exists() and not (site / "serenity_history.json").exists()
    assert set(res["copied"]) <= set(bs.DATA_FILES)
    assert {p.name for p in site.iterdir()} <= {".nojekyll", "404.html", "index.html", *bs.DATA_FILES}
    for name in ("investors.json", "evidence.json", "strategy.json", "deepdive.json", "copycheck.json"):
        assert name in res["copied"]
    # ページが読むデータは、すべて許可リストに入っている（読まないものは公開しない）
    fetched = set(re.findall(r"fetch\('([\w.-]+\.json)'\)", (ROOT / "web" / "ledger.html").read_text(encoding="utf-8")))
    assert fetched - set(bs.PERSONAL) == set(bs.DATA_FILES)
