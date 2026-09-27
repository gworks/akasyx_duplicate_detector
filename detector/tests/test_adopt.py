# test_adopt.py - 中身のある保存フォルダを実体から登録し直す（#4 / #5）
#
# 中身があるのに保存記録が 1 件も無い保存フォルダは、adopt 以外の全コマンドで断る。
# adopt は保存フォルダを走査し、実体を stored として登録する（同じ内容は全保存フォルダで 1 つ。
# 2 つ目以降は unregistered にして archive_duplicate として報告する）。
import os
import shutil

import pytest

import crawler_client
import main
from conftest import archive_db_path, write_file
from database import archive_id_path, get_session
from errors import PreflightError
from models import (
    MODE_ADOPT,
    MODE_REPORT,
    MODE_VERIFY,
    RESULT_ARCHIVE_DUPLICATE,
    STATUS_STORED,
    STATUS_UNREGISTERED,
    Archive,
    ArchiveFile,
    IngestItem,
)


def _db(tmp_path):
    return get_session(archive_db_path(tmp_path))


def _rows(tmp_path, **filters):
    sess, engine = _db(tmp_path)
    try:
        return [
            (r.stored_path_rel, r.status)
            for r in sess.query(ArchiveFile).filter_by(**filters).order_by(ArchiveFile.stored_path_rel)
        ]
    finally:
        sess.close(); engine.dispose()


def _adopt(make_config, root, tmp_path, fake_crawler):
    fake_crawler(root, str(tmp_path / "adopt_crawl.db"))
    return main.run(make_config(mode=MODE_ADOPT, archive_root=root))


@pytest.fixture
def photos(tmp_path):
    """中身のある既存フォルダ（まだ保存フォルダではない）。"""
    root = tmp_path / "photos"
    write_file(str(root / "2024" / "a.jpg"), b"AAAA")
    write_file(str(root / "2024" / "b.jpg"), b"BBBB")
    return str(root)


# --- #4: 中身のあるフォルダを初めて保存フォルダにする -------------------------------


def test_add_refuses_folder_with_content_and_no_records(make_config, tmp_path, fake_crawler, photos):
    """中身のあるフォルダを取り込み先にすると、何も書かずに断り、adopt を案内する。"""
    src = str(tmp_path / "inbox")
    write_file(os.path.join(src, "a.jpg"), b"AAAA")
    fake_crawler(src, str(tmp_path / "c.db"))
    with pytest.raises(PreflightError, match="adopt"):
        main.run(make_config(archive_root=photos, source_path=src))
    assert os.path.exists(os.path.join(src, "a.jpg"))
    assert not os.path.exists(archive_id_path(photos))  # 断る前に識別子を書かない


@pytest.mark.parametrize("mode", [MODE_VERIFY, MODE_REPORT])
def test_other_commands_also_refuse_until_adopted(make_config, tmp_path, fake_crawler, photos, mode):
    fake_crawler(photos, str(tmp_path / "c.db"))
    with pytest.raises(PreflightError, match="adopt"):
        main.run(make_config(mode=mode, archive_root=photos))


def test_adopt_registers_contents_as_stored(make_config, tmp_path, fake_crawler, photos):
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    assert _rows(tmp_path) == [("2024/a.jpg", STATUS_STORED), ("2024/b.jpg", STATUS_STORED)]
    assert os.path.exists(archive_id_path(photos))
    # adopt した内容と同じファイルは、取り込みで重複として投入元に残る（#4 の重複が入らない）
    src = str(tmp_path / "inbox")
    write_file(os.path.join(src, "copy_of_a.jpg"), b"AAAA")
    fake_crawler(src, str(tmp_path / "c2.db"))
    assert main.run(make_config(archive_root=photos, source_path=src)) == main.EXIT_OK
    assert os.path.exists(os.path.join(src, "copy_of_a.jpg"))


def test_empty_folder_still_opens_without_adopt(make_config, tmp_path, fake_crawler):
    """空のフォルダはこれまでどおり、そのまま保存フォルダにできる。"""
    empty = str(tmp_path / "empty_archive")
    os.makedirs(empty)
    src = str(tmp_path / "inbox")
    write_file(os.path.join(src, "a.jpg"), b"AAAA")
    fake_crawler(src, str(tmp_path / "c.db"))
    assert main.run(make_config(archive_root=empty, source_path=src)) == main.EXIT_OK


def test_files_copied_into_registered_empty_archive_need_adopt(make_config, tmp_path, fake_crawler):
    """記録が 0 件の保存フォルダに、あとから手でファイルを入れた場合も adopt を求める。"""
    arch = str(tmp_path / "arch")
    os.makedirs(arch)
    assert main.run(make_config(mode=MODE_REPORT, archive_root=arch)) == main.EXIT_OK  # 空で登録
    write_file(os.path.join(arch, "manual", "x.jpg"), b"XXXX")
    with pytest.raises(PreflightError, match="adopt"):
        main.run(make_config(mode=MODE_REPORT, archive_root=arch))
    assert _adopt(make_config, arch, tmp_path, fake_crawler) == main.EXIT_OK
    assert _rows(tmp_path) == [("manual/x.jpg", STATUS_STORED)]


# --- #5: 正本 DB を失った保存フォルダを復旧する -------------------------------------


def test_adopt_recovers_archive_after_master_db_is_lost(make_config, tmp_path, fake_crawler, photos):
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    with open(archive_id_path(photos), encoding="utf-8") as f:
        uid = f.read().strip()
    os.remove(archive_db_path(tmp_path))  # 正本 DB を失った（別の Mac への移行など）
    for sfx in ("-wal", "-shm"):
        if os.path.exists(archive_db_path(tmp_path) + sfx):
            os.remove(archive_db_path(tmp_path) + sfx)
    with pytest.raises(PreflightError, match="adopt"):
        main.run(make_config(mode=MODE_REPORT, archive_root=photos))
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    sess, engine = _db(tmp_path)
    try:
        assert sess.query(Archive).one().uid == uid  # 識別子を引き継ぐ
    finally:
        sess.close(); engine.dispose()
    assert _rows(tmp_path, status=STATUS_STORED) == [
        ("2024/a.jpg", STATUS_STORED), ("2024/b.jpg", STATUS_STORED)
    ]


# --- 重複 ---------------------------------------------------------------------------


def test_duplicates_inside_the_folder_keep_one_stored(make_config, tmp_path, fake_crawler, photos):
    write_file(os.path.join(photos, "2025", "a_copy.jpg"), b"AAAA")
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    assert _rows(tmp_path) == [
        ("2024/a.jpg", STATUS_STORED),
        ("2024/b.jpg", STATUS_STORED),
        ("2025/a_copy.jpg", STATUS_UNREGISTERED),
    ]
    sess, engine = _db(tmp_path)
    try:
        item = sess.query(IngestItem).filter_by(name="a_copy.jpg").one()
        assert item.result == RESULT_ARCHIVE_DUPLICATE
    finally:
        sess.close(); engine.dispose()
    assert os.path.exists(os.path.join(photos, "2025", "a_copy.jpg"))  # ファイルは動かさない


def test_content_stored_in_another_archive_is_not_stored_again(make_config, tmp_path, fake_crawler, photos):
    other = str(tmp_path / "other")
    os.makedirs(other)
    src = str(tmp_path / "inbox")
    write_file(os.path.join(src, "a.jpg"), b"AAAA")
    fake_crawler(src, str(tmp_path / "c.db"))
    assert main.run(make_config(archive_root=other, source_path=src)) == main.EXIT_OK
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    assert ("2024/a.jpg", STATUS_UNREGISTERED) in _rows(tmp_path)
    assert ("2024/b.jpg", STATUS_STORED) in _rows(tmp_path)


# --- 再実行・途中失敗 -----------------------------------------------------------------


def test_adopt_twice_is_refused(make_config, tmp_path, fake_crawler, photos):
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    with pytest.raises(PreflightError, match="verify"):
        _adopt(make_config, photos, tmp_path, fake_crawler)


def test_interrupted_adopt_leaves_no_records_and_can_be_retried(
    make_config, tmp_path, fake_crawler, photos, monkeypatch
):
    def _broken(*a, **k):
        raise RuntimeError("crawler crashed")

    monkeypatch.setattr(crawler_client, "resolve_crawler_repo", lambda path: path)
    monkeypatch.setattr(crawler_client, "run_crawler", _broken)
    assert main.run(make_config(mode=MODE_ADOPT, archive_root=photos)) == main.EXIT_FATAL
    assert _rows(tmp_path) == []
    with pytest.raises(PreflightError, match="adopt"):  # 途中で落ちても他のコマンドは断ったまま
        main.run(make_config(mode=MODE_REPORT, archive_root=photos))
    monkeypatch.undo()
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    assert len(_rows(tmp_path, status=STATUS_STORED)) == 2


# --- 登録しないもの ---------------------------------------------------------------------


def test_adopt_skips_metadata_and_empty_files(make_config, tmp_path, fake_crawler, photos):
    write_file(os.path.join(photos, ".DS_Store"), b"junk")
    write_file(os.path.join(photos, "2024", "empty.txt"), b"")
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    names = [p for p, _ in _rows(tmp_path)]
    assert names == ["2024/a.jpg", "2024/b.jpg"]


def test_unreadable_files_are_reported_and_fail_the_run(make_config, tmp_path, monkeypatch, photos):
    """ハッシュを取れなかったファイルは登録せず、件数を出して終了コードを 0 以外にする。"""
    from conftest import build_crawler_db
    import sqlite3
    crawl_db = str(tmp_path / "c.db")

    def _run(target, config, extra_excludes=()):
        scan_id = build_crawler_db(crawl_db, target, excludes=tuple(extra_excludes))
        conn = sqlite3.connect(crawl_db)
        conn.execute("UPDATE fs_files SET filehash = NULL, hash_algo = NULL WHERE name = 'b.jpg'")
        conn.commit(); conn.close()
        return crawler_client.CrawlerScan(db_path=crawl_db, scan_id=scan_id, status="completed", root_dir=target)

    monkeypatch.setattr(crawler_client, "resolve_crawler_repo", lambda path: path)
    monkeypatch.setattr(crawler_client, "run_crawler", _run)
    assert main.run(make_config(mode=MODE_ADOPT, archive_root=photos)) == main.EXIT_HAS_FAILURES
    assert _rows(tmp_path) == [("2024/a.jpg", STATUS_STORED)]


def test_legacy_archive_is_migrated_not_adopted(make_config, tmp_path, fake_crawler):
    """v0.1.x の DB がある保存フォルダは移行で記録ができるので、adopt は要らない（断って verify を案内）。"""
    from test_archives import _make_legacy_db
    from database import legacy_db_path
    root = str(tmp_path / "legacy")
    write_file(os.path.join(root, "inbox", "a.txt"), b"abc")
    _make_legacy_db(legacy_db_path(root), root)
    assert main.run(make_config(mode=MODE_REPORT, archive_root=root)) == main.EXIT_OK
    with pytest.raises(PreflightError, match="verify"):
        _adopt(make_config, root, tmp_path, fake_crawler)


def test_adopt_cli_is_available(tmp_path, photos):
    from config import parse_arguments
    cfg = parse_arguments(["adopt", photos])
    assert cfg.mode == MODE_ADOPT and cfg.archive_root == os.path.abspath(photos)
    shutil.rmtree(str(tmp_path / "photos"))


def test_folder_with_only_empty_files_needs_no_adopt(make_config, tmp_path, fake_crawler):
    """adopt が登録するもの（1 バイト以上の実ファイル）が無いフォルダは、adopt なしで使える。

    「中身あり」の判定を adopt の登録条件とそろえる。ずれると、adopt しても何も登録されず断られ続ける。
    """
    root = str(tmp_path / "only_empty")
    write_file(os.path.join(root, "empty.txt"), b"")
    write_file(os.path.join(root, ".DS_Store"), b"junk")
    assert main.run(make_config(mode=MODE_REPORT, archive_root=root)) == main.EXIT_OK


def test_files_copied_after_an_empty_adopt_still_need_adopt(make_config, tmp_path, fake_crawler):
    """何も登録せずに終わった adopt の後で手でファイルを入れたら、また adopt を求める（add で重複を作らない）。"""
    root = str(tmp_path / "only_empty")
    write_file(os.path.join(root, "empty.txt"), b"")
    assert _adopt(make_config, root, tmp_path, fake_crawler) == main.EXIT_OK
    write_file(os.path.join(root, "manual", "x.jpg"), b"XXXX")
    src = str(tmp_path / "inbox")
    write_file(os.path.join(src, "x_copy.jpg"), b"XXXX")
    fake_crawler(src, str(tmp_path / "c.db"))
    with pytest.raises(PreflightError, match="adopt"):
        main.run(make_config(archive_root=root, source_path=src))
    assert _adopt(make_config, root, tmp_path, fake_crawler) == main.EXIT_OK
    assert ("manual/x.jpg", STATUS_STORED) in _rows(tmp_path)


def test_failed_count_is_not_doubled(make_config, tmp_path, monkeypatch, photos):
    """実行記録の failed は、失敗した件数そのもの（2 倍にしない）。"""
    from conftest import build_crawler_db
    import sqlite3
    from models import Ingest
    crawl_db = str(tmp_path / "c.db")

    def _run(target, config, extra_excludes=()):
        scan_id = build_crawler_db(crawl_db, target, excludes=tuple(extra_excludes))
        conn = sqlite3.connect(crawl_db)
        conn.execute("UPDATE fs_files SET filehash = NULL, hash_algo = NULL WHERE name = 'b.jpg'")
        conn.commit(); conn.close()
        return crawler_client.CrawlerScan(db_path=crawl_db, scan_id=scan_id, status="completed", root_dir=target)

    monkeypatch.setattr(crawler_client, "resolve_crawler_repo", lambda path: path)
    monkeypatch.setattr(crawler_client, "run_crawler", _run)
    main.run(make_config(mode=MODE_ADOPT, archive_root=photos))
    sess, engine = _db(tmp_path)
    try:
        assert sess.query(Ingest).filter_by(mode=MODE_ADOPT).one().failed == 1
    finally:
        sess.close(); engine.dispose()
