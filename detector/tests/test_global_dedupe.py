# test_global_dedupe.py - 重複判定を全保存フォルダ共通にする（#6）
#
# 正本 DB 1 つに登録された保存フォルダのどこかに同じ内容があれば、add は duplicate として投入元に残す。
# delete-duplicates は、実物がある保存フォルダ（別の保存フォルダでもよい）で 2 点検証してから消す。
import os
import shutil

import pytest

import dedupe
import ingest
import main
from conftest import archive_db_path, write_file
from database import get_session, master_db_lock
from errors import PreflightError
from models import (
    MODE_DELETE_DUPLICATES,
    MODE_REPORT,
    RESOLUTION_DELETED,
    RESULT_DUPLICATE,
    STATUS_STORED,
    ArchiveFile,
    IngestItem,
)


def _add(make_config, root, source, tmp_path, fake_crawler, **kwargs):
    fake_crawler(source, str(tmp_path / f"crawl_{os.path.basename(source)}.db"))
    return main.run(make_config(archive_root=root, source_path=source, **kwargs))


def _dirs(tmp_path, *names):
    paths = []
    for n in names:
        p = tmp_path / n
        p.mkdir()
        paths.append(str(p))
    return paths


def _db(tmp_path):
    return get_session(archive_db_path(tmp_path))


def _seed_cross(make_config, tmp_path, fake_crawler):
    """保存フォルダ A に x.txt を保存し、同じ内容の y.txt を B への add で重複として残します。"""
    a, b, src_a, src_b = _dirs(tmp_path, "archive_a", "archive_b", "in_a", "in_b")
    write_file(os.path.join(src_a, "x.txt"), b"SAME")
    assert _add(make_config, a, src_a, tmp_path, fake_crawler) == main.EXIT_OK
    leftover = write_file(os.path.join(src_b, "y.txt"), b"SAME")
    assert _add(make_config, b, src_b, tmp_path, fake_crawler) == main.EXIT_OK
    return a, b, leftover


def test_content_in_other_archive_is_duplicate(make_config, tmp_path, fake_crawler):
    a, b, leftover = _seed_cross(make_config, tmp_path, fake_crawler)
    assert os.path.exists(leftover)  # 投入元に残る
    sess, engine = _db(tmp_path)
    try:
        stored = sess.query(ArchiveFile).filter_by(status=STATUS_STORED).all()
        assert len(stored) == 1  # B には入っていない
        item = sess.query(IngestItem).filter_by(result=RESULT_DUPLICATE).one()
        assert item.archive_file_id == stored[0].id
        # どの保存フォルダにあるかをメッセージ（CSV）に出す
        assert a in (item.message or "")
    finally:
        sess.close(); engine.dispose()


def test_dry_run_sees_other_archive(make_config, tmp_path, fake_crawler, capsys):
    a, b, src_a, src_b = _dirs(tmp_path, "archive_a", "archive_b", "in_a", "in_b")
    write_file(os.path.join(src_a, "x.txt"), b"SAME")
    assert _add(make_config, a, src_a, tmp_path, fake_crawler) == main.EXIT_OK
    write_file(os.path.join(src_b, "y.txt"), b"SAME")
    assert _add(make_config, b, src_b, tmp_path, fake_crawler, dry_run=True) == main.EXIT_OK
    assert "duplicates (left in place): 1" in capsys.readouterr().out


def test_find_owning_is_global(session, archive, tmp_path):
    from models import Archive
    other = Archive(uid="other", root_abs=str(tmp_path / "other"))
    session.add(other)
    session.commit()
    row = ArchiveFile(
        archive_id=other.id, filehash="h", hash_algo="sha256", size=1, name="n",
        stored_path_rel="n", status=STATUS_STORED,
    )
    session.add(row)
    session.commit()
    assert ingest.find_owning(session, "h", "sha256", archive_id=1).id == row.id


def test_delete_duplicates_verifies_in_owner_archive(make_config, tmp_path, fake_crawler):
    a, b, leftover = _seed_cross(make_config, tmp_path, fake_crawler)
    cfg = make_config(mode=MODE_DELETE_DUPLICATES, archive_root=b, assume_yes=True)
    assert main.run(cfg) == main.EXIT_OK
    assert not os.path.exists(leftover)
    sess, engine = _db(tmp_path)
    try:
        item = sess.query(IngestItem).filter_by(result=RESULT_DUPLICATE).one()
        assert item.resolution == RESOLUTION_DELETED
    finally:
        sess.close(); engine.dispose()


def test_delete_duplicates_keeps_when_owner_archive_unavailable(
    make_config, tmp_path, fake_crawler
):
    """実物のある保存フォルダがつながっていなければ消さずに残し、理由を出す。"""
    a, b, leftover = _seed_cross(make_config, tmp_path, fake_crawler)
    shutil.move(a, str(tmp_path / "unplugged"))  # NAS が外れた状態を模す
    cfg = make_config(mode=MODE_DELETE_DUPLICATES, archive_root=b, assume_yes=True)
    assert main.run(cfg) == main.EXIT_OK
    assert os.path.exists(leftover)
    sess, engine = _db(tmp_path)
    try:
        item = sess.query(IngestItem).filter_by(result=RESULT_DUPLICATE).one()
        assert item.resolution is None
        verdict, message = dedupe.check_item(sess, cfg, item)
        assert verdict == dedupe.CHECK_ARCHIVE_UNAVAILABLE
        assert a in message
    finally:
        sess.close(); engine.dispose()


def test_delete_duplicates_never_deletes_the_archived_file_itself(
    make_config, tmp_path, fake_crawler
):
    """消す対象が保存済みの実物そのもの（同じファイル）なら消さない。"""
    a, b, leftover = _seed_cross(make_config, tmp_path, fake_crawler)
    sess, engine = _db(tmp_path)
    try:
        item = sess.query(IngestItem).filter_by(result=RESULT_DUPLICATE).one()
        owner = sess.get(ArchiveFile, item.archive_file_id)
        stored = os.path.join(a, *owner.stored_path_rel.split("/"))
        item.source_path_abs = stored  # 投入元が保存フォルダ A の中だった場合と同じ状態
        sess.commit()
    finally:
        sess.close(); engine.dispose()
    cfg = make_config(mode=MODE_DELETE_DUPLICATES, archive_root=b, assume_yes=True)
    assert main.run(cfg) == main.EXIT_OK
    assert os.path.exists(stored)


def _stored_files(root):
    return sorted(
        os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs if ".akasyx" not in d
    )


@pytest.mark.parametrize("inside", [True, False])
def test_add_refuses_source_overlapping_other_archive(
    make_config, tmp_path, fake_crawler, inside
):
    """投入元が登録済みの別の保存フォルダと重なる（中にある／中に含む）なら断る。

    通すと、A の保存済みファイルが自分自身の重複と判定され、delete-duplicates で消されうる。
    """
    (outer, b, src_a) = _dirs(tmp_path, "outer", "archive_b", "in_a")
    a = os.path.join(outer, "archive_a")
    os.makedirs(a)
    write_file(os.path.join(src_a, "x.txt"), b"SAME")
    assert _add(make_config, a, src_a, tmp_path, fake_crawler) == main.EXIT_OK
    before = _stored_files(a)
    assert before
    source = a if inside else outer
    fake_crawler(source, str(tmp_path / "crawl_overlap.db"))
    with pytest.raises(PreflightError, match="archive folder"):
        main.run(make_config(archive_root=b, source_path=source))
    assert _stored_files(a) == before


def test_runs_are_exclusive_per_master_db(make_config, tmp_path, fake_crawler):
    """別の保存フォルダでも、同じ正本 DB を使う実行は同時に 1 本だけ。"""
    a, b, src_b = _dirs(tmp_path, "archive_a", "archive_b", "in_b")
    write_file(os.path.join(src_b, "y.txt"), b"SAME")
    fake_crawler(src_b, str(tmp_path / "crawl.db"))
    cfg = make_config(archive_root=b, source_path=src_b)
    with master_db_lock(cfg.archive_db):  # 保存フォルダ A への add が実行中の状態
        with pytest.raises(PreflightError, match="in use"):
            main.run(cfg)
    assert os.path.exists(os.path.join(src_b, "y.txt"))
    assert main.run(cfg) == main.EXIT_OK  # 終われば実行できる


def test_every_command_takes_the_master_db_lock(make_config, tmp_path):
    """report も起動時に中断分の復旧（書き込み）をするので、ロックを取る。"""
    (a,) = _dirs(tmp_path, "archive_a")
    cfg = make_config(mode=MODE_REPORT, archive_root=a)
    with master_db_lock(cfg.archive_db):
        with pytest.raises(PreflightError, match="in use"):
            main.run(cfg)


# --- レビュー指摘（2026-09-27）------------------------------------------------


def test_pending_row_of_other_archive_is_not_an_owner(make_config, tmp_path, fake_crawler):
    """別の保存フォルダで中断したまま残った pending 行は持ち主にしない。

    参照すると、その保存フォルダの起動時復旧が pending 行を消すときに FK 違反で止まり、
    内容もどこにも保存されないまま「重複」扱いになる。
    """
    from models import STATUS_PENDING, Archive
    a, b, src_a, src_b = _dirs(tmp_path, "archive_a", "archive_b", "in_a", "in_b")
    write_file(os.path.join(src_a, "seed.txt"), b"SEED")
    assert _add(make_config, a, src_a, tmp_path, fake_crawler) == main.EXIT_OK
    crashed = write_file(os.path.join(src_a, "x.txt"), b"SAME")
    sess, engine = _db(tmp_path)
    try:
        aid = sess.query(Archive).filter_by(root_abs=os.path.realpath(a)).one().id
        sess.add(ArchiveFile(  # A への add が予約直後に落ちた状態
            archive_id=aid, filehash=__import__("utl.hashing", fromlist=["x"]).file_hash(crashed),
            hash_algo="sha256", size=4, name="x.txt", stored_path_rel="2026-09/x.txt",
            origin_path_abs=crashed, status=STATUS_PENDING,
        ))
        sess.commit()
    finally:
        sess.close(); engine.dispose()
    write_file(os.path.join(src_b, "y.txt"), b"SAME")
    assert _add(make_config, b, src_b, tmp_path, fake_crawler) == main.EXIT_OK
    assert not os.path.exists(os.path.join(src_b, "y.txt"))  # B に保存された
    # A の復旧（pending 行の削除）が FK 違反で止まらない
    assert main.run(make_config(mode=MODE_REPORT, archive_root=a)) == main.EXIT_OK


def test_moved_current_archive_is_not_checked_against_its_old_location(
    make_config, tmp_path, fake_crawler
):
    """保存フォルダを移動した直後の add が、移動前の登録上の場所と比べられて断られない。"""
    pics, nas = _dirs(tmp_path, "pictures", "nas")
    old = os.path.join(pics, "archive")
    os.makedirs(old)
    src0 = os.path.join(tmp_path, "in0")
    write_file(os.path.join(src0, "seed.txt"), b"SEED")
    assert _add(make_config, old, src0, tmp_path, fake_crawler) == main.EXIT_OK
    new = os.path.join(nas, "archive")
    shutil.move(old, new)
    write_file(os.path.join(pics, "p.jpg"), b"PHOTO")
    assert _add(make_config, new, pics, tmp_path, fake_crawler) == main.EXIT_OK


def test_deleted_registered_archive_does_not_block_its_parent(
    make_config, tmp_path, fake_crawler
):
    """実体を消した（試しに使っただけの）保存フォルダの登録は、上位フォルダを投入元にするのを妨げない。"""
    desk, real = _dirs(tmp_path, "desktop", "real_archive")
    trial = os.path.join(desk, "trial_archive")
    os.makedirs(trial)
    src0 = os.path.join(tmp_path, "in0")
    write_file(os.path.join(src0, "seed.txt"), b"SEED")
    assert _add(make_config, trial, src0, tmp_path, fake_crawler) == main.EXIT_OK
    shutil.rmtree(trial)
    write_file(os.path.join(desk, "d.txt"), b"DESK")
    assert _add(make_config, real, desk, tmp_path, fake_crawler) == main.EXIT_OK


def test_owner_in_current_archive_is_preferred(make_config, tmp_path, fake_crawler):
    """同じ内容が複数の保存フォルダにあるとき（#6 より前のデータ）、今の保存フォルダの行を優先する。"""
    from models import Archive
    a, b, src_a, src_b = _dirs(tmp_path, "archive_a", "archive_b", "in_a", "in_b")
    write_file(os.path.join(src_a, "x.txt"), b"SAME")
    assert _add(make_config, a, src_a, tmp_path, fake_crawler) == main.EXIT_OK
    write_file(os.path.join(src_b, "seed.txt"), b"SEED")
    assert _add(make_config, b, src_b, tmp_path, fake_crawler) == main.EXIT_OK
    sess, engine = _db(tmp_path)
    try:  # #6 より前に B にも同じ内容が保存されていた状態を作る
        bid = sess.query(Archive).filter_by(root_abs=os.path.realpath(b)).one().id
        write_file(os.path.join(b, "old", "x.txt"), b"SAME")
        sess.add(ArchiveFile(
            archive_id=bid, filehash=__import__("utl.hashing", fromlist=["x"]).file_hash(
                os.path.join(b, "old", "x.txt")),
            hash_algo="sha256", size=4, name="x.txt", stored_path_rel="old/x.txt",
            status=STATUS_STORED,
        ))
        sess.commit()
    finally:
        sess.close(); engine.dispose()
    leftover = write_file(os.path.join(src_b, "again.txt"), b"SAME")
    assert _add(make_config, b, src_b, tmp_path, fake_crawler) == main.EXIT_OK
    shutil.move(a, str(tmp_path / "unplugged"))  # A（id が小さい）を外す
    cfg = make_config(mode=MODE_DELETE_DUPLICATES, archive_root=b, assume_yes=True)
    assert main.run(cfg) == main.EXIT_OK
    assert not os.path.exists(leftover)  # B の実物で検証して消せた


def test_master_db_lock_follows_symlinks(tmp_path):
    """正本 DB ファイルへのシンボリックリンクで指しても、同じロックになる。

    （フォルダのシンボリックリンクはロックファイルも同じ実体に解決されるので、問題になるのはファイルの別名）
    """
    real_db = tmp_path / "data" / "archive.db"
    real_db.parent.mkdir()
    real_db.write_bytes(b"")
    link = tmp_path / "link.db"
    os.symlink(real_db, link)
    with master_db_lock(str(real_db)):
        with pytest.raises(PreflightError, match="in use"):
            with master_db_lock(str(link)):
                pass


def test_owner_location_checked_once_per_archive(
    make_config, tmp_path, fake_crawler, monkeypatch
):
    """delete-duplicates は、実物のある保存フォルダの確認を保存フォルダごとに 1 回だけ行う。"""
    import archives
    a, b, src_a, src_b = _dirs(tmp_path, "archive_a", "archive_b", "in_a", "in_b")
    for i in range(3):
        write_file(os.path.join(src_a, f"x{i}.txt"), f"S{i}".encode())
    assert _add(make_config, a, src_a, tmp_path, fake_crawler) == main.EXIT_OK
    for i in range(3):
        write_file(os.path.join(src_b, f"y{i}.txt"), f"S{i}".encode())
    assert _add(make_config, b, src_b, tmp_path, fake_crawler) == main.EXIT_OK
    calls = []
    real = archives.read_uid
    monkeypatch.setattr(archives, "read_uid", lambda root: calls.append(root) or real(root))
    cfg = make_config(mode=MODE_DELETE_DUPLICATES, archive_root=b)
    assert main.run(cfg) == main.EXIT_OK
    assert calls.count(os.path.realpath(a)) == 1


# --- レビュー指摘 2 回目（2026-09-27）: 保存フォルダの目印（.akasyx/archive.id）で判定する -----------


def _file_hash(path):
    from utl import hashing
    return hashing.file_hash(path)


def test_add_refuses_source_containing_unregistered_archive(make_config, tmp_path, fake_crawler):
    """投入元の中に保存フォルダ（登録が古い・別の正本 DB のもの）があれば断り、何も動かさない。"""
    a, src = _dirs(tmp_path, "archive_a", "inbox")
    foreign = os.path.join(src, "moved_archive")
    write_file(os.path.join(foreign, ".akasyx", "archive.id"), b"someone-else\n")
    write_file(os.path.join(foreign, "2026-01", "x.jpg"), b"X")
    fake_crawler(src, str(tmp_path / "crawl.db"))
    with pytest.raises(PreflightError, match="archive folder"):
        main.run(make_config(archive_root=a, source_path=src))
    assert os.path.exists(os.path.join(foreign, ".akasyx", "archive.id"))
    assert os.path.exists(os.path.join(foreign, "2026-01", "x.jpg"))


def test_refused_add_does_not_register_the_target(make_config, tmp_path, fake_crawler):
    """投入元が保存フォルダの中なら、取り込み先を登録する前に断る（登録だけ残さない）。"""
    from models import Archive
    a, c, src_a = _dirs(tmp_path, "archive_a", "archive_c", "in_a")
    write_file(os.path.join(src_a, "x.txt"), b"SAME")
    assert _add(make_config, a, src_a, tmp_path, fake_crawler) == main.EXIT_OK
    inner = os.path.join(a, "sub")
    os.makedirs(inner)
    with pytest.raises(PreflightError, match="archive folder"):
        main.run(make_config(archive_root=c, source_path=inner))
    sess, engine = _db(tmp_path)
    try:
        assert sess.query(Archive).count() == 1
    finally:
        sess.close(); engine.dispose()
    assert not os.path.exists(os.path.join(c, ".akasyx", "archive.id"))


def test_folder_that_is_no_longer_an_archive_does_not_block(make_config, tmp_path, fake_crawler):
    """誤って保存フォルダにして .akasyx を消したフォルダは、登録が残っていても投入元にできる。"""
    docs, real, src0 = _dirs(tmp_path, "documents", "real_archive", "in0")
    write_file(os.path.join(src0, "seed.txt"), b"SEED")
    assert _add(make_config, docs, src0, tmp_path, fake_crawler) == main.EXIT_OK
    shutil.rmtree(os.path.join(docs, ".akasyx"))
    scans = os.path.join(docs, "scans")
    write_file(os.path.join(scans, "s.pdf"), b"SCAN")
    assert _add(make_config, real, scans, tmp_path, fake_crawler) == main.EXIT_OK


def test_leftover_akasyx_folder_in_source_is_not_ingested(make_config, tmp_path, fake_crawler):
    """識別子の無い .akasyx（ロック・作業ファイルの残り）は取り込まない。"""
    a, src = _dirs(tmp_path, "archive_a", "inbox")
    write_file(os.path.join(src, "old", ".akasyx", "tmp", "x.part"), b"PART")
    write_file(os.path.join(src, "keep.txt"), b"KEEP")
    assert _add(make_config, a, src, tmp_path, fake_crawler) == main.EXIT_OK
    assert os.path.exists(os.path.join(src, "old", ".akasyx", "tmp", "x.part"))


def test_delete_duplicates_keeps_source_inside_an_archive(make_config, tmp_path, fake_crawler):
    """投入元のファイルが（あとから）保存フォルダの中になっていれば、実体が別でも消さない。"""
    a, b, leftover = _seed_cross(make_config, tmp_path, fake_crawler)
    src_b = os.path.dirname(leftover)
    write_file(os.path.join(src_b, ".akasyx", "archive.id"), b"became-an-archive\n")
    cfg = make_config(mode=MODE_DELETE_DUPLICATES, archive_root=b, assume_yes=True)
    assert main.run(cfg) == main.EXIT_OK
    assert os.path.exists(leftover)
    sess, engine = _db(tmp_path)
    try:
        item = sess.query(IngestItem).filter_by(result=RESULT_DUPLICATE).one()
        assert dedupe.check_item(sess, cfg, item)[0] == dedupe.CHECK_SOURCE_IN_ARCHIVE
    finally:
        sess.close(); engine.dispose()


def test_delete_duplicates_tries_other_copies(make_config, tmp_path, fake_crawler):
    """判定時に参照した実物が消えていても、別の保存フォルダに検証できる実物があれば消せる。"""
    from models import Archive
    a, b, leftover = _seed_cross(make_config, tmp_path, fake_crawler)
    c, src_c = _dirs(tmp_path, "archive_c", "in_c")
    write_file(os.path.join(src_c, "seed.txt"), b"SEED")
    assert _add(make_config, c, src_c, tmp_path, fake_crawler) == main.EXIT_OK
    sess, engine = _db(tmp_path)
    try:
        item = sess.query(IngestItem).filter_by(result=RESULT_DUPLICATE).one()
        owner = sess.get(ArchiveFile, item.archive_file_id)
        os.unlink(os.path.join(a, *owner.stored_path_rel.split("/")))  # A の実物を手で消した
        cid = sess.query(Archive).filter_by(root_abs=os.path.realpath(c)).one().id
        copy = write_file(os.path.join(c, "old", "x.txt"), b"SAME")  # #6 より前に C にもあった
        sess.add(ArchiveFile(
            archive_id=cid, filehash=_file_hash(copy), hash_algo="sha256", size=4,
            name="x.txt", stored_path_rel="old/x.txt", status=STATUS_STORED,
        ))
        sess.commit()
    finally:
        sess.close(); engine.dispose()
    cfg = make_config(mode=MODE_DELETE_DUPLICATES, archive_root=b, assume_yes=True)
    assert main.run(cfg) == main.EXIT_OK
    assert not os.path.exists(leftover)


def test_master_db_lock_left_by_dead_process_is_taken(tmp_path):
    """落ちたプロセスが残したロックファイルは、そのまま取れる（OS のロックは終了で外れる）。"""
    from database import master_db_lock_path
    db = str(tmp_path / "archive.db")
    write_file(master_db_lock_path(db), b"999999")
    with master_db_lock(db):
        pass
    with master_db_lock(db):  # 解放後にもう一度取れる
        pass


# --- レビュー指摘 3 回目（2026-09-27）------------------------------------------


def _archive_id_of(tmp_path, root):
    from models import Archive
    sess, engine = _db(tmp_path)
    try:
        return sess.query(Archive).filter_by(root_abs=os.path.realpath(root)).one().id
    finally:
        sess.close(); engine.dispose()


def test_forget_lets_content_of_deleted_archive_be_stored_again(
    make_config, tmp_path, fake_crawler, capsys
):
    """消した保存フォルダの登録を archives --forget で外せば、その内容を別の保存フォルダに保存できる。"""
    t, r, src_t, src_r = _dirs(tmp_path, "trial", "real", "in_t", "in_r")
    write_file(os.path.join(src_t, "x.txt"), b"SAME")
    assert _add(make_config, t, src_t, tmp_path, fake_crawler) == main.EXIT_OK
    tid = _archive_id_of(tmp_path, t)
    shutil.rmtree(t)
    write_file(os.path.join(src_r, "y.txt"), b"SAME")
    assert _add(make_config, r, src_r, tmp_path, fake_crawler) == main.EXIT_OK
    assert os.path.exists(os.path.join(src_r, "y.txt"))  # まだ T の重複扱い
    sess, engine = _db(tmp_path)
    try:  # つながっていない保存フォルダにあることを CSV のメッセージで知らせる
        item = sess.query(IngestItem).filter_by(result=RESULT_DUPLICATE).one()
        assert "not available" in item.message and "--forget" in item.message
    finally:
        sess.close(); engine.dispose()

    argv = ["archives", "--forget", str(tid), "--archive-db", archive_db_path(tmp_path)]
    assert main.main(argv) == main.EXIT_OK
    assert _add(make_config, r, src_r, tmp_path, fake_crawler) == main.EXIT_OK
    assert not os.path.exists(os.path.join(src_r, "y.txt"))  # 今度は R に保存された


def test_forget_refuses_archive_that_is_present(make_config, tmp_path, fake_crawler):
    t, src_t = _dirs(tmp_path, "trial", "in_t")
    write_file(os.path.join(src_t, "x.txt"), b"SAME")
    assert _add(make_config, t, src_t, tmp_path, fake_crawler) == main.EXIT_OK
    tid = _archive_id_of(tmp_path, t)
    argv = ["archives", "--forget", str(tid), "--archive-db", archive_db_path(tmp_path)]
    assert main.main(argv) == main.EXIT_REJECTED
    argv = ["archives", "--forget", "999", "--archive-db", archive_db_path(tmp_path)]
    assert main.main(argv) == main.EXIT_REJECTED


def test_add_skips_file_whose_real_location_is_in_an_archive(
    make_config, tmp_path, monkeypatch
):
    """シンボリックリンクを辿って保存フォルダの下位フォルダに入っても、その保存物は動かさない。"""
    import crawler_client
    from conftest import build_crawler_db
    a, x, src = _dirs(tmp_path, "archive_a", "foreign_x", "inbox")
    write_file(os.path.join(x, ".akasyx", "archive.id"), b"other-db\n")
    kept = write_file(os.path.join(x, "2024-01", "x.jpg"), b"XJPG")
    os.symlink(os.path.join(x, "2024-01"), os.path.join(src, "link"))
    write_file(os.path.join(src, "own.txt"), b"OWN")
    crawl_db = str(tmp_path / "crawl.db")

    def _run(target, config, extra_excludes=()):
        scan_id = build_crawler_db(crawl_db, target)
        import sqlite3
        conn = sqlite3.connect(crawl_db)  # --follow-symlinks で辿った分を足す
        conn.execute(
            "INSERT INTO fs_files (scan_id, last_seen_scan_id, name, path_abs, path_rel, size,"
            " filehash, hash_algo, status) VALUES (?, ?, 'x.jpg', ?, 'link/x.jpg', 4, ?, 'sha256', 'active')",
            (scan_id, scan_id, os.path.join(src, "link", "x.jpg"), _file_hash(kept)),
        )
        conn.commit(); conn.close()
        return crawler_client.CrawlerScan(db_path=crawl_db, scan_id=scan_id, status="completed", root_dir=target)

    monkeypatch.setattr(crawler_client, "resolve_crawler_repo", lambda path: path)
    monkeypatch.setattr(crawler_client, "run_crawler", _run)
    cfg = make_config(archive_root=a, source_path=src, follow_symlinks=True)
    assert main.run(cfg) == main.EXIT_OK
    assert os.path.exists(kept)
    assert not os.path.exists(os.path.join(src, "own.txt"))  # 普通のファイルは取り込まれる


def test_lock_error_other_than_contention_is_reported(tmp_path, monkeypatch):
    """ロックに対応しない場所などのエラーは、トレースバックではなく断りとして返す。"""
    import errno
    import fcntl

    def _flock(fd, op):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(fcntl, "flock", _flock)
    with pytest.raises(PreflightError, match="lock"):
        with master_db_lock(str(tmp_path / "archive.db")):
            pass


def test_hardlinked_duplicate_can_be_deleted(make_config, tmp_path, fake_crawler):
    """投入元が保存物のハードリンクなら、消してもリンクが 1 本減るだけなので消してよい。"""
    a, b, leftover = _seed_cross(make_config, tmp_path, fake_crawler)
    sess, engine = _db(tmp_path)
    try:
        item = sess.query(IngestItem).filter_by(result=RESULT_DUPLICATE).one()
        stored = os.path.join(a, *sess.get(ArchiveFile, item.archive_file_id).stored_path_rel.split("/"))
    finally:
        sess.close(); engine.dispose()
    os.unlink(leftover)
    os.link(stored, leftover)
    cfg = make_config(mode=MODE_DELETE_DUPLICATES, archive_root=b, assume_yes=True)
    assert main.run(cfg) == main.EXIT_OK
    assert not os.path.exists(leftover)
    assert os.path.exists(stored)


def test_source_containing_archive_is_refused_before_scan_and_registration(
    make_config, tmp_path, monkeypatch
):
    """投入元の中に保存フォルダがあれば、走査（全ハッシュ）の前・取り込み先の登録の前に断る。"""
    import crawler_client
    from models import Archive
    d, src = _dirs(tmp_path, "fresh_d", "inbox")
    write_file(os.path.join(src, "old_archive", ".akasyx", "archive.id"), b"x\n")

    def _never(*a, **k):
        raise AssertionError("crawler must not run")

    monkeypatch.setattr(crawler_client, "resolve_crawler_repo", lambda path: path)
    monkeypatch.setattr(crawler_client, "run_crawler", _never)
    with pytest.raises(PreflightError, match="archive folder"):
        main.run(make_config(archive_root=d, source_path=src))
    assert not os.path.exists(os.path.join(d, ".akasyx", "archive.id"))
    sess, engine = _db(tmp_path)
    try:
        assert sess.query(Archive).count() == 0
    finally:
        sess.close(); engine.dispose()


def test_source_under_folder_named_akasyx_is_ingested(make_config, tmp_path, fake_crawler):
    """投入元より上位のフォルダ名が .akasyx でも、投入元の中のファイルは取り込む。"""
    a, base = _dirs(tmp_path, "archive_a", "vol")
    src = os.path.join(base, ".akasyx", "tmp", "restore")
    write_file(os.path.join(src, "r.txt"), b"RESTORE")
    assert _add(make_config, a, src, tmp_path, fake_crawler) == main.EXIT_OK
    assert not os.path.exists(os.path.join(src, "r.txt"))


def test_source_in_archive_is_decided_before_hashing(make_config, tmp_path, fake_crawler, monkeypatch):
    a, b, leftover = _seed_cross(make_config, tmp_path, fake_crawler)
    write_file(os.path.join(os.path.dirname(leftover), ".akasyx", "archive.id"), b"x\n")
    from utl import hashing
    calls = []
    real = hashing.file_hash
    monkeypatch.setattr(hashing, "file_hash", lambda p, *a, **k: calls.append(p) or real(p, *a, **k))
    cfg = make_config(mode=MODE_DELETE_DUPLICATES, archive_root=b)
    assert main.run(cfg) == main.EXIT_OK
    assert leftover not in calls


def test_trash_dir_inside_another_archive_is_refused(make_config, tmp_path, fake_crawler):
    a, b, leftover = _seed_cross(make_config, tmp_path, fake_crawler)
    cfg = make_config(
        mode=MODE_DELETE_DUPLICATES, archive_root=b, assume_yes=True,
        trash_dir=os.path.join(a, "trash"),
    )
    with pytest.raises(PreflightError, match="archive folder"):
        main.run(cfg)
    assert os.path.exists(leftover)


def test_archive_scan_survives_symlink_loop(tmp_path):
    """--follow-symlinks の事前チェックが、シンボリックリンクの循環で止まらない。"""
    import archives
    src = tmp_path / "inbox"
    (src / "a").mkdir(parents=True)
    os.symlink(src, src / "a" / "loop")
    assert archives.archives_below(str(src), follow_symlinks=True) == []
