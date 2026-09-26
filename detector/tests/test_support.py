# test_support.py - 設定・ハッシュ・ヘルパー・DB 制約・レポート
import os

import config as config_module
import crawler_client
import main
import pytest
import report
from conftest import write_file
from database import get_session
from errors import PreflightError
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


def test_data_root_is_repo_dist_in_development():
    assert config_module.data_root() == os.path.join(config_module.repo_root(), "dist")


def test_data_root_is_app_home_when_packaged(tmp_path, monkeypatch):
    monkeypatch.setenv("AKASYX_PACKAGED", "1")
    monkeypatch.setenv("AKASYX_DETECTOR_HOME", str(tmp_path / "home"))
    assert config_module.data_root() == str(tmp_path / "home")
    assert config_module._default_db_dir() == str(tmp_path / "home" / "db")
    assert config_module._default_log_dir() == str(tmp_path / "home" / "log")


def test_app_home_is_separate_from_akasyx_search(monkeypatch):
    """akasyx_search の akasyx/ と分ける（search のデータ移動・削除手順に巻き込まれないように）。"""
    monkeypatch.setattr(config_module.sys, "platform", "darwin")
    assert config_module.app_home() == os.path.expanduser(
        "~/Library/Application Support/akasyx-duplicate-detector"
    )
    monkeypatch.setattr(config_module.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/me/AppData/Local")
    assert config_module.app_home() == os.path.join(
        "C:/Users/me/AppData/Local", "akasyx-duplicate-detector"
    )


def test_version_reads_bundled_copy_when_frozen(tmp_path, monkeypatch):
    (tmp_path / "version.txt").write_text("9.8.7\n")
    monkeypatch.setattr(config_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config_module.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert config_module.app_version() == "9.8.7"


def test_siblings_bin_comes_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AKASYX_SIBLINGS_BIN", "/App/Resources/bin")
    cfg = config_module.parse_arguments(["report", str(tmp_path)])
    assert cfg.siblings_bin == "/App/Resources/bin"


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
        archive_id=1,
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


def test_missing_archive_db_is_created_on_startup(tmp_path, caplog):
    """正本 DB が無ければ（親フォルダごと）作り、その旨をログに出す。"""
    path = tmp_path / "nowhere" / "yet" / "archive.db"
    assert not path.exists()
    with caplog.at_level("WARNING"):
        sess, engine = get_session(str(path))
    try:
        assert path.exists()
        assert "creating a new one" in caplog.text
        # テーブルまで揃っている（ar_archives が引ける）
        from models import Archive
        assert sess.query(Archive).count() == 0
    finally:
        sess.close()
        engine.dispose()

    # 2 回目は「接続」であって作成ではない
    caplog.clear()
    with caplog.at_level("WARNING"):
        sess, engine = get_session(str(path))
    sess.close(); engine.dispose()
    assert "creating a new one" not in caplog.text


def test_pragmas_are_applied(tmp_path):
    sess, engine = get_session(str(tmp_path / "x" / "archive.db"))
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
    assert "Archive folder status" in capsys.readouterr().out


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
    assert "Unresolved duplicates (still in the source): 1" in out


def test_report_ingest_detail(make_config, archive, source, tmp_path, fake_crawler, capsys):
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "seed.db"))
    main.run(make_config(archive_root=archive, source_path=source))
    capsys.readouterr()

    main.run(make_config(mode=MODE_REPORT, archive_root=archive, ingest_id=1))
    assert "Run #1" in capsys.readouterr().out


def test_report_unknown_ingest(make_config, archive, capsys):
    main.run(make_config(mode=MODE_REPORT, archive_root=archive, ingest_id=999))
    assert "not found" in capsys.readouterr().out


def test_report_ingest_of_other_archive_is_not_shown(
    make_config, archive, source, tmp_path, fake_crawler, capsys
):
    """正本 DB を共有する別の保存フォルダの実行 ID を指定しても表示しない。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "seed.db"))
    main.run(make_config(archive_root=archive, source_path=source))  # 実行 #1 は archive のもの
    other = str(tmp_path / "other_archive")
    os.makedirs(other)
    capsys.readouterr()

    main.run(make_config(mode=MODE_REPORT, archive_root=other, ingest_id=1))
    out = capsys.readouterr().out
    assert "Run #1 not found" in out
    assert source not in out


def test_recent_ingests_on_empty_db(session):
    assert "(no history)" in "\n".join(report._recent_ingests(session, 1))


# --- crawler の起動（開発時 uv run / 配布版は同梱の実行形式） --------------------


def test_crawler_command_uses_bundled_executable(make_config, tmp_path):
    cfg = make_config(siblings_bin=str(tmp_path / "bin"))
    base, cwd = crawler_client.crawler_command(cfg)
    exe = str(tmp_path / "bin" / "akasyx-crawler" / "akasyx-crawler")
    assert base == [exe]
    assert cwd == os.path.dirname(exe)


def test_crawler_command_uses_uv_in_development(make_config, tmp_path, monkeypatch):
    repo = tmp_path / "akasyx_crawler"
    write_file(str(repo / "crawler" / "main.py"), b"")
    monkeypatch.setattr(crawler_client.shutil, "which", lambda name: "/usr/bin/uv")
    cfg = make_config(crawler_repo=str(repo))
    base, cwd = crawler_client.crawler_command(cfg)
    assert base == ["uv", "run", "main.py"]
    assert cwd == str(repo / "crawler")


def test_check_crawler_rejects_missing_bundled_executable(make_config, tmp_path):
    cfg = make_config(siblings_bin=str(tmp_path / "bin"))
    with pytest.raises(PreflightError, match="Bundled crawler"):
        crawler_client.check_crawler(cfg)


def test_check_crawler_accepts_bundled_executable(make_config, tmp_path):
    exe = tmp_path / "bin" / "akasyx-crawler" / "akasyx-crawler"
    write_file(str(exe), b"#!/bin/sh\n")
    exe.chmod(0o755)
    cfg = make_config(siblings_bin=str(tmp_path / "bin"))
    crawler_client.check_crawler(cfg)  # 例外にならない
