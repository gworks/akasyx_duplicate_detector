# test_main.py - 事前チェック・終了コード・end-to-end（設計書 §11 / §12）
import os
from datetime import datetime

import main
import pytest
from conftest import archive_db_path, build_crawler_db, write_file
from crawler_client import CrawlerScan
from errors import PreflightError
from models import STATUS_STORED, ArchiveFile, Ingest, IngestItem


_OPEN_DB_PATH = [None]


def _run(config):
    _OPEN_DB_PATH[0] = config.archive_db
    return main.run(config)


# --- 事前チェック（§12）------------------------------------------------------


def test_rejects_archive_inside_source(make_config, tmp_path):
    source = tmp_path / "inbox"
    (source / "archive").mkdir(parents=True)
    config = make_config(
        archive_root=str(source / "archive"), source_path=str(source)
    )
    with pytest.raises(PreflightError, match="nested"):
        main.preflight(config)


def test_rejects_source_inside_archive(make_config, archive):
    inner = os.path.join(archive, "inbox")
    os.makedirs(inner)
    config = make_config(archive_root=archive, source_path=inner)
    with pytest.raises(PreflightError, match="nested"):
        main.preflight(config)


def test_rejects_same_path(make_config, archive):
    config = make_config(archive_root=archive, source_path=archive)
    with pytest.raises(PreflightError, match="nested"):
        main.preflight(config)


def test_rejects_missing_archive(make_config, tmp_path):
    config = make_config(archive_root=str(tmp_path / "nope"))
    with pytest.raises(PreflightError, match="Archive folder not found"):
        main.preflight(config)


def test_rejects_missing_source(make_config, archive, tmp_path):
    config = make_config(archive_root=archive, source_path=str(tmp_path / "nope"))
    with pytest.raises(PreflightError, match="Source not found"):
        main.preflight(config)


def test_rejects_missing_crawler_repo(make_config, archive, source, tmp_path):
    """crawler が無ければ add / verify は実行前に断る（設計書 §12）。"""
    write_file(os.path.join(source, "a.txt"), b"hello")
    config = make_config(
        archive_root=archive,
        source_path=source,
        crawler_repo=str(tmp_path / "absent"),
    )
    with pytest.raises(PreflightError, match="akasyx_crawler not found"):
        main.preflight(config)


def test_single_file_source_does_not_need_crawler_repo(make_config, archive, tmp_path):
    """単一ファイルは crawler を使わないので、crawler 不在でも通る。"""
    path = write_file(str(tmp_path / "solo" / "x.txt"), b"solo")
    config = make_config(
        archive_root=archive, source_path=path, crawler_repo=str(tmp_path / "absent")
    )
    main.preflight(config)  # 例外にならない


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
            "--archive-db", archive_db_path(tmp_path),
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

    # 新規2件が保存フォルダ（今月の年月フォルダ / 投入元名 / 相対パス）へ、重複と0バイトは投入元に残る
    month = f"{datetime.now():%Y-%m}"
    name = os.path.basename(source)
    assert os.path.exists(os.path.join(archive, month, name, "a.txt"))
    assert os.path.exists(os.path.join(archive, month, name, "sub", "b.txt"))
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
    # 単一ファイルは投入元フォルダ名を付けず、年月フォルダ直下
    assert os.path.exists(os.path.join(archive, f"{datetime.now():%Y-%m}", "x.txt"))


def test_add_keeps_source_structure_under_month(
    make_config, archive, source, tmp_path, fake_crawler
):
    """保存先は <YYYY-MM>/<投入元名>/<相対パス>。投入元の階層は年月フォルダの中に再現される。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")
    write_file(os.path.join(source, "deep", "nested", "b.txt"), b"BBB")
    fake_crawler(source, str(tmp_path / "crawler.db"))
    config = make_config(archive_root=archive, source_path=source)
    assert _run(config) == main.EXIT_OK

    month = f"{datetime.now():%Y-%m}"
    name = os.path.basename(source)
    assert os.path.exists(os.path.join(archive, month, name, "a.txt"))
    assert os.path.exists(os.path.join(archive, month, name, "deep", "nested", "b.txt"))


def test_add_ignores_os_junk_and_prunes_it(
    make_config, archive, source, tmp_path, fake_crawler
):
    """.DS_Store は取り込まず、--prune-empty-dirs で .DS_Store だけ残ったフォルダも消える。"""
    write_file(os.path.join(source, "sub", "a.txt"), b"AAA")
    write_file(os.path.join(source, "sub", ".DS_Store"), b"junk")
    write_file(os.path.join(source, ".DS_Store"), b"junk")
    fake_crawler(source, str(tmp_path / "crawler.db"))
    config = make_config(archive_root=archive, source_path=source, prune_empty_dirs=True)
    assert _run(config) == main.EXIT_OK

    sess, engine = _open(archive)
    try:
        assert {r.name for r in sess.query(ArchiveFile).all()} == {"a.txt"}
    finally:
        sess.close()
        engine.dispose()
    assert not os.path.exists(os.path.join(source, "sub"))   # .DS_Store だけになった sub は消える
    assert os.path.isdir(source)                              # 投入元そのものは残す


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
        with pytest.raises(PreflightError, match="in use by another process"):
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
            "--archive-db", archive_db_path(tmp_path),
        ]
    )
    assert code == main.EXIT_OK


def _open(_archive_root_unused):
    """正本 DB を開く（引数は旧 API 互換のため残す。DB は保存フォルダの外にある）。"""
    from database import get_session

    return get_session(_OPEN_DB_PATH[0])


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
