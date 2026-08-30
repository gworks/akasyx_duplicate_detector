# test_support.py - 設定・ハッシュ・ヘルパー・DB 制約・レポート
import os

import config as config_module
import main
import pytest
import report
from conftest import write_file
from database import get_session
from models import (
    MODE_ADD,
    MODE_REPORT,
    MODE_VERIFY,
    STATUS_MISSING,
    STATUS_STORED,
    ArchiveFile,
)
from sqlalchemy.exc import IntegrityError
from utl import hashing
from utl.helpers import format_size, is_nested, to_posix


# --- config ------------------------------------------------------------------


def test_parse_add_defaults(tmp_path):
    cfg = config_module.parse_arguments(["add", str(tmp_path), str(tmp_path / "in")])
    assert cfg.mode == MODE_ADD
    assert cfg.min_size == 1        # 既定で 0 バイトを除外
    assert cfg.dry_run is False
    assert cfg.dest_subdir is None  # 未指定は None（'' とは別物）


def test_parse_dest_subdir_empty(tmp_path):
    cfg = config_module.parse_arguments(
        ["add", str(tmp_path), str(tmp_path), "--dest-subdir", ""]
    )
    assert cfg.dest_subdir == ""


def test_parse_verify_flags(tmp_path):
    cfg = config_module.parse_arguments(
        ["verify", str(tmp_path), "--flag-quarantine", "missing", "unregistered"]
    )
    assert cfg.mode == MODE_VERIFY
    assert cfg.flag_quarantine == ["missing", "unregistered"]


def test_parse_rejects_unknown_quarantine_kind(tmp_path):
    with pytest.raises(SystemExit):
        config_module.parse_arguments(
            ["verify", str(tmp_path), "--flag-quarantine", "nonsense"]
        )


def test_version_matches_version_txt():
    with open(os.path.join(config_module.repo_root(), "version.txt")) as f:
        assert config_module.app_version() == f.read().strip()


def test_config_snapshot_is_serializable(tmp_path):
    cfg = config_module.parse_arguments(["report", str(tmp_path)])
    assert config_module.config_snapshot(cfg)["mode"] == MODE_REPORT


# --- hashing / helpers --------------------------------------------------------


def test_file_hash_matches_hashlib(tmp_path):
    import hashlib

    path = write_file(str(tmp_path / "a.bin"), b"x" * 3000)
    assert hashing.file_hash(path) == hashlib.sha256(b"x" * 3000).hexdigest()


def test_file_hash_of_empty_file(tmp_path):
    path = write_file(str(tmp_path / "e.bin"), b"")
    assert hashing.file_hash(path).startswith("e3b0c442")


def test_to_posix():
    assert to_posix("a\\b\\c") == "a/b/c"
    assert to_posix("/a/b/") == "a/b"


def test_is_nested():
    assert is_nested("/a", "/a/b")
    assert is_nested("/a", "/a")
    assert not is_nested("/a/b", "/a")


def test_format_size():
    assert format_size(512) == "512 B"
    assert format_size(2048) == "2.0 KiB"


# --- DB 制約（§8）-------------------------------------------------------------


def _row(session, filehash, rel, status=STATUS_STORED):
    row = ArchiveFile(
        filehash=filehash, hash_algo="sha256", size=1,
        name=os.path.basename(rel), stored_path_rel=rel, status=status,
    )
    session.add(row)
    session.commit()
    return row


def test_unique_index_blocks_double_registration(session):
    _row(session, "a" * 64, "a.txt")
    with pytest.raises(IntegrityError):
        _row(session, "a" * 64, "b.txt")
    session.rollback()


def test_unique_index_ignores_missing_rows(session):
    """missing は内容を保持していないので、同内容の再登録を妨げない。"""
    _row(session, "a" * 64, "a.txt", status=STATUS_MISSING)
    _row(session, "a" * 64, "b.txt")  # 例外にならない
    assert session.query(ArchiveFile).count() == 2


def test_unique_path_index(session):
    _row(session, "a" * 64, "same.txt")
    with pytest.raises(IntegrityError):
        _row(session, "b" * 64, "same.txt")
    session.rollback()


def test_pragmas_are_applied(archive):
    sess, engine = get_session(archive)
    try:
        with engine.connect() as conn:
            from sqlalchemy import text

            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert conn.execute(text("PRAGMA synchronous")).scalar() == 2  # FULL
    finally:
        sess.close()
        engine.dispose()


# --- report -------------------------------------------------------------------


def test_report_runs_on_empty_archive(make_config, archive, capsys):
    assert main.run(make_config(mode=MODE_REPORT, archive_root=archive)) == main.EXIT_OK
    assert "保存フォルダの状態" in capsys.readouterr().out


def test_report_shows_stored_and_unresolved(
    make_config, archive, source, tmp_path, fake_crawler, capsys
):
    write_file(os.path.join(source, "a.txt"), b"AAA")
    write_file(os.path.join(source, "copy.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "seed.db"))
    main.run(make_config(archive_root=archive, source_path=source))
    capsys.readouterr()

    main.run(make_config(mode=MODE_REPORT, archive_root=archive))
    out = capsys.readouterr().out
    assert "stored" in out
    assert "未処置の重複（投入元に残っている）: 1 件" in out


def test_report_ingest_detail(make_config, archive, source, tmp_path, fake_crawler, capsys):
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "seed.db"))
    main.run(make_config(archive_root=archive, source_path=source))
    capsys.readouterr()

    main.run(make_config(mode=MODE_REPORT, archive_root=archive, ingest_id=1))
    assert "実行 #1" in capsys.readouterr().out


def test_report_unknown_ingest(make_config, archive, capsys):
    main.run(make_config(mode=MODE_REPORT, archive_root=archive, ingest_id=999))
    assert "見つかりません" in capsys.readouterr().out


def test_recent_ingests_on_empty_db(session):
    assert "（履歴なし）" in "\n".join(report._recent_ingests(session))
