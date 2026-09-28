# test_archives_ui.py - UI から保存フォルダの一覧・登録を外す・adopt を扱うための出力（#11）
#
# UI（electron-ui/detector-output.js）は detector の出力を読んで導線を出す。ここで守るのは
# - `archives --json` が stdout に JSON だけを出す（ログが混ざると UI が一覧を読めない）
# - `Note:` 行と adopt の案内が、UI の正規表現が拾える形で出る
import json
import os
import re
import shutil

import main
from conftest import archive_db_path, write_file
from database import get_session
from models import Archive

# electron-ui/detector-output.js の NOTE_FORGET_LINE / ADOPT_HINT_LINE と同じ
NOTE_FORGET_LINE = re.compile(r"^Note: .*archive folder #(\d+) .*`archives --forget (\d+)`")
ADOPT_HINT_LINE = re.compile(r"(?:Register the files in it first|Run adopt again): adopt (.+)$")


def _add(make_config, root, source, tmp_path, fake_crawler):
    fake_crawler(source, str(tmp_path / f"crawl_{os.path.basename(source)}.db"))
    return main.run(make_config(archive_root=root, source_path=source))


def _dirs(tmp_path, *names):
    out = []
    for n in names:
        (tmp_path / n).mkdir()
        out.append(str(tmp_path / n))
    return out


def _archive_id_of(tmp_path, root):
    sess, engine = get_session(archive_db_path(tmp_path))
    try:
        return sess.query(Archive).filter_by(root_abs=os.path.realpath(root)).one().id
    finally:
        sess.close(); engine.dispose()


def _list_json(tmp_path, capsys):
    capsys.readouterr()
    argv = ["archives", "--json", "--archive-db", archive_db_path(tmp_path)]
    assert main.main(argv) == main.EXIT_OK
    return json.loads(capsys.readouterr().out)  # stdout は JSON だけ


def test_archives_json_lists_state_of_each_archive(make_config, tmp_path, fake_crawler, capsys):
    t, r, src_t, src_r = _dirs(tmp_path, "trial", "real", "in_t", "in_r")
    write_file(os.path.join(src_t, "x.txt"), b"AAAA")
    write_file(os.path.join(src_r, "y.txt"), b"BBBBBB")
    assert _add(make_config, t, src_t, tmp_path, fake_crawler) == main.EXIT_OK
    assert _add(make_config, r, src_r, tmp_path, fake_crawler) == main.EXIT_OK
    tid = _archive_id_of(tmp_path, t)
    shutil.rmtree(t)

    data = _list_json(tmp_path, capsys)
    assert data["master_db"] == archive_db_path(tmp_path)
    assert data["db_exists"] is True
    by_id = {a["id"]: a for a in data["archives"]}
    trial = by_id[tid]
    assert trial["root"] == os.path.realpath(t)
    assert trial["present"] is False
    assert trial["stored_files"] == 1 and trial["stored_bytes"] == 4
    assert trial["forgotten_files"] == 0
    assert trial["pending_adoption"] is False
    assert trial["last_used_at"]  # ISO 8601
    real = by_id[_archive_id_of(tmp_path, r)]
    assert real["present"] is True and real["stored_files"] == 1

    # 登録を外すと stored が forgotten に移る（UI はこれでボタンを消す）
    argv = ["archives", "--forget", str(tid), "--archive-db", archive_db_path(tmp_path)]
    assert main.main(argv) == main.EXIT_OK
    trial = {a["id"]: a for a in _list_json(tmp_path, capsys)["archives"]}[tid]
    assert trial["stored_files"] == 0 and trial["forgotten_files"] == 1


def test_archives_json_with_no_archives(tmp_path, capsys):
    from database import get_session
    sess, engine = get_session(archive_db_path(tmp_path))  # 登録 0 件の正本 DB
    sess.close(); engine.dispose()
    data = _list_json(tmp_path, capsys)
    assert data["archives"] == [] and data["db_exists"] is True


def test_note_line_names_the_archive_to_forget(make_config, tmp_path, fake_crawler, capsys):
    """消した保存フォルダにしか無い重複の Note: 行から、UI が保存フォルダの ID を拾える。"""
    t, r, src_t, src_r = _dirs(tmp_path, "trial", "real", "in_t", "in_r")
    write_file(os.path.join(src_t, "x.txt"), b"SAME")
    assert _add(make_config, t, src_t, tmp_path, fake_crawler) == main.EXIT_OK
    tid = _archive_id_of(tmp_path, t)
    shutil.rmtree(t)
    write_file(os.path.join(src_r, "y.txt"), b"SAME")
    capsys.readouterr()
    assert _add(make_config, r, src_r, tmp_path, fake_crawler) == main.EXIT_OK
    lines = [m for m in map(NOTE_FORGET_LINE.match, capsys.readouterr().out.splitlines()) if m]
    assert [(int(m.group(1)), int(m.group(2))) for m in lines] == [(tid, tid)]


def test_adopt_hint_names_the_folder(make_config, tmp_path, fake_crawler, capsys):
    """中身のあるフォルダを断るときの案内から、UI が adopt するフォルダを拾える。"""
    root, src = _dirs(tmp_path, "photos", "in")
    write_file(os.path.join(root, "old.jpg"), b"OLD")
    write_file(os.path.join(src, "new.jpg"), b"NEW")
    fake_crawler(src, str(tmp_path / "crawl.db"))
    capsys.readouterr()
    argv = [
        "add", root, src, "--archive-db", archive_db_path(tmp_path),
        "--db-dir", str(tmp_path / "dbs"), "--log-dir", str(tmp_path / "log"),
    ]
    assert main.main(argv) == main.EXIT_REJECTED
    hints = [m.group(1) for m in map(ADOPT_HINT_LINE.search, capsys.readouterr().err.splitlines()) if m]
    assert hints == [os.path.realpath(root)]


def test_adopt_hint_is_not_offered_for_folder_of_another_master_db(tmp_path, capsys):
    """識別子のある（別の正本 DB で使っていたかもしれない）フォルダの断りでは、adopt の導線を出さない。
    正しい対処は --archive-db の指定で、adopt すると同じ保存フォルダが 2 つの正本 DB に登録される。"""
    root, src = _dirs(tmp_path, "photos", "in")
    write_file(os.path.join(root, ".akasyx", "archive.id"), b"0123456789abcdef0123456789abcdef\n")
    write_file(os.path.join(root, "old.jpg"), b"OLD")
    write_file(os.path.join(src, "new.jpg"), b"NEW")
    capsys.readouterr()
    argv = [
        "add", root, src, "--archive-db", archive_db_path(tmp_path),
        "--db-dir", str(tmp_path / "dbs"), "--log-dir", str(tmp_path / "log"),
    ]
    assert main.main(argv) == main.EXIT_REJECTED
    err = capsys.readouterr().err
    assert "--archive-db" in err
    assert not [m for m in map(ADOPT_HINT_LINE.search, err.splitlines()) if m]


def test_forget_refuses_when_uid_does_not_match(make_config, tmp_path, fake_crawler, capsys):
    """UI は一覧で見た保存フォルダの uid を --expect-uid で渡す。別の正本 DB・別の保存フォルダの同じ ID は外さない。"""
    t, src_t = _dirs(tmp_path, "trial", "in_t")
    write_file(os.path.join(src_t, "x.txt"), b"AAAA")
    assert _add(make_config, t, src_t, tmp_path, fake_crawler) == main.EXIT_OK
    tid = _archive_id_of(tmp_path, t)
    shutil.rmtree(t)
    base = ["archives", "--forget", str(tid), "--archive-db", archive_db_path(tmp_path)]
    assert main.main(base + ["--expect-uid", "ffffffffffffffffffffffffffffffff"]) == main.EXIT_REJECTED
    trial = {a["id"]: a for a in _list_json(tmp_path, capsys)["archives"]}[tid]
    assert trial["stored_files"] == 1  # 外していない
    assert main.main(base + ["--expect-uid", trial["uid"]]) == main.EXIT_OK
    trial = {a["id"]: a for a in _list_json(tmp_path, capsys)["archives"]}[tid]
    assert trial["forgotten_files"] == 1


def test_archives_json_does_not_create_missing_master_db(tmp_path, capsys):
    """一覧は読むだけ。正本 DB が無ければ作らず、無いことを返す（UI の入力途中のパスに DB を作らない）。"""
    db = str(tmp_path / "not" / "yet" / "archive.db")
    capsys.readouterr()
    argv = ["archives", "--json", "--archive-db", db, "--log-dir", str(tmp_path / "log")]
    assert main.main(argv) == main.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data == {"master_db": db, "db_exists": False, "archives": []}
    assert not os.path.exists(tmp_path / "not")


def test_expect_uid_accepts_any_uid_text():
    """uid は識別子ファイルの中身そのまま。UI は --expect-uid=<uid> で渡すので、先頭が - でも受け取れる。"""
    import config as config_module

    config = config_module.parse_arguments(["archives", "--forget", "3", "--expect-uid=-odd uid!"])
    assert config.forget_id == 3 and config.expect_uid == "-odd uid!"


def test_archives_text_list_does_not_create_missing_master_db(tmp_path, capsys):
    """テキストの一覧（UI の保存フォルダタブの Run）も読むだけ。無い正本 DB は作らない。"""
    db = str(tmp_path / "typo" / "archive.db")
    argv = ["archives", "--archive-db", db, "--log-dir", str(tmp_path / "log")]
    assert main.main(argv) == main.EXIT_OK
    out = capsys.readouterr().out
    assert "not found" in out and db in out
    assert not os.path.exists(tmp_path / "typo")


def test_forget_does_not_create_missing_master_db(tmp_path):
    db = str(tmp_path / "typo" / "archive.db")
    argv = ["archives", "--forget", "1", "--archive-db", db, "--log-dir", str(tmp_path / "log")]
    assert main.main(argv) == main.EXIT_REJECTED
    assert not os.path.exists(tmp_path / "typo")
