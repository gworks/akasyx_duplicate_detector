# test_csv_line_ui.py - UI が CSV の場所を拾う行と、detector のログの書式が揃っていることを確かめる（#26）
#
# UI（electron-ui/detector-output.js の CSV_LINE）は、ログの INFO 行の形（YYYY-MM-DD 時刻 INFO ロガー名: CSV report: パス）の
# ときだけ CSV の場所を拾い、「CSV を開く」ボタンを出す（失敗の WARNING の行にファイル名として同じ語が入っても取り違えない
# ように。#18）。そのため config.setup_logging の書式を変えると、ボタンが黙って出なくなる。ここでは本物のログの書式で
# 各コマンドを動かし、出た行が UI の正規表現に合うことを確かめる。
import logging
import os
import re

import main
import pytest
from conftest import archive_db_path, write_file

DETECTOR_OUTPUT_JS = os.path.join(os.path.dirname(__file__), "..", "..", "electron-ui", "detector-output.js")


def _ui_csv_line() -> re.Pattern:
    """UI の正規表現（electron-ui/detector-output.js の CSV_LINE）を、写しではなく本物から読みます。

    UI 側を書き換えたときも、ずれに気づけるように。使っているのは JavaScript と Python で意味の同じ書き方だけ。
    """
    with open(DETECTOR_OUTPUT_JS, encoding="utf-8") as f:
        m = re.search(r"^const CSV_LINE = /(.+)/([a-z]*);$", f.read(), re.M)
    assert m, "CSV_LINE not found in detector-output.js"
    assert m.group(2) == "", "flags are not translated"
    return re.compile(m.group(1))


@pytest.fixture
def real_logging(monkeypatch):
    """config.setup_logging の書式でコンソール（stderr）に出させる。

    pytest は root ロガーに自分のハンドラを付けるので、そのままでは setup_logging の basicConfig が何もしない（ログが
    stderr に出ない）。basicConfig の代わりに、setup_logging が渡した書式・ハンドラをそのまま root に足す（pytest の
    ハンドラは触らない）。終わったら足したハンドラを閉じて外す。
    """
    root = logging.getLogger()
    level = root.level
    ours = []

    def basic_config(**kwargs):
        # 本物では 1 回の実行が 1 つのプロセス。コマンドごとに、そのコマンドの setup_logging が作ったハンドラに付け替える
        # （前のコマンドのハンドラで書き続けると、コマンドごとの設定の違いを確かめられない）
        for h in ours:
            root.removeHandler(h)
            h.close()
        ours.clear()
        formatter = logging.Formatter(kwargs.get("format"), kwargs.get("datefmt"), kwargs.get("style", "%"))
        for h in kwargs.get("handlers") or []:
            if h.formatter is None:  # 本物の basicConfig と同じく、ハンドラ自身の書式があればそれを使う
                h.setFormatter(formatter)
            root.addHandler(h)
            ours.append(h)
        if kwargs.get("level") is not None:
            root.setLevel(kwargs["level"])

    monkeypatch.setattr(logging, "basicConfig", basic_config)
    yield
    for h in ours:
        root.removeHandler(h)
        h.close()
    root.setLevel(level)


def _csv_paths(stderr: str) -> list[str]:
    # 読むのはテストの中（detector-output.js が無い・形が変わったときに、モジュールの読み込みではなくこのテストが落ちる）
    csv_line = _ui_csv_line()
    return [m.group(1) for m in map(csv_line.match, stderr.splitlines()) if m]


def _run(argv, capsys) -> list[str]:
    capsys.readouterr()
    assert main.main(argv) == main.EXIT_OK
    paths = _csv_paths(capsys.readouterr().err)
    assert len(paths) == 1, "UI の CSV_LINE に合う CSV の行が 1 行だけ出る"
    assert os.path.isfile(paths[0]), "拾ったパスが実在する CSV（パスの後ろに余計な文字が付かない）"
    return paths


def test_csv_line_of_every_command_matches_the_ui_regex(tmp_path, fake_crawler, capsys, real_logging):
    common = [
        "--archive-db", archive_db_path(tmp_path),
        "--db-dir", str(tmp_path / "dbs"), "--log-dir", str(tmp_path / "log"),
        # 開発機の本物の crawler に頼らない（CI には無い）。走査は fake_crawler が差し替える
        "--crawler-repo", str(tmp_path / "no_crawler"),
    ]
    archive = str(tmp_path / "archive")
    os.makedirs(archive)
    source = str(tmp_path / "in")
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "crawl_in.db"))
    _run(["add", archive, source, *common], capsys)

    # 同じ中身をもう一度入れる → 重複として投入元に残る（delete-duplicates の対象）
    again = str(tmp_path / "again")
    write_file(os.path.join(again, "a.txt"), b"AAA")
    fake_crawler(again, str(tmp_path / "crawl_again.db"))
    _run(["add", archive, again, *common], capsys)
    _run(["delete-duplicates", archive, *common], capsys)

    fake_crawler(archive, str(tmp_path / "crawl_archive.db"))
    _run(["verify", archive, *common], capsys)

    # 記録の無いファイルの入った保存フォルダを登録する
    other = str(tmp_path / "other")
    write_file(os.path.join(other, "b.txt"), b"BBBB")
    fake_crawler(other, str(tmp_path / "crawl_other.db"))
    _run(["adopt", other, *common], capsys)
