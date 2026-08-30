# test_dedupe.py - 据え置いた重複の後始末（設計書 §10）
#
# 「2点検証のどちらかを崩したら削除されない」がこのファイルの最重要テスト。
import os

import dedupe
import main
from conftest import write_file
from models import (
    MODE_DELETE_DUPLICATES,
    RESOLUTION_DELETED,
    RESOLUTION_GONE,
    RESOLUTION_TRASHED,
    RESULT_DUPLICATE,
    STATUS_MISSING,
    ArchiveFile,
    IngestItem,
)


def _seed(make_config, archive, source, tmp_path, fake_crawler):
    """a.txt を保存フォルダへ、copy.txt を重複として投入元に残した状態を作ります。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")
    write_file(os.path.join(source, "copy.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "seed.db"))
    assert main.run(make_config(archive_root=archive, source_path=source)) == main.EXIT_OK
    # crawler は path_abs 昇順で返すので a.txt が移動、copy.txt が重複になる
    return os.path.join(source, "copy.txt")


def _dedupe(make_config, archive, **kwargs):
    config = make_config(
        mode=MODE_DELETE_DUPLICATES, archive_root=archive, **kwargs
    )
    return main.run(config)


def test_lists_without_deleting_by_default(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    """--yes が無ければ一覧表示のみ。何も消えない。"""
    leftover = _seed(make_config, archive, source, tmp_path, fake_crawler)
    assert _dedupe(make_config, archive) == main.EXIT_OK
    assert os.path.exists(leftover)
    session.expire_all()
    item = session.query(IngestItem).filter_by(result=RESULT_DUPLICATE).one()
    assert item.resolution is None


def test_deletes_with_yes(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    leftover = _seed(make_config, archive, source, tmp_path, fake_crawler)
    assert _dedupe(make_config, archive, assume_yes=True) == main.EXIT_OK
    assert not os.path.exists(leftover)
    session.expire_all()
    item = session.query(IngestItem).filter_by(result=RESULT_DUPLICATE).one()
    assert item.resolution == RESOLUTION_DELETED
    assert item.resolved_at is not None


def test_trash_dir_moves_instead_of_deleting(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    leftover = _seed(make_config, archive, source, tmp_path, fake_crawler)
    trash = str(tmp_path / "trash")
    assert (
        _dedupe(make_config, archive, assume_yes=True, trash_dir=trash) == main.EXIT_OK
    )
    assert not os.path.exists(leftover)
    assert os.path.exists(os.path.join(trash, "copy.txt"))
    session.expire_all()
    assert (
        session.query(IngestItem).filter_by(result=RESULT_DUPLICATE).one().resolution
        == RESOLUTION_TRASHED
    )


def test_does_not_delete_when_source_content_changed(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    """検証① 元ファイルの内容が変わっていたら消さない。"""
    leftover = _seed(make_config, archive, source, tmp_path, fake_crawler)
    write_file(leftover, b"CHANGED AFTER JUDGEMENT")

    _dedupe(make_config, archive, assume_yes=True)
    assert os.path.exists(leftover)
    session.expire_all()
    assert (
        session.query(IngestItem).filter_by(result=RESULT_DUPLICATE).one().resolution
        is None
    )


def test_does_not_delete_when_archive_copy_is_gone(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    """検証② 保存フォルダ側の実体が無ければ、投入元が最後の1本かもしれない。"""
    leftover = _seed(make_config, archive, source, tmp_path, fake_crawler)
    row = session.query(ArchiveFile).one()
    os.unlink(os.path.join(archive, *row.stored_path_rel.split("/")))

    _dedupe(make_config, archive, assume_yes=True)
    assert os.path.exists(leftover)
    session.expire_all()
    assert (
        session.query(IngestItem).filter_by(result=RESULT_DUPLICATE).one().resolution
        is None
    )


def test_does_not_delete_when_archive_row_is_missing(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    leftover = _seed(make_config, archive, source, tmp_path, fake_crawler)
    row = session.query(ArchiveFile).one()
    row.status = STATUS_MISSING
    session.commit()

    _dedupe(make_config, archive, assume_yes=True)
    assert os.path.exists(leftover)


def test_gone_source_is_marked_resolved(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    leftover = _seed(make_config, archive, source, tmp_path, fake_crawler)
    os.unlink(leftover)

    _dedupe(make_config, archive, assume_yes=True)
    session.expire_all()
    assert (
        session.query(IngestItem).filter_by(result=RESULT_DUPLICATE).one().resolution
        == RESOLUTION_GONE
    )


def test_dry_run_ingests_are_not_targets(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    """dry-run の実行は ar_ingest_items を書かないので対象にならない。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "dry.db"))
    main.run(make_config(archive_root=archive, source_path=source, dry_run=True))

    config = make_config(mode=MODE_DELETE_DUPLICATES, archive_root=archive)
    session.expire_all()
    assert dedupe._pending_items(session, config) == []


def test_processed_items_are_not_reprocessed(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    _seed(make_config, archive, source, tmp_path, fake_crawler)
    _dedupe(make_config, archive, assume_yes=True)
    config = make_config(mode=MODE_DELETE_DUPLICATES, archive_root=archive)
    session.expire_all()
    assert dedupe._pending_items(session, config) == []
