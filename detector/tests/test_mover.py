# test_mover.py - 衝突回避・3フェーズ移動・クラッシュ復旧（設計書 §7.2 〜 §7.4）
import os

import mover
import pytest
from conftest import write_file
from crawler_client import ScannedFile
from database import tmp_dir
from errors import DetectorError
from models import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_STORED,
    ArchiveFile,
)
from utl import hashing


def _scanned(path, rel=None):
    return ScannedFile(
        name=os.path.basename(path),
        path_abs=path,
        path_rel=rel or os.path.basename(path),
        size=os.path.getsize(path),
        filehash=hashing.file_hash(path),
        hash_algo="sha256",
    )


# --- 衝突回避（§7.2）---------------------------------------------------------


def test_resolve_dest_uses_source_structure(session, archive):
    rel = mover.resolve_dest(session, archive, "写真2024", "旅行/IMG_0001.jpg")
    assert rel == "写真2024/旅行/IMG_0001.jpg"


def test_resolve_dest_avoids_existing_file(session, archive):
    write_file(os.path.join(archive, "sub", "a.txt"), b"x")
    assert mover.resolve_dest(session, archive, None, "sub/a.txt") == "sub/a (2).txt"


def test_resolve_dest_avoids_db_reservation(session, archive):
    session.add(
        ArchiveFile(
            filehash="a" * 64, hash_algo="sha256", size=1, name="a.txt",
            stored_path_rel="a.txt", status=STATUS_PENDING,
        )
    )
    session.commit()
    assert mover.resolve_dest(session, archive, None, "a.txt") == "a (2).txt"


def test_resolve_dest_is_case_insensitive(session, archive):
    """大文字小文字を区別しない FS でも衝突しないよう casefold して判定する。"""
    session.add(
        ArchiveFile(
            filehash="a" * 64, hash_algo="sha256", size=1, name="A.TXT",
            stored_path_rel="A.TXT", status=STATUS_STORED,
        )
    )
    session.commit()
    assert mover.resolve_dest(session, archive, None, "a.txt") == "a (2).txt"


def test_resolve_dest_honours_extra_taken(session, archive):
    taken = {"a.txt", "a (2).txt"}
    rel = mover.resolve_dest(session, archive, None, "a.txt", extra_taken=taken)
    assert rel == "a (3).txt"


def test_resolve_dest_gives_up_eventually(session, archive, monkeypatch):
    monkeypatch.setattr(mover, "MAX_COLLISION_SUFFIX", 2)
    write_file(os.path.join(archive, "a.txt"), b"x")
    write_file(os.path.join(archive, "a (2).txt"), b"y")
    with pytest.raises(DetectorError):
        mover.resolve_dest(session, archive, None, "a.txt")


# --- safe_move（§7.3 Phase 2）------------------------------------------------


def test_safe_move_same_filesystem(tmp_path, archive):
    src = write_file(str(tmp_path / "src.txt"), b"hello")
    dst = os.path.join(archive, "dst.txt")
    mover.safe_move(src, dst, hashing.file_hash(src), tmp_dir(archive))
    assert not os.path.exists(src)
    assert open(dst, "rb").read() == b"hello"


def test_safe_move_cross_filesystem_verifies_before_unlink(
    tmp_path, archive, monkeypatch
):
    """別FS 経路: copy → 検証 → 元の削除、の順であること。"""
    src = write_file(str(tmp_path / "src.txt"), b"hello")
    dst = os.path.join(archive, "dst.txt")
    monkeypatch.setattr(mover, "same_filesystem", lambda *_: False)

    mover.safe_move(src, dst, hashing.file_hash(src), tmp_dir(archive))
    assert not os.path.exists(src)
    assert open(dst, "rb").read() == b"hello"
    assert os.listdir(tmp_dir(archive)) == []


def test_safe_move_cross_filesystem_keeps_source_when_verify_fails(
    tmp_path, archive, monkeypatch
):
    """検証が通らなければ元ファイルは絶対に消さない（設計書 §1 の原則）。"""
    src = write_file(str(tmp_path / "src.txt"), b"hello")
    dst = os.path.join(archive, "dst.txt")
    monkeypatch.setattr(mover, "same_filesystem", lambda *_: False)

    with pytest.raises(DetectorError):
        mover.safe_move(src, dst, "0" * 64, tmp_dir(archive))

    assert os.path.exists(src)          # 元は残っている
    assert not os.path.exists(dst)      # 壊れたコピーは置かれていない
    assert os.listdir(tmp_dir(archive)) == []  # .part も残らない


# --- plan_and_move（§7.3）----------------------------------------------------


def test_plan_and_move_stores_and_commits(session, make_config, tmp_path, ingest_row):
    config = make_config(source_path=str(tmp_path))
    src = write_file(str(tmp_path / "inbox" / "a.txt"), b"hello")
    result = mover.plan_and_move(session, config, ingest_row.id, _scanned(src), "inbox")

    assert result.ok
    assert result.stored_path_rel == "inbox/a.txt"
    assert result.archive_file.status == STATUS_STORED
    assert result.archive_file.verified_at is not None
    assert not os.path.exists(src)


def test_plan_and_move_conflict_on_duplicate_content(
    session, make_config, tmp_path, ingest_row, archive
):
    """UNIQUE 制約が二重登録を弾き、conflict として返る（§8）。"""
    config = make_config(source_path=str(tmp_path))
    first = write_file(str(tmp_path / "inbox" / "a.txt"), b"hello")
    mover.plan_and_move(session, config, ingest_row.id, _scanned(first), "inbox")

    second = write_file(str(tmp_path / "inbox2" / "b.txt"), b"hello")
    result = mover.plan_and_move(
        session, config, ingest_row.id, _scanned(second), "inbox2"
    )
    assert not result.ok
    assert result.conflict
    assert os.path.exists(second)  # 移動されていない


def test_plan_and_move_failure_reverts_reservation(
    session, make_config, tmp_path, ingest_row, monkeypatch
):
    """移動前に落ちたら予約行は消えて、次回そのまま再試行できる。"""
    config = make_config(source_path=str(tmp_path))
    src = write_file(str(tmp_path / "inbox" / "a.txt"), b"hello")

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mover, "safe_move", _boom)
    result = mover.plan_and_move(session, config, ingest_row.id, _scanned(src), "inbox")

    assert not result.ok
    assert session.query(ArchiveFile).count() == 0
    assert os.path.exists(src)


# --- クラッシュ復旧（§7.4）---------------------------------------------------


def _pending(session, archive, rel, src, content):
    write_file(src, content)
    row = ArchiveFile(
        filehash=hashing.file_hash(src),
        hash_algo="sha256",
        size=len(content),
        name=os.path.basename(rel),
        stored_path_rel=rel,
        origin_path_abs=src,
        status=STATUS_PENDING,
    )
    session.add(row)
    session.commit()
    return row


def test_recover_completed_move(session, make_config, archive, tmp_path):
    """保存先にあり、ハッシュ一致 → stored に確定し、残った元は消す。"""
    config = make_config()
    src = str(tmp_path / "inbox" / "a.txt")
    row = _pending(session, archive, "a.txt", src, b"hello")
    write_file(os.path.join(archive, "a.txt"), b"hello")

    counts = mover.recover_pending(session, config)
    assert counts["stored"] == 1
    assert row.status == STATUS_STORED
    assert not os.path.exists(src)


def test_recover_content_mismatch_is_failed(session, make_config, archive, tmp_path):
    """保存先に別物が置かれている → 自動では消さず failed にする。"""
    config = make_config()
    src = str(tmp_path / "inbox" / "a.txt")
    row = _pending(session, archive, "a.txt", src, b"hello")
    write_file(os.path.join(archive, "a.txt"), b"DIFFERENT")

    counts = mover.recover_pending(session, config)
    assert counts["failed"] == 1
    assert row.status == STATUS_FAILED
    assert os.path.exists(src)


def test_recover_before_move_reverts(session, make_config, archive, tmp_path):
    """保存先が無く元が残っている → 予約を取り消して次回やり直す。"""
    config = make_config()
    src = str(tmp_path / "inbox" / "a.txt")
    _pending(session, archive, "a.txt", src, b"hello")

    counts = mover.recover_pending(session, config)
    assert counts["reverted"] == 1
    assert session.query(ArchiveFile).count() == 0
    assert os.path.exists(src)


def test_recover_both_gone_is_failed(session, make_config, archive, tmp_path):
    """どちらにも実体が無い → 判断できないので failed で人に見せる。"""
    config = make_config()
    src = str(tmp_path / "inbox" / "a.txt")
    row = _pending(session, archive, "a.txt", src, b"hello")
    os.unlink(src)

    counts = mover.recover_pending(session, config)
    assert counts["failed"] == 1
    assert row.status == STATUS_FAILED


def test_cleanup_tmp_removes_part_files(archive):
    part = os.path.join(tmp_dir(archive), "abc.part")
    write_file(part, b"x")
    assert mover.cleanup_tmp(archive) == 1
    assert not os.path.exists(part)


def test_prune_empty_dirs_keeps_root(tmp_path):
    root = tmp_path / "inbox"
    (root / "a" / "b").mkdir(parents=True)
    assert mover.prune_empty_dirs(str(root)) == 2
    assert root.is_dir()
