# test_packaging.py - 配布版のビルド（packaging/build_python.sh）の設定を確かめる
#
# 配布版の実行形式は PYTHONUNBUFFERED・PYTHONIOENCODING を読まないため、同じことをビルドで指定している（#24）。
# 抜けると、crawler の進み具合（Processing files: n/全体）が一覧作成の終わり近くまでまとめて届いて UI が止まって
# 見える。ロケールが utf-8 でない環境（Windows など）では stdout が utf-8 にならず、UI で日本語のパスが化ける。
import os
import re
import shutil
import subprocess

import pytest

BUILD_PYTHON = os.path.join(os.path.dirname(__file__), "..", "..", "packaging", "build_python.sh")


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def _pyinstaller_calls() -> list[str]:
    """pyinstaller を呼ぶコマンド（行末の \\ で続く行も含めて 1 つ）の一覧を返します。

    説明のコメントにオプションが書いてあっても数えない。続きの途中のコメントの行は、bash と同じくそこでコマンドが
    終わったものとして扱う（その後ろのオプションは PyInstaller に渡らない）。
    """
    with open(BUILD_PYTHON, encoding="utf-8") as f:
        lines = [ln.rstrip("\r\n") for ln in f]
    calls = []
    for i, ln in enumerate(lines):
        if not _is_comment(ln) and re.search(r"\bpyinstaller main\.py\b", ln):
            call = [ln]
            while call[-1].endswith("\\") and i + len(call) < len(lines) and not _is_comment(lines[i + len(call)]):
                call.append(lines[i + len(call)])
            calls.append("\n".join(call))
    return calls


def _the_pyinstaller_call() -> str:
    # detector と crawler は同じ呼び出し（pyi()）で固める。下のオプションが両方に効くことの前提
    calls = _pyinstaller_calls()
    assert len(calls) == 1, calls
    return calls[0]


def test_packaged_executables_do_not_buffer_output():
    assert re.search(r"--python-option\s+u\b", _the_pyinstaller_call())


def test_packaged_executables_write_utf8():
    assert re.search(r"--python-option\s+[\"']X utf8[\"']", _the_pyinstaller_call())


def test_pyinstaller_supports_python_option():
    # --python-option は PyInstaller 6 から。古い版が使われるとビルドが失敗する
    m = re.search(r"--with\s+[\"']?pyinstaller>=(\d+)(?:\.\d+)*(?:[,\"'\s]|$)", _the_pyinstaller_call())
    assert m and int(m.group(1)) >= 6


needs_bash = pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="needs bash on a POSIX host (skipped on Windows; CI runs it on Linux)")


def _run_build_with_crawler(tmp_path, crawler_main):
    """兄弟の並び（akasyx_duplicate_detector と akasyx_crawler）を tmp に作って build_python.sh を実行します。"""
    repo = tmp_path / "akasyx_duplicate_detector"
    (repo / "packaging").mkdir(parents=True)
    shutil.copy(BUILD_PYTHON, repo / "packaging" / "build_python.sh")
    if crawler_main is not None:
        crawler = tmp_path / "akasyx_crawler" / "crawler"
        crawler.mkdir(parents=True)
        (crawler / "main.py").write_text(crawler_main, encoding="utf-8")
    r = subprocess.run(["bash", str(repo / "packaging" / "build_python.sh")], capture_output=True, encoding="utf-8",
                       errors="replace", timeout=60)
    return r, repo


@needs_bash
def test_build_stops_with_a_crawler_that_prints_japanese_progress(tmp_path):
    # akasyx_crawler v0.1.1 までは進み具合を日本語で出す。UI が読めないので、固める前に止める
    r, repo = _run_build_with_crawler(tmp_path, 'print(f"ファイル処理中: {n}/{total}")\n')
    assert r.returncode == 1
    assert "古い版" in r.stderr
    assert not (repo / "build").exists()  # 何も固めていない


@needs_bash
def test_build_passes_the_guard_with_a_crawler_that_prints_english_progress(tmp_path):
    # UI が読める crawler はガードを通る（この先は tmp に detector が無いので失敗するが、ガードの案内は出ない）
    r, repo = _run_build_with_crawler(tmp_path, 'print(f"Processing files: {n}/{total} ({pct:.1f}%)")\n')
    assert "古い版" not in r.stderr
    assert "がありません" not in r.stderr
    assert "== akasyx-detector" in r.stdout  # ガードの後、detector を固める所まで進んだ
    assert r.returncode != 0  # tmp に detector が無いので、そこで止まる（PyInstaller は動かない）
    assert "akasyx_duplicate_detector/detector" in r.stderr  # 止まった理由は detector が無いこと


@needs_bash
def test_build_stops_when_a_japanese_progress_line_remains(tmp_path):
    # 全体が分かるときの行だけ英語にした crawler は、全体が分からないときに日本語の行を出す。UI が読めないので止める
    r, _ = _run_build_with_crawler(
        tmp_path, 'print(f"Processing files: {n}/{total}")\nprint(f"ファイル処理中: {n}/不明")\n'
    )
    assert r.returncode == 1
    assert "古い版" in r.stderr


@needs_bash
def test_build_ignores_the_old_japanese_line_in_a_comment(tmp_path):
    # 英語で出す crawler のコメントに昔の日本語の行が書いてあっても、古い版とは見なさない
    r, _ = _run_build_with_crawler(
        tmp_path, '# 旧: ファイル処理中: n/全体\nprint(f"Processing files: {n}/{total}")\n'
    )
    assert "古い版" not in r.stderr
    assert "がありません" not in r.stderr
    assert "akasyx_duplicate_detector/detector" in r.stderr  # 止まった理由は detector が無いこと（ガードの後）
    assert "== akasyx-detector" in r.stdout


@needs_bash
def test_build_ignores_the_english_line_in_a_comment(tmp_path):
    # 日本語で出す crawler のコメントに英語の行が書いてあっても通さない
    r, _ = _run_build_with_crawler(
        tmp_path, '# TODO: Processing files: n/total in English\nprint(f"ファイル処理中: {n}/{total}")\n'
    )
    assert r.returncode == 1
    assert "古い版" in r.stderr


@needs_bash
def test_build_says_the_crawler_is_missing(tmp_path):
    r, _ = _run_build_with_crawler(tmp_path, None)
    assert r.returncode == 1
    assert "がありません" in r.stderr
    assert "古い版" not in r.stderr
