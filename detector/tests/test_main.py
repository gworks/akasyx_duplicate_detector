# test_main.py - 事前チェック・終了コード・end-to-end（設計書 §11 / §12）
import os

import main
import pytest
from conftest import build_crawler_db, write_file
from crawler_client import CrawlerScan
from errors import PreflightError
from models import STATUS_STORED, ArchiveFile, Ingest, IngestItem


def _run(config):
    return main.run(config)


# --- 事前チェック（§12）------------------------------------------------------


def test_rejects_archive_inside_source(make_config, tmp_path):
    source = tmp_path / "inbox"
    (source / "archive").mkdir(parents=True)
    config = make_config(
        archive_root=str(source / "archive"), source_path=str(source)
    )
    with pytest.raises(PreflightError, match="入れ子"):
        main.preflight(config)


def test_rejects_source_inside_archive(make_config, archive):
    inner = os.path.join(archive, "inbox")
    os.makedirs(inner)
    config = make_config(archive_root=archive, source_path=inner)
    with pytest.raises(PreflightError, match="入れ子"):
        main.preflight(config)


def test_rejects_same_path(make_config, archive):
    config = make_config(archive_root=archive, source_path=archive)
    with pytest.raises(PreflightError, match="入れ子"):
        main.preflight(config)


def test_rejects_missing_archive(make_config, tmp_path):
    config = make_config(archive_root=str(tmp_path / "nope"))
    with pytest.raises(PreflightError, match="保存用フォルダがありません"):
        main.preflight(config)


def test_rejects_missing_source(make_config, archive, tmp_path):
    config = make_config(archive_root=archive, source_path=str(tmp_path / "nope"))
    with pytest.raises(PreflightError, match="投入元がありません"):
        main.preflight(config)


def test_rejects_incomplete_crawler_scan(
    make_config, archive, source, tmp_path, fake_crawler
):
    """不完全な走査では取り込まない（設計書 §3）。"""
    write_file(os.path.join(source, "a.txt"), b"hello")
    fake_crawler(source, str(tmp_path / "crawler.db"), scan_status="interrupted")
    code = main.main(
        [
            "add", archive, source,
            "--db-dir", str(tmp_path / "db"),
            "--log-dir", str(tmp_path / "log"),
        ]
    )
    assert code == main.EXIT_REJECTED
    assert os.path.exists(os.path.join(source, "a.txt"))  # 1件も動いていない


# --- end-to-end（add）--------------------------------------------------------


def test_add_moves_new_and_keeps_duplicates(
    make_config, archive, source, tmp_path, fake_crawler
):
    write_file(os.path.join(source, "a.txt"), b"AAA")
    write_file(os.path.join(source, "sub", "b.txt"), b"BBB")
    write_file(os.path.join(source, "sub", "copy_of_a.txt"), b"AAA")  # 内容重複
    write_file(os.path.join(source, "empty.txt"), b"")               # 0バイト

    fake_crawler(source, str(tmp_path / "crawler.db"))
    config = make_config(archive_root=archive, source_path=source)
    assert _run(config) == main.EXIT_OK

    # 新規2件が保存フォルダへ、重複と0バイトは投入元に残る
    name = os.path.basename(source)
    assert os.path.exists(os.path.join(archive, name, "a.txt"))
    assert os.path.exists(os.path.join(archive, name, "sub", "b.txt"))
    assert os.path.exists(os.path.join(source, "sub", "copy_of_a.txt"))
    assert os.path.exists(os.path.join(source, "empty.txt"))

    sess, engine = _open(archive)
    try:
        stored = sess.query(ArchiveFile).filter_by(status=STATUS_STORED).all()
        assert {r.name for r in stored} == {"a.txt", "b.txt"}
        results = {i.name: i.result for i in sess.query(IngestItem).all()}
        assert results["copy_of_a.txt"] == "duplicate"
        assert results["empty.txt"] == "skipped_empty"
    finally:
        sess.close()
        engine.dispose()


def test_add_is_idempotent(make_config, archive, source, tmp_path, fake_crawler):
    """同じ内容を再投入しても2重に保存されない。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "crawler1.db"))
    _run(make_config(archive_root=archive, source_path=source))

    write_file(os.path.join(source, "a.txt"), b"AAA")  # 同じ内容をもう一度置く
    fake_crawler(source, str(tmp_path / "crawler2.db"))
    _run(make_config(archive_root=archive, source_path=source))

    sess, engine = _open(archive)
    try:
        assert sess.query(ArchiveFile).filter_by(status=STATUS_STORED).count() == 1
    finally:
        sess.close()
        engine.dispose()
    assert os.path.exists(os.path.join(source, "a.txt"))  # 2回目は残る


def test_dry_run_changes_nothing(make_config, archive, source, tmp_path, fake_crawler):
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "crawler.db"))
    config = make_config(archive_root=archive, source_path=source, dry_run=True)
    assert _run(config) == main.EXIT_OK

    assert os.path.exists(os.path.join(source, "a.txt"))
    sess, engine = _open(archive)
    try:
        assert sess.query(ArchiveFile).count() == 0
        assert sess.query(IngestItem).count() == 0
        assert sess.query(Ingest).one().dry_run == 1
    finally:
        sess.close()
        engine.dispose()


def test_single_file_source_needs_no_crawler(make_config, archive, tmp_path):
    path = write_file(str(tmp_path / "solo" / "x.txt"), b"solo")
    config = make_config(archive_root=archive, source_path=path)
    assert _run(config) == main.EXIT_OK
    assert os.path.exists(os.path.join(archive, "x.txt"))


def test_failed_item_returns_exit_code_2(
    make_config, archive, source, tmp_path, fake_crawler, monkeypatch
):
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "crawler.db"))

    import mover

    monkeypatch.setattr(
        mover, "safe_move", lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    )
    config = make_config(archive_root=archive, source_path=source)
    assert _run(config) == main.EXIT_HAS_FAILURES


def test_lock_prevents_concurrent_run(make_config, archive, source):
    from database import archive_lock

    config = make_config(archive_root=archive, source_path=source)
    with archive_lock(archive):
        with pytest.raises(PreflightError, match="使用中"):
            with archive_lock(archive):
                pass  # pragma: no cover


def test_cli_smoke(archive, source, tmp_path, monkeypatch, fake_crawler):
    """argparse からの経路（main.main）が通ること。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "crawler.db"))
    code = main.main(
        [
            "add", archive, source,
            "--db-dir", str(tmp_path / "db"),
            "--log-dir", str(tmp_path / "log"),
        ]
    )
    assert code == main.EXIT_OK


def _open(archive):
    from database import get_session

    return get_session(archive)


def test_build_crawler_db_roundtrip(source, tmp_path):
    """テスト用の crawler DB が read_files で読めること（フィクスチャ自身の検証）。"""
    import crawler_client

    write_file(os.path.join(source, "a.txt"), b"AAA")
    db = str(tmp_path / "c.db")
    scan_id = build_crawler_db(db, source)
    files = list(crawler_client.read_files(db, scan_id))
    assert [f.path_rel for f in files] == ["a.txt"]
    assert files[0].hash_algo == "sha256"
    assert isinstance(
        crawler_client.CrawlerScan(db, scan_id, "completed", source), CrawlerScan
    )
