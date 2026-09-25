# conftest.py - テスト共通のフィクスチャ（設計書 §14）
#
# crawler は別リポジトリで CI には存在しないため、fs_files 相当のテーブルを持つ
# 一時 SQLite を作って read_files を通す。判定ロジック側は crawler に触れない。
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module  # noqa: E402
import crawler_client  # noqa: E402
from config import DetectorConfig  # noqa: E402
from database import get_session  # noqa: E402
from models import MODE_ADD, Archive, Ingest  # noqa: E402
from utl import hashing  # noqa: E402

CREATE_SCANS = """
CREATE TABLE fs_scans (
    id INTEGER PRIMARY KEY, source_type TEXT, root_dir TEXT,
    started_at TIMESTAMP, finished_at TIMESTAMP, status TEXT,
    total_files INTEGER, total_dirs INTEGER, ignored_files INTEGER,
    stats_json TEXT, config_json TEXT
)
"""
CREATE_FILES = """
CREATE TABLE fs_files (
    id INTEGER PRIMARY KEY, scan_id INTEGER, last_seen_scan_id INTEGER,
    source_type TEXT, root_dir TEXT, name TEXT, path_abs TEXT UNIQUE,
    path_rel TEXT, extension TEXT, size INTEGER, mime_type TEXT,
    filehash TEXT, hash_algo TEXT, created_at TIMESTAMP, modified_at TIMESTAMP,
    accessed_at TIMESTAMP, mtime_ns INTEGER, change_key TEXT, statinfo TEXT,
    meta_schema TEXT, metainfo TEXT, content_created_at TIMESTAMP,
    content_author TEXT, duration_seconds REAL, width INTEGER, height INTEGER,
    duplicate_of_id INTEGER, status TEXT, timestamp TIMESTAMP
)
"""


def write_file(path: str, content: bytes) -> str:
    """ファイルを作って親ディレクトリごと用意します。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


def build_crawler_db(
    db_path: str,
    root_dir: str,
    scan_status: str = "completed",
    excludes: tuple[str, ...] = (),
) -> int:
    """root_dir 配下の実ファイルから crawler の DB を模して作ります。戻り値は scan_id。

    excludes は crawler の --exclude 相当。`.akasyx/` のようなディレクトリ名と、
    `.DS_Store` のようなファイル名の両方を名前一致で除外する（実 crawler は gitignore 記法）。
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if os.path.exists(db_path):
        os.unlink(db_path)
    skip = {e.strip("/") for e in excludes}
    conn = sqlite3.connect(db_path)
    conn.execute(CREATE_SCANS)
    conn.execute(CREATE_FILES)
    cur = conn.execute(
        "INSERT INTO fs_scans (root_dir, status) VALUES (?, ?)",
        (root_dir, scan_status),
    )
    scan_id = cur.lastrowid

    for dirpath, dirs, names in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in sorted(names):
            if name in skip:
                continue
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, root_dir).replace(os.sep, "/")
            size = os.path.getsize(abs_path)
            filehash = hashing.file_hash(abs_path)
            conn.execute(
                "INSERT INTO fs_files (scan_id, last_seen_scan_id, name, path_abs,"
                " path_rel, size, filehash, hash_algo, status, modified_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'sha256', 'active', '2026-08-30T00:00:00')",
                (scan_id, scan_id, name, abs_path, rel, size, filehash),
            )
    conn.commit()
    conn.close()
    return scan_id


@pytest.fixture
def archive(tmp_path):
    """空の保存用フォルダ。"""
    path = tmp_path / "archive"
    path.mkdir()
    return str(path)


@pytest.fixture
def source(tmp_path):
    """空の投入元フォルダ。"""
    path = tmp_path / "inbox"
    path.mkdir()
    return str(path)


def archive_db_path(tmp_path) -> str:
    """テスト用の正本 DB（保存フォルダの外。実運用の dist/archive.db に相当）。"""
    return str(tmp_path / "dist" / "archive.db")


ARCHIVE_ID = 1  # session フィクスチャが登録する保存フォルダ #1


@pytest.fixture(autouse=True)
def _isolate_archive_db(tmp_path, monkeypatch):
    """argparse 経由（main.main）のテストが実運用の dist/archive.db を汚さないよう、既定を tmp に向ける。

    2026-09-14 に pytest の tmp パスが本番 DB の ar_archives に 4 件登録される事故が起きたため。
    """
    monkeypatch.setattr(config_module, "_default_archive_db", lambda: archive_db_path(tmp_path))
    # 実行環境の変数で既定の置き場（開発時 dist/ か配布版の App Support か）が変わらないようにする
    monkeypatch.delenv("AKASYX_PACKAGED", raising=False)
    monkeypatch.delenv("AKASYX_DETECTOR_HOME", raising=False)
    monkeypatch.delenv("AKASYX_SIBLINGS_BIN", raising=False)


@pytest.fixture
def make_config(tmp_path, archive):
    def _make(**kwargs):
        params = {
            "mode": MODE_ADD,
            "archive_root": archive,
            "archive_db": archive_db_path(tmp_path),
            # main.run() を通さず mover 等を直接呼ぶテスト用。run() は resolve_archive で上書きする
            "archive_id": ARCHIVE_ID,
            "db_dir": str(tmp_path / "dist" / "db"),
            "log_dir": str(tmp_path / "dist" / "log"),
            # 実機に ../akasyx_crawler があるかどうかでテスト結果が変わらないよう、
            # 既定は必ず存在しないパスにする（使う場合は fake_crawler が差し替える）
            "crawler_repo": str(tmp_path / "no_crawler_here"),
        }
        params.update(kwargs)
        cfg = DetectorConfig(**params)
        os.makedirs(cfg.db_dir, exist_ok=True)
        os.makedirs(cfg.log_dir, exist_ok=True)
        return cfg

    return _make


@pytest.fixture
def session(tmp_path, archive):
    """正本 DB に接続し、`archive` フィクスチャの保存フォルダを #1 として登録済みにする。"""
    sess, engine = get_session(archive_db_path(tmp_path))
    if sess.get(Archive, ARCHIVE_ID) is None:
        sess.add(Archive(id=ARCHIVE_ID, uid="test-archive-1", root_abs=archive))
        sess.commit()
    yield sess
    sess.close()
    engine.dispose()


@pytest.fixture
def ingest_row(session, archive):
    row = Ingest(archive_id=ARCHIVE_ID, mode=MODE_ADD, archive_root=archive, status="running")
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def fake_crawler(monkeypatch):
    """crawler の起動を、テスト内で作った DB を返すだけの処理に差し替えます。"""

    def _install(root_dir: str, db_path: str, scan_status: str = "completed"):
        # 呼ばれた時点の実ファイルから作り直す（実 crawler と同じく、走査は実行時）
        def _run(target, config, extra_excludes=()):
            scan_id = build_crawler_db(
                db_path, target, scan_status, excludes=tuple(extra_excludes)
            )
            return crawler_client.CrawlerScan(
                db_path=db_path,
                scan_id=scan_id,
                status=scan_status,
                root_dir=target,
            )

        # preflight は resolve_crawler_repo を別途呼ぶ。run_crawler だけ差し替えると
        # 実機に crawler がある環境でしか通らないテストになる（CI で発覚）
        monkeypatch.setattr(crawler_client, "resolve_crawler_repo", lambda path: path)
        # ingest / verify は同じモジュールオブジェクトを参照するのでこれで足りる
        monkeypatch.setattr(crawler_client, "run_crawler", _run)
        return _run

    return _install
