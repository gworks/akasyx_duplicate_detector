# test_ingest.py - 判定表（設計書 §7.1）の全分岐
import os

import ingest
from conftest import write_file
from crawler_client import ScannedFile
from models import (
    RESULT_DUPLICATE,
    RESULT_FAILED,
    RESULT_MOVED,
    RESULT_SKIPPED_EMPTY,
    RESULT_SKIPPED_NOHASH,
    STATUS_MISSING,
    STATUS_STORED,
    ArchiveFile,
)


def _scanned(**kwargs):
    params = {
        "name": "a.txt",
        "path_abs": "/inbox/a.txt",
        "path_rel": "a.txt",
        "size": 10,
        "filehash": "h" * 64,
        "hash_algo": "sha256",
    }
    params.update(kwargs)
    return ScannedFile(**params)


def _store(session, filehash, size=10, rel="a.txt", status=STATUS_STORED):
    row = ArchiveFile(
        archive_id=1,
        filehash=filehash,
        hash_algo="sha256",
        size=size,
        name=os.path.basename(rel),
        stored_path_rel=rel,
        status=status,
    )
    session.add(row)
    session.commit()
    return row


def test_zero_byte_is_not_moved(session, make_config):
    """0バイトは保存価値が無いので移動しない（ユーザー決定・設計書 §7.1 #1）。"""
    config = make_config()
    result, _existing, message = ingest.judge(session, _scanned(size=0), config)
    assert result == RESULT_SKIPPED_EMPTY
    assert "min_size" in message


def test_min_size_zero_treats_empty_as_content(session, make_config):
    config = make_config(min_size=0)
    result, _existing, _message = ingest.judge(session, _scanned(size=0), config)
    assert result == RESULT_MOVED


def test_missing_hash_is_never_treated_as_new(session, make_config):
    """判定不能を新規と誤判定すると重複が保存フォルダに紛れ込む（§7.1 #2）。"""
    config = make_config()
    result, _existing, _message = ingest.judge(
        session, _scanned(filehash=None, hash_algo=None), config
    )
    assert result == RESULT_SKIPPED_NOHASH


def test_known_hash_is_duplicate(session, make_config):
    config = make_config()
    stored = _store(session, "h" * 64)
    result, existing, _message = ingest.judge(session, _scanned(), config)
    assert result == RESULT_DUPLICATE
    assert existing.id == stored.id


def test_unknown_hash_is_moved(session, make_config):
    config = make_config()
    _store(session, "z" * 64)
    result, existing, _message = ingest.judge(session, _scanned(), config)
    assert result == RESULT_MOVED
    assert existing is None


def test_hash_match_with_size_mismatch_is_failed(session, make_config):
    """SHA-256 が一致して size が違うのは DB 破損か実装バグの兆候（§7.1）。"""
    config = make_config()
    _store(session, "h" * 64, size=999)
    result, _existing, message = ingest.judge(session, _scanned(size=10), config)
    assert result == RESULT_FAILED
    assert "サイズが違います" in message


def test_missing_row_does_not_block_reregistration(session, make_config):
    """missing 行は内容を保持していないので、同内容の再登録を妨げない（§8）。"""
    config = make_config()
    _store(session, "h" * 64, status=STATUS_MISSING)
    result, _existing, _message = ingest.judge(session, _scanned(), config)
    assert result == RESULT_MOVED


def test_different_hash_algo_is_not_compared(session, make_config):
    """hash_algo が違うもの同士は照合してはならない（crawler §14）。"""
    config = make_config()
    _store(session, "h" * 64)
    scanned = _scanned(hash_algo="quickxor")
    result, _existing, _message = ingest.judge(session, scanned, config)
    assert result == RESULT_MOVED


def test_dest_subdir_defaults_to_source_folder_name(make_config, source):
    """年月フォルダの下に投入元フォルダ名が入る（既定）。"""
    config = make_config(source_path=source)
    assert ingest.resolve_dest_subdir(config) == os.path.basename(source)


def test_dest_subdir_explicit_overrides(make_config, source):
    config = make_config(source_path=source, dest_subdir="photos")
    assert ingest.resolve_dest_subdir(config) == "photos"


def test_dest_subdir_empty_string_means_archive_root(make_config, source):
    """--dest-subdir '' は「直下に展開」であって「未指定」ではない。"""
    config = make_config(source_path=source, dest_subdir="")
    assert ingest.resolve_dest_subdir(config) is None


def test_single_file_source_has_no_subdir(make_config, tmp_path):
    path = write_file(str(tmp_path / "solo" / "x.txt"), b"x")
    config = make_config(source_path=path)
    assert ingest.resolve_dest_subdir(config) is None
