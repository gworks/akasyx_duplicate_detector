# test_verify.py - 整合性チェック（設計書 §9）
import os
from datetime import datetime

import main
import verify
from conftest import write_file
from models import (
    DISPOSITION_QUARANTINE,
    MODE_VERIFY,
    RESULT_ARCHIVE_DUPLICATE,
    RESULT_HASH_MISMATCH,
    RESULT_MISSING,
    RESULT_RELOCATED,
    RESULT_UNREGISTERED,
    STATUS_MISSING,
    STATUS_STORED,
    STATUS_UNREGISTERED,
    ArchiveFile,
)


def _seed_archive(make_config, archive, source, tmp_path, fake_crawler, files):
    """add で保存フォルダを作ってから、verify 用の状態を整えます。"""
    for rel, content in files.items():
        write_file(os.path.join(source, rel), content)
    fake_crawler(source, str(tmp_path / "seed.db"))
    config = make_config(archive_root=archive, source_path=source)
    assert main.run(config) == main.EXIT_OK
    # 保存先は <今月の YYYY-MM>/<投入元フォルダ名>/<相対パス>（作成直後のファイルなので今月）
    return os.path.join(f"{datetime.now():%Y-%m}", os.path.basename(source))


def _verify(make_config, archive, tmp_path, fake_crawler, name, flags=()):
    fake_crawler(archive, str(tmp_path / f"verify_{name}.db"))
    config = make_config(
        mode=MODE_VERIFY, archive_root=archive, flag_quarantine=list(flags)
    )
    code = main.run(config)
    return code


def test_verify_all_ok(make_config, archive, source, tmp_path, fake_crawler, session):
    _seed_archive(
        make_config, archive, source, tmp_path, fake_crawler, {"a.txt": b"AAA"}
    )
    assert _verify(make_config, archive, tmp_path, fake_crawler, "ok") == main.EXIT_OK
    session.expire_all()
    row = session.query(ArchiveFile).one()
    assert row.status == STATUS_STORED
    assert row.verified_at is not None


def test_verify_detects_missing(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    sub = _seed_archive(
        make_config, archive, source, tmp_path, fake_crawler, {"a.txt": b"AAA"}
    )
    os.unlink(os.path.join(archive, sub, "a.txt"))

    _verify(make_config, archive, tmp_path, fake_crawler, "missing")
    session.expire_all()
    row = session.query(ArchiveFile).one()
    assert row.status == STATUS_MISSING  # 行は消さない


def test_verify_detects_relocation_not_missing_plus_unregistered(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    """人が動かしたファイルを2件に割らない（設計書 §9 の要点）。"""
    sub = _seed_archive(
        make_config, archive, source, tmp_path, fake_crawler, {"a.txt": b"AAA"}
    )
    moved_to = os.path.join(archive, "別の場所", "a.txt")
    os.makedirs(os.path.dirname(moved_to))
    os.replace(os.path.join(archive, sub, "a.txt"), moved_to)

    _verify(make_config, archive, tmp_path, fake_crawler, "reloc")
    session.expire_all()
    rows = session.query(ArchiveFile).all()
    assert len(rows) == 1
    assert rows[0].status == STATUS_STORED
    assert rows[0].stored_path_rel == "別の場所/a.txt"


def test_verify_detects_unregistered(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    _seed_archive(
        make_config, archive, source, tmp_path, fake_crawler, {"a.txt": b"AAA"}
    )
    write_file(os.path.join(archive, "外から.txt"), b"OUTSIDE")

    _verify(make_config, archive, tmp_path, fake_crawler, "unreg")
    session.expire_all()
    row = session.query(ArchiveFile).filter_by(status=STATUS_UNREGISTERED).one()
    assert row.stored_path_rel == "外から.txt"


def test_verify_detects_archive_duplicate(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    _seed_archive(
        make_config, archive, source, tmp_path, fake_crawler, {"a.txt": b"AAA"}
    )
    write_file(os.path.join(archive, "同じ内容.txt"), b"AAA")

    _verify(make_config, archive, tmp_path, fake_crawler, "dup")
    session.expire_all()
    items = {i.result for i in _items(session)}
    assert RESULT_ARCHIVE_DUPLICATE in items


def test_verify_detects_hash_mismatch_without_rewriting_db(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    sub = _seed_archive(
        make_config, archive, source, tmp_path, fake_crawler, {"a.txt": b"AAA"}
    )
    session.expire_all()
    original = session.query(ArchiveFile).one().filehash
    write_file(os.path.join(archive, sub, "a.txt"), b"REWRITTEN")

    _verify(make_config, archive, tmp_path, fake_crawler, "mismatch")
    session.expire_all()
    row = session.query(ArchiveFile).one()
    assert row.filehash == original  # DB は書き換えない
    assert RESULT_HASH_MISMATCH in {i.result for i in _items(session)}


def test_verify_revives_missing_when_file_returns(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    sub = _seed_archive(
        make_config, archive, source, tmp_path, fake_crawler, {"a.txt": b"AAA"}
    )
    target = os.path.join(archive, sub, "a.txt")
    os.unlink(target)
    _verify(make_config, archive, tmp_path, fake_crawler, "gone")
    session.expire_all()
    assert session.query(ArchiveFile).one().status == STATUS_MISSING

    write_file(target, b"AAA")  # 戻ってきた
    _verify(make_config, archive, tmp_path, fake_crawler, "back")
    session.expire_all()
    row = session.query(ArchiveFile).one()
    assert row.status == STATUS_STORED
    assert RESULT_RELOCATED in {i.result for i in _items(session)}


def test_flag_quarantine_sets_disposition_without_moving(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    """フラグを立てるだけで、ファイルは一切動かさない（設計書 §9）。"""
    _seed_archive(
        make_config, archive, source, tmp_path, fake_crawler, {"a.txt": b"AAA"}
    )
    stray = write_file(os.path.join(archive, "外から.txt"), b"OUTSIDE")

    _verify(
        make_config, archive, tmp_path, fake_crawler, "flag",
        flags=(RESULT_UNREGISTERED,),
    )
    session.expire_all()
    row = session.query(ArchiveFile).filter_by(status=STATUS_UNREGISTERED).one()
    assert row.disposition == DISPOSITION_QUARANTINE
    assert os.path.exists(stray)  # 動いていない


def test_verify_is_repeatable(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    """verify を繰り返しても unregistered 行が増えない。"""
    _seed_archive(
        make_config, archive, source, tmp_path, fake_crawler, {"a.txt": b"AAA"}
    )
    write_file(os.path.join(archive, "外から.txt"), b"OUTSIDE")
    _verify(make_config, archive, tmp_path, fake_crawler, "r1")
    _verify(make_config, archive, tmp_path, fake_crawler, "r2")
    session.expire_all()
    assert session.query(ArchiveFile).filter_by(status=STATUS_UNREGISTERED).count() == 1


def test_missing_result_constant_is_reported(
    make_config, archive, source, tmp_path, fake_crawler, session
):
    sub = _seed_archive(
        make_config, archive, source, tmp_path, fake_crawler, {"a.txt": b"AAA"}
    )
    os.unlink(os.path.join(archive, sub, "a.txt"))
    _verify(make_config, archive, tmp_path, fake_crawler, "m2")
    session.expire_all()
    assert RESULT_MISSING in {i.result for i in _items(session)}


def test_archive_stats(session, archive):
    session.add(
        ArchiveFile(
            archive_id=1,
            filehash="a" * 64, hash_algo="sha256", size=100, name="a",
            stored_path_rel="a", status=STATUS_STORED,
        )
    )
    session.commit()
    stats = verify.archive_stats(session, 1)
    assert stats[STATUS_STORED] == {"count": 1, "size": 100}


def _items(session):
    from models import IngestItem

    return session.query(IngestItem).all()
