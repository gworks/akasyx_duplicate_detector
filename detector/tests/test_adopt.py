# test_adopt.py - 中身のある保存フォルダを実体から登録し直す（#4 / #5）
#
# 中身があるのに保存記録が 1 件も無い保存フォルダは、adopt 以外の全コマンドで断る。
# adopt は保存フォルダを走査し、実体を stored として登録する（同じ内容は全保存フォルダで 1 つ。
# 2 つ目以降は unregistered にして archive_duplicate として報告する）。
import os
import shutil

import pytest

import crawler_client
import main
from conftest import archive_db_path, write_file
from database import archive_id_path, get_session
from errors import PreflightError
from models import (
    MODE_ADOPT,
    MODE_REPORT,
    MODE_VERIFY,
    RESULT_ARCHIVE_DUPLICATE,
    STATUS_STORED,
    STATUS_UNREGISTERED,
    Archive,
    ArchiveFile,
    IngestItem,
)


def _db(tmp_path):
    return get_session(archive_db_path(tmp_path))


def _rows(tmp_path, **filters):
    sess, engine = _db(tmp_path)
    try:
        return [
            (r.stored_path_rel, r.status)
            for r in sess.query(ArchiveFile).filter_by(**filters).order_by(ArchiveFile.stored_path_rel)
        ]
    finally:
        sess.close(); engine.dispose()


def _adopt(make_config, root, tmp_path, fake_crawler):
    fake_crawler(root, str(tmp_path / "adopt_crawl.db"))
    return main.run(make_config(mode=MODE_ADOPT, archive_root=root))


@pytest.fixture
def photos(tmp_path):
    """中身のある既存フォルダ（まだ保存フォルダではない）。"""
    root = tmp_path / "photos"
    write_file(str(root / "2024" / "a.jpg"), b"AAAA")
    write_file(str(root / "2024" / "b.jpg"), b"BBBB")
    return str(root)


# --- #4: 中身のあるフォルダを初めて保存フォルダにする -------------------------------


def test_add_refuses_folder_with_content_and_no_records(make_config, tmp_path, fake_crawler, photos):
    """中身のあるフォルダを取り込み先にすると、何も書かずに断り、adopt を案内する。"""
    src = str(tmp_path / "inbox")
    write_file(os.path.join(src, "a.jpg"), b"AAAA")
    fake_crawler(src, str(tmp_path / "c.db"))
    with pytest.raises(PreflightError, match="adopt"):
        main.run(make_config(archive_root=photos, source_path=src))
    assert os.path.exists(os.path.join(src, "a.jpg"))
    assert not os.path.exists(archive_id_path(photos))  # 断る前に識別子を書かない


@pytest.mark.parametrize("mode", [MODE_VERIFY, MODE_REPORT])
def test_other_commands_also_refuse_until_adopted(make_config, tmp_path, fake_crawler, photos, mode):
    fake_crawler(photos, str(tmp_path / "c.db"))
    with pytest.raises(PreflightError, match="adopt"):
        main.run(make_config(mode=mode, archive_root=photos))


def test_adopt_registers_contents_as_stored(make_config, tmp_path, fake_crawler, photos):
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    assert _rows(tmp_path) == [("2024/a.jpg", STATUS_STORED), ("2024/b.jpg", STATUS_STORED)]
    assert os.path.exists(archive_id_path(photos))
    # adopt した内容と同じファイルは、取り込みで重複として投入元に残る（#4 の重複が入らない）
    src = str(tmp_path / "inbox")
    write_file(os.path.join(src, "copy_of_a.jpg"), b"AAAA")
    fake_crawler(src, str(tmp_path / "c2.db"))
    assert main.run(make_config(archive_root=photos, source_path=src)) == main.EXIT_OK
    assert os.path.exists(os.path.join(src, "copy_of_a.jpg"))


def test_empty_folder_still_opens_without_adopt(make_config, tmp_path, fake_crawler):
    """空のフォルダはこれまでどおり、そのまま保存フォルダにできる。"""
    empty = str(tmp_path / "empty_archive")
    os.makedirs(empty)
    src = str(tmp_path / "inbox")
    write_file(os.path.join(src, "a.jpg"), b"AAAA")
    fake_crawler(src, str(tmp_path / "c.db"))
    assert main.run(make_config(archive_root=empty, source_path=src)) == main.EXIT_OK


def test_files_copied_into_registered_archive_are_left_to_verify(make_config, tmp_path, fake_crawler):
    """登録済みの保存フォルダに、あとから手でファイルを入れた場合は adopt を求めない（従来どおり verify の扱い）。

    断るのは「adopt 待ち」の印があるときだけ（2026-09-28 決定。記録の状態から推し量る判定は、verify・forget で
    状態が書き換わるたびにずれたのでやめた）。手で入れたファイルは、記録が無ければ adopt で登録することもできる。
    """
    arch = str(tmp_path / "arch")
    os.makedirs(arch)
    assert main.run(make_config(mode=MODE_REPORT, archive_root=arch)) == main.EXIT_OK  # 空で登録
    write_file(os.path.join(arch, "manual", "x.jpg"), b"XXXX")
    assert main.run(make_config(mode=MODE_REPORT, archive_root=arch)) == main.EXIT_OK
    assert _adopt(make_config, arch, tmp_path, fake_crawler) == main.EXIT_OK  # 保存記録が無いので adopt もできる
    assert _rows(tmp_path) == [("manual/x.jpg", STATUS_STORED)]


# --- #5: 正本 DB を失った保存フォルダを復旧する -------------------------------------


def test_adopt_recovers_archive_after_master_db_is_lost(make_config, tmp_path, fake_crawler, photos):
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    with open(archive_id_path(photos), encoding="utf-8") as f:
        uid = f.read().strip()
    os.remove(archive_db_path(tmp_path))  # 正本 DB を失った（別の Mac への移行など）
    for sfx in ("-wal", "-shm"):
        if os.path.exists(archive_db_path(tmp_path) + sfx):
            os.remove(archive_db_path(tmp_path) + sfx)
    with pytest.raises(PreflightError, match="adopt"):
        main.run(make_config(mode=MODE_REPORT, archive_root=photos))
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    sess, engine = _db(tmp_path)
    try:
        assert sess.query(Archive).one().uid == uid  # 識別子を引き継ぐ
    finally:
        sess.close(); engine.dispose()
    assert _rows(tmp_path, status=STATUS_STORED) == [
        ("2024/a.jpg", STATUS_STORED), ("2024/b.jpg", STATUS_STORED)
    ]


# --- 重複 ---------------------------------------------------------------------------


def test_duplicates_inside_the_folder_keep_one_stored(make_config, tmp_path, fake_crawler, photos):
    write_file(os.path.join(photos, "2025", "a_copy.jpg"), b"AAAA")
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    assert _rows(tmp_path) == [
        ("2024/a.jpg", STATUS_STORED),
        ("2024/b.jpg", STATUS_STORED),
        ("2025/a_copy.jpg", STATUS_UNREGISTERED),
    ]
    sess, engine = _db(tmp_path)
    try:
        item = sess.query(IngestItem).filter_by(name="a_copy.jpg").one()
        assert item.result == RESULT_ARCHIVE_DUPLICATE
    finally:
        sess.close(); engine.dispose()
    assert os.path.exists(os.path.join(photos, "2025", "a_copy.jpg"))  # ファイルは動かさない


def test_content_stored_in_another_archive_is_not_stored_again(make_config, tmp_path, fake_crawler, photos):
    other = str(tmp_path / "other")
    os.makedirs(other)
    src = str(tmp_path / "inbox")
    write_file(os.path.join(src, "a.jpg"), b"AAAA")
    fake_crawler(src, str(tmp_path / "c.db"))
    assert main.run(make_config(archive_root=other, source_path=src)) == main.EXIT_OK
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    assert ("2024/a.jpg", STATUS_UNREGISTERED) in _rows(tmp_path)
    assert ("2024/b.jpg", STATUS_STORED) in _rows(tmp_path)


# --- 再実行・途中失敗 -----------------------------------------------------------------


def test_adopt_twice_is_refused(make_config, tmp_path, fake_crawler, photos):
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    with pytest.raises(PreflightError, match="verify"):
        _adopt(make_config, photos, tmp_path, fake_crawler)


def test_interrupted_adopt_leaves_no_records_and_can_be_retried(
    make_config, tmp_path, fake_crawler, photos, monkeypatch
):
    def _broken(*a, **k):
        raise RuntimeError("crawler crashed")

    monkeypatch.setattr(crawler_client, "resolve_crawler_repo", lambda path: path)
    monkeypatch.setattr(crawler_client, "run_crawler", _broken)
    assert main.run(make_config(mode=MODE_ADOPT, archive_root=photos)) == main.EXIT_FATAL
    assert _rows(tmp_path) == []
    with pytest.raises(PreflightError, match="adopt"):  # 途中で落ちても他のコマンドは断ったまま
        main.run(make_config(mode=MODE_REPORT, archive_root=photos))
    monkeypatch.undo()
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    assert len(_rows(tmp_path, status=STATUS_STORED)) == 2


# --- 登録しないもの ---------------------------------------------------------------------


def test_adopt_skips_metadata_and_empty_files(make_config, tmp_path, fake_crawler, photos):
    write_file(os.path.join(photos, ".DS_Store"), b"junk")
    write_file(os.path.join(photos, "2024", "empty.txt"), b"")
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK
    names = [p for p, _ in _rows(tmp_path)]
    assert names == ["2024/a.jpg", "2024/b.jpg"]


def test_unreadable_file_makes_adopt_register_nothing(make_config, tmp_path, monkeypatch, photos):
    """ハッシュを取れないファイルが 1 つでもあれば、何も登録せずに断る（直してからやり直せる）。

    一部だけ登録して確定すると、記録があるので adopt をやり直せず、登録し損ねた内容と同じファイルを
    あとの add が重複として止めずに取り込む。
    """
    from conftest import build_crawler_db
    import sqlite3
    crawl_db = str(tmp_path / "c.db")

    def _run(target, config, extra_excludes=()):
        scan_id = build_crawler_db(crawl_db, target, excludes=tuple(extra_excludes))
        conn = sqlite3.connect(crawl_db)
        conn.execute("UPDATE fs_files SET filehash = NULL, hash_algo = NULL WHERE name = 'b.jpg'")
        conn.commit(); conn.close()
        return crawler_client.CrawlerScan(db_path=crawl_db, scan_id=scan_id, status="completed", root_dir=target)

    monkeypatch.setattr(crawler_client, "resolve_crawler_repo", lambda path: path)
    monkeypatch.setattr(crawler_client, "run_crawler", _run)
    with pytest.raises(PreflightError, match="b.jpg"):
        main.run(make_config(mode=MODE_ADOPT, archive_root=photos))
    assert _rows(tmp_path) == []


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root は権限を無視して読める")
def test_unreadable_folder_makes_adopt_register_nothing(make_config, tmp_path, fake_crawler, photos):
    """読めないフォルダがあれば（crawler は黙って飛ばす）、走査の前に断り、何も登録しない。"""
    locked = os.path.join(photos, "2019")
    write_file(os.path.join(locked, "c.jpg"), b"CCCC")
    os.chmod(locked, 0)
    try:
        with pytest.raises(PreflightError, match="2019"):
            _adopt(make_config, photos, tmp_path, fake_crawler)
        assert _rows(tmp_path) == []
    finally:
        os.chmod(locked, 0o755)
    assert _adopt(make_config, photos, tmp_path, fake_crawler) == main.EXIT_OK  # 直せばやり直せる
    assert ("2019/c.jpg", STATUS_STORED) in _rows(tmp_path)


def test_nested_leftover_akasyx_is_not_counted_as_content(make_config, tmp_path):
    """途中の階層の .akasyx/（識別子の無い残り）も、adopt と同じく中身として数えない。"""
    root = str(tmp_path / "arch")
    write_file(os.path.join(root, "sub", ".akasyx", "tmp", "x.part"), b"PART")
    assert main.run(make_config(mode=MODE_REPORT, archive_root=root)) == main.EXIT_OK


def test_legacy_archive_is_migrated_not_adopted(make_config, tmp_path, fake_crawler):
    """v0.1.x の DB がある保存フォルダは移行で記録ができるので、adopt は要らない（断って verify を案内）。"""
    from test_archives import _make_legacy_db
    from database import legacy_db_path
    root = str(tmp_path / "legacy")
    write_file(os.path.join(root, "inbox", "a.txt"), b"abc")
    _make_legacy_db(legacy_db_path(root), root)
    assert main.run(make_config(mode=MODE_REPORT, archive_root=root)) == main.EXIT_OK
    with pytest.raises(PreflightError, match="verify"):
        _adopt(make_config, root, tmp_path, fake_crawler)


def test_adopt_cli_is_available(tmp_path, photos):
    from config import parse_arguments
    cfg = parse_arguments(["adopt", photos])
    assert cfg.mode == MODE_ADOPT and cfg.archive_root == os.path.abspath(photos)
    shutil.rmtree(str(tmp_path / "photos"))


def test_folder_with_only_empty_files_needs_no_adopt(make_config, tmp_path, fake_crawler):
    """adopt が登録するもの（1 バイト以上の実ファイル）が無いフォルダは、adopt なしで使える。

    「中身あり」の判定を adopt の登録条件とそろえる。ずれると、adopt しても何も登録されず断られ続ける。
    """
    root = str(tmp_path / "only_empty")
    write_file(os.path.join(root, "empty.txt"), b"")
    write_file(os.path.join(root, ".DS_Store"), b"junk")
    assert main.run(make_config(mode=MODE_REPORT, archive_root=root)) == main.EXIT_OK


def test_verify_after_adopt_keeps_archive_usable(make_config, tmp_path, fake_crawler):
    """中身が全部他の保存フォルダの重複で adopt した後、verify を挟んでも使える（adopt 待ちの印は外れている）。"""
    a = str(tmp_path / "a")
    os.makedirs(a)
    src = str(tmp_path / "inbox")
    write_file(os.path.join(src, "a.jpg"), b"AAAA")
    fake_crawler(src, str(tmp_path / "c.db"))
    assert main.run(make_config(archive_root=a, source_path=src)) == main.EXIT_OK
    b = str(tmp_path / "b")
    write_file(os.path.join(b, "x", "a.jpg"), b"AAAA")
    assert _adopt(make_config, b, tmp_path, fake_crawler) == main.EXIT_OK
    fake_crawler(b, str(tmp_path / "v.db"))
    assert main.run(make_config(mode=MODE_VERIFY, archive_root=b)) == main.EXIT_OK
    assert main.run(make_config(mode=MODE_REPORT, archive_root=b)) == main.EXIT_OK


def test_failed_count_is_not_doubled():
    """実行記録の failed は、失敗した件数そのもの（RESULT_FAILED と "failed" は同じ文字列。2 倍にしない）。"""
    from models import RESULT_FAILED, Ingest
    record = Ingest()
    main._apply_counters(record, {RESULT_FAILED: 1})
    assert record.failed == 1


def test_git_folder_is_not_counted_as_content(make_config, tmp_path):
    """crawler は既定で .git/ を走査しない（adopt でも登録されない）ので、中身として数えない。"""
    root = str(tmp_path / "repo_like")
    write_file(os.path.join(root, ".git", "objects", "ab", "cdef"), b"blob")
    write_file(os.path.join(root, "sub", ".git", "HEAD"), b"ref")
    assert main.run(make_config(mode=MODE_REPORT, archive_root=root)) == main.EXIT_OK


def test_folder_with_only_non_owning_records_still_needs_adopt(make_config, tmp_path, fake_crawler):
    """消した保存フォルダを forget した跡に写真フォルダを置いた場合（missing の行だけ残る）も adopt を求める。"""
    old = str(tmp_path / "arch")
    src0 = str(tmp_path / "in0")
    write_file(os.path.join(src0, "seed.jpg"), b"SEED")
    fake_crawler(src0, str(tmp_path / "c0.db"))
    os.makedirs(old)
    assert main.run(make_config(archive_root=old, source_path=src0)) == main.EXIT_OK
    sess, engine = _db(tmp_path)
    try:
        aid = sess.query(Archive).one().id
    finally:
        sess.close(); engine.dispose()
    shutil.rmtree(old)
    argv = ["archives", "--forget", str(aid), "--archive-db", archive_db_path(tmp_path)]
    assert main.main(argv) == main.EXIT_OK
    write_file(os.path.join(old, "2020", "p.jpg"), b"PHOTO")  # 同じパスに識別子の無い写真フォルダ
    src = str(tmp_path / "inbox")
    write_file(os.path.join(src, "p_copy.jpg"), b"PHOTO")
    fake_crawler(src, str(tmp_path / "c1.db"))
    with pytest.raises(PreflightError, match="adopt"):
        main.run(make_config(archive_root=old, source_path=src))
    assert _adopt(make_config, old, tmp_path, fake_crawler) == main.EXIT_OK
    assert ("2020/p.jpg", STATUS_STORED) in _rows(tmp_path)


def test_adopt_reuses_unregistered_rows_at_same_path(make_config, tmp_path, fake_crawler):
    """verify が未登録として登録した行しか無い保存フォルダも adopt でき、同じパスの行を使い回す。"""
    arch = str(tmp_path / "arch")
    os.makedirs(arch)
    assert main.run(make_config(mode=MODE_REPORT, archive_root=arch)) == main.EXIT_OK  # 空で登録
    sess, engine = _db(tmp_path)
    try:
        aid = sess.query(Archive).one().id
        f = write_file(os.path.join(arch, "x.jpg"), b"XXXX")
        from utl import hashing
        sess.add(ArchiveFile(archive_id=aid, filehash=hashing.file_hash(f), hash_algo="sha256", size=4,
                             name="x.jpg", stored_path_rel="x.jpg", status=STATUS_UNREGISTERED))
        sess.commit()
    finally:
        sess.close(); engine.dispose()
    assert _adopt(make_config, arch, tmp_path, fake_crawler) == main.EXIT_OK
    assert _rows(tmp_path) == [("x.jpg", STATUS_STORED)]  # 行は 1 つのまま


def test_adopt_uses_a_fresh_crawler_db(make_config, tmp_path, monkeypatch, photos):
    """前回の走査の「ハッシュの取り直しをやめた」記録に引きずられないよう、使い捨ての作業用 DB で走査する。"""
    from conftest import build_crawler_db
    seen = []

    def _run(target, config, extra_excludes=()):
        seen.append(config.db_dir)
        db = os.path.join(config.db_dir, "file_inventory.db")
        scan_id = build_crawler_db(db, target, excludes=tuple(extra_excludes))
        return crawler_client.CrawlerScan(db_path=db, scan_id=scan_id, status="completed", root_dir=target)

    monkeypatch.setattr(crawler_client, "resolve_crawler_repo", lambda path: path)
    monkeypatch.setattr(crawler_client, "run_crawler", _run)
    cfg = make_config(mode=MODE_ADOPT, archive_root=photos)
    assert main.run(cfg) == main.EXIT_OK
    assert seen and seen[0] != cfg.db_dir
    assert not os.path.exists(seen[0])  # 終わったら消す


def test_folder_of_only_duplicates_elsewhere_is_usable_after_adopt(make_config, tmp_path, fake_crawler):
    """中身がすべて別の保存フォルダの重複でも（adopt で stored が 1 件もできない）、adopt の後は使える。"""
    a = str(tmp_path / "a")
    os.makedirs(a)
    src = str(tmp_path / "inbox")
    write_file(os.path.join(src, "a.jpg"), b"AAAA")
    fake_crawler(src, str(tmp_path / "c.db"))
    assert main.run(make_config(archive_root=a, source_path=src)) == main.EXIT_OK
    b = str(tmp_path / "b")
    write_file(os.path.join(b, "x", "a.jpg"), b"AAAA")
    write_file(os.path.join(b, "y", "a2.jpg"), b"AAAA")
    assert _adopt(make_config, b, tmp_path, fake_crawler) == main.EXIT_OK
    assert main.run(make_config(mode=MODE_REPORT, archive_root=b)) == main.EXIT_OK
    sess, engine = _db(tmp_path)
    try:
        # 組の最初（x/a.jpg）も別の保存フォルダの重複なので、2 つ目の説明は本当の保存先（a）を指す
        item = sess.query(IngestItem).filter_by(name="a2.jpg").one()
        assert a in item.message
    finally:
        sess.close(); engine.dispose()


def test_adopt_on_unregistered_legacy_folder_is_refused_before_registering(make_config, tmp_path, fake_crawler):
    """v0.1.x の DB が残る未登録フォルダへの adopt は、登録も adopt 待ちの印も作らずに断る（開けば移行される）。"""
    from test_archives import _make_legacy_db
    from database import legacy_db_path
    root = str(tmp_path / "legacy")
    write_file(os.path.join(root, "inbox", "a.txt"), b"abc")
    _make_legacy_db(legacy_db_path(root), root)
    with pytest.raises(PreflightError, match="v0.1"):
        _adopt(make_config, root, tmp_path, fake_crawler)
    sess, engine = _db(tmp_path)
    try:
        assert sess.query(Archive).count() == 0
    finally:
        sess.close(); engine.dispose()
    assert main.run(make_config(mode=MODE_REPORT, archive_root=root)) == main.EXIT_OK  # 移行して使える


def test_adopt_clears_quarantine_flag_on_rows_it_stores(make_config, tmp_path, fake_crawler):
    """verify が付けた隔離予定の印は、adopt が stored にした行からは外す（正本を隔離対象にしない）。"""
    from models import DISPOSITION_QUARANTINE
    arch = str(tmp_path / "arch")
    os.makedirs(arch)
    assert main.run(make_config(mode=MODE_REPORT, archive_root=arch)) == main.EXIT_OK
    write_file(os.path.join(arch, "x.jpg"), b"XXXX")
    write_file(os.path.join(arch, "dup", "x.jpg"), b"XXXX")
    fake_crawler(arch, str(tmp_path / "v.db"))
    cfg = make_config(mode=MODE_VERIFY, archive_root=arch, flag_quarantine=["unregistered", "archive_duplicate"])
    assert main.run(cfg) == main.EXIT_OK
    assert _adopt(make_config, arch, tmp_path, fake_crawler) == main.EXIT_OK
    sess, engine = _db(tmp_path)
    try:
        rows = {r.stored_path_rel: r for r in sess.query(ArchiveFile)}
        assert rows["dup/x.jpg"].status == STATUS_STORED  # パス順で先
        assert rows["dup/x.jpg"].disposition is None
        assert rows["x.jpg"].status == STATUS_UNREGISTERED
        assert rows["x.jpg"].disposition == DISPOSITION_QUARANTINE  # 重複の方は印を残す
    finally:
        sess.close(); engine.dispose()
