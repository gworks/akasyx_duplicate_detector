# test_archives.py - 正本 DB 1 つで複数の保存フォルダを扱う（設計書 §4 / §8。v0.2.0）
import os
import sqlite3

import archives
import main
from conftest import ARCHIVE_ID, archive_db_path, write_file
from database import archive_id_path, legacy_db_path
from models import STATUS_STORED, Archive, ArchiveFile, Ingest, IngestItem


def _seed(make_config, root, source, tmp_path, fake_crawler, name, content):
    write_file(os.path.join(source, name), content)
    fake_crawler(source, str(tmp_path / f"crawl_{name}.db"))
    assert main.run(make_config(archive_root=root, source_path=source)) == main.EXIT_OK


def test_resolve_registers_and_writes_id_file(session, archive):
    row = archives.resolve_archive(session, archive)
    assert row.root_abs == archive
    with open(archive_id_path(archive), encoding="utf-8") as f:
        assert f.read().strip() == row.uid


def test_resolve_follows_moved_folder_by_uid(session, tmp_path):
    """保存フォルダを移動しても .akasyx/archive.id の uid で同じ行に繋がり、パスが更新される。"""
    old = tmp_path / "old_place"
    old.mkdir()
    row = archives.resolve_archive(session, str(old))
    new = tmp_path / "new_place"
    os.rename(old, new)
    again = archives.resolve_archive(session, str(new))
    assert again.id == row.id
    assert again.root_abs == str(new)


def test_resolve_rewrites_missing_id_file_by_path(session, archive):
    row = archives.resolve_archive(session, archive)
    os.unlink(archive_id_path(archive))
    again = archives.resolve_archive(session, archive)
    assert again.id == row.id
    assert os.path.exists(archive_id_path(archive))


def test_two_archives_do_not_share_duplicates(
    make_config, tmp_path, fake_crawler
):
    """同じ内容でも保存フォルダが違えば別々に保存される（重複判定は保存フォルダ単位）。"""
    a = tmp_path / "archive_a"; a.mkdir()
    b = tmp_path / "archive_b"; b.mkdir()
    src_a = tmp_path / "in_a"; src_a.mkdir()
    src_b = tmp_path / "in_b"; src_b.mkdir()
    _seed(make_config, str(a), str(src_a), tmp_path, fake_crawler, "x.txt", b"SAME")
    _seed(make_config, str(b), str(src_b), tmp_path, fake_crawler, "y.txt", b"SAME")

    from database import get_session
    sess, engine = get_session(archive_db_path(tmp_path))
    try:
        rows = sess.query(ArchiveFile).filter_by(status=STATUS_STORED).all()
        assert len(rows) == 2
        assert len({r.archive_id for r in rows}) == 2
        assert sess.query(Archive).count() >= 2
    finally:
        sess.close(); engine.dispose()


def test_same_archive_still_dedupes(make_config, archive, tmp_path, fake_crawler):
    src1 = tmp_path / "in1"; src1.mkdir()
    src2 = tmp_path / "in2"; src2.mkdir()
    _seed(make_config, archive, str(src1), tmp_path, fake_crawler, "x.txt", b"SAME")
    _seed(make_config, archive, str(src2), tmp_path, fake_crawler, "y.txt", b"SAME")
    assert os.path.exists(os.path.join(str(src2), "y.txt"))  # 重複として残る


def test_archives_subcommand_lists_registered(make_config, archive, tmp_path, capsys):
    session_cfg = make_config()
    from database import get_session
    sess, engine = get_session(session_cfg.archive_db)
    archives.resolve_archive(sess, archive)
    sess.close(); engine.dispose()

    from models import MODE_ARCHIVES
    cfg = make_config(mode=MODE_ARCHIVES, archive_root=None)
    assert main.run(cfg) == main.EXIT_OK
    out = capsys.readouterr().out
    assert archive in out
    assert "登録済みの保存フォルダ: 1 件" in out


def _make_legacy_db(path: str, root: str) -> None:
    """v0.1.x の archive.db（archive_id 列の無い 3 テーブル）を最小構成で作る。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE ar_ingests (id INTEGER PRIMARY KEY, mode TEXT, source_root TEXT,
            archive_root TEXT, crawler_db_path TEXT, crawler_scan_id INTEGER, dry_run INTEGER,
            started_at TIMESTAMP, finished_at TIMESTAMP, status TEXT, total INTEGER, moved INTEGER,
            duplicated INTEGER, skipped INTEGER, failed INTEGER, stats_json TEXT, config_json TEXT,
            app_version TEXT);
        CREATE TABLE ar_archive_files (id INTEGER PRIMARY KEY, filehash TEXT, hash_algo TEXT,
            size INTEGER, name TEXT, stored_path_rel TEXT, origin_path_abs TEXT, origin_root TEXT,
            origin_modified_at TIMESTAMP, mime_type TEXT, ingest_id INTEGER, status TEXT,
            disposition TEXT, verified_at TIMESTAMP, created_at TIMESTAMP, updated_at TIMESTAMP);
        CREATE TABLE ar_ingest_items (id INTEGER PRIMARY KEY, ingest_id INTEGER, source_path_abs TEXT,
            source_path_rel TEXT, name TEXT, size INTEGER, filehash TEXT, hash_algo TEXT, result TEXT,
            archive_file_id INTEGER, planned_path_rel TEXT, resolution TEXT, resolved_at TIMESTAMP,
            message TEXT, created_at TIMESTAMP);
        """
    )
    conn.execute(
        "INSERT INTO ar_ingests (id, mode, archive_root, dry_run, status, started_at, moved)"
        " VALUES (7, 'add', ?, 0, 'completed', '2026-08-30 00:00:00', 1)", (root,)
    )
    conn.execute(
        "INSERT INTO ar_archive_files (id, filehash, hash_algo, size, name, stored_path_rel,"
        " ingest_id, status, created_at, updated_at) VALUES (11, ?, 'sha256', 3, 'a.txt',"
        " 'inbox/a.txt', 7, 'stored', '2026-08-30 00:00:00', '2026-08-30 00:00:00')", ("a" * 64,)
    )
    conn.execute(
        "INSERT INTO ar_ingest_items (id, ingest_id, source_path_abs, name, size, filehash,"
        " hash_algo, result, archive_file_id, created_at) VALUES (21, 7, '/in/copy.txt', 'copy.txt',"
        " 3, ?, 'sha256', 'duplicate', 11, '2026-08-30 00:00:00')", ("a" * 64,)
    )
    conn.commit(); conn.close()


def test_legacy_db_is_imported_and_renamed(session, archive):
    """v0.1.x の保存フォルダ内 archive.db を正本 DB に取り込み、旧 DB は改名して残す。"""
    legacy = legacy_db_path(archive)
    _make_legacy_db(legacy, archive)

    row = archives.resolve_archive(session, archive)

    files = session.query(ArchiveFile).filter_by(archive_id=row.id).all()
    assert [f.stored_path_rel for f in files] == ["inbox/a.txt"]
    ing = session.query(Ingest).filter_by(archive_id=row.id).one()
    assert ing.moved == 1
    item = session.query(IngestItem).one()
    assert item.ingest_id == ing.id                 # id は付け直されて参照が追い直される
    assert item.archive_file_id == files[0].id
    assert not os.path.exists(legacy)
    assert any(n.startswith("archive.db.migrated-") for n in os.listdir(os.path.dirname(legacy)))


def test_second_run_does_not_reimport(session, archive):
    legacy = legacy_db_path(archive)
    _make_legacy_db(legacy, archive)
    archives.resolve_archive(session, archive)
    archives.resolve_archive(session, archive)  # 2 回目: 旧 DB は既に改名済みで何もしない
    assert session.query(ArchiveFile).count() == 1
