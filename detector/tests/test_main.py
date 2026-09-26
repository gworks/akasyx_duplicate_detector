# test_main.py - 事前チェック・終了コード・end-to-end（設計書 §11 / §12）
import os
import sys
from datetime import datetime

import ingest as ingest_module
import main
import pytest
from conftest import archive_db_path, build_crawler_db, write_file
from crawler_client import CrawlerScan
from errors import PreflightError
from models import (
    MODE_DELETE_DUPLICATES,
    MODE_REPORT,
    MODE_VERIFY,
    STATUS_STORED,
    Archive,
    ArchiveFile,
    Ingest,
    IngestItem,
)


_OPEN_DB_PATH = [None]


def _run(config):
    _OPEN_DB_PATH[0] = config.archive_db
    return main.run(config)


# --- 事前チェック（§12）------------------------------------------------------


def test_rejects_archive_inside_source(make_config, tmp_path):
    source = tmp_path / "inbox"
    (source / "archive").mkdir(parents=True)
    config = make_config(
        archive_root=str(source / "archive"), source_path=str(source)
    )
    with pytest.raises(PreflightError, match="nested"):
        main.preflight(config)


def test_rejects_source_inside_archive(make_config, archive):
    inner = os.path.join(archive, "inbox")
    os.makedirs(inner)
    config = make_config(archive_root=archive, source_path=inner)
    with pytest.raises(PreflightError, match="nested"):
        main.preflight(config)


def test_rejects_same_path(make_config, archive):
    config = make_config(archive_root=archive, source_path=archive)
    with pytest.raises(PreflightError, match="nested"):
        main.preflight(config)


def test_rejects_missing_archive(make_config, tmp_path):
    config = make_config(archive_root=str(tmp_path / "nope"))
    with pytest.raises(PreflightError, match="Archive folder not found"):
        main.preflight(config)


def test_rejects_missing_source(make_config, archive, tmp_path):
    config = make_config(archive_root=archive, source_path=str(tmp_path / "nope"))
    with pytest.raises(PreflightError, match="Source not found"):
        main.preflight(config)


def test_rejects_missing_crawler_repo(make_config, archive, source, tmp_path):
    """crawler が無ければ add / verify は実行前に断る（設計書 §12）。"""
    write_file(os.path.join(source, "a.txt"), b"hello")
    config = make_config(
        archive_root=archive,
        source_path=source,
        crawler_repo=str(tmp_path / "absent"),
    )
    with pytest.raises(PreflightError, match="akasyx_crawler not found"):
        main.preflight(config)


def test_single_file_source_does_not_need_crawler_repo(make_config, archive, tmp_path):
    """単一ファイルは crawler を使わないので、crawler 不在でも通る。"""
    path = write_file(str(tmp_path / "solo" / "x.txt"), b"solo")
    config = make_config(
        archive_root=archive, source_path=path, crawler_repo=str(tmp_path / "absent")
    )
    main.preflight(config)  # 例外にならない


def test_rejects_incomplete_crawler_scan(
    make_config, archive, source, tmp_path, fake_crawler
):
    """不完全な走査では取り込まない（設計書 §3）。"""
    write_file(os.path.join(source, "a.txt"), b"hello")
    fake_crawler(source, str(tmp_path / "crawler.db"), scan_status="interrupted")
    code = main.main(
        [
            "add", archive, source,
            "--db-dir", str(tmp_path / "db"),
            "--log-dir", str(tmp_path / "log"),
            "--archive-db", archive_db_path(tmp_path),
        ]
    )
    assert code == main.EXIT_REJECTED
    assert os.path.exists(os.path.join(source, "a.txt"))  # 1件も動いていない


# --- end-to-end（add）--------------------------------------------------------


def test_add_moves_new_and_keeps_duplicates(
    make_config, archive, source, tmp_path, fake_crawler
):
    write_file(os.path.join(source, "a.txt"), b"AAA")
    write_file(os.path.join(source, "sub", "b.txt"), b"BBB")
    write_file(os.path.join(source, "sub", "copy_of_a.txt"), b"AAA")  # 内容重複
    write_file(os.path.join(source, "empty.txt"), b"")               # 0バイト

    fake_crawler(source, str(tmp_path / "crawler.db"))
    config = make_config(archive_root=archive, source_path=source)
    assert _run(config) == main.EXIT_OK

    # 新規2件が保存フォルダ（今月の年月フォルダ / 投入元名 / 相対パス）へ、重複と0バイトは投入元に残る
    month = f"{datetime.now():%Y-%m}"
    name = os.path.basename(source)
    assert os.path.exists(os.path.join(archive, month, name, "a.txt"))
    assert os.path.exists(os.path.join(archive, month, name, "sub", "b.txt"))
    assert os.path.exists(os.path.join(source, "sub", "copy_of_a.txt"))
    assert os.path.exists(os.path.join(source, "empty.txt"))

    sess, engine = _open(archive)
    try:
        stored = sess.query(ArchiveFile).filter_by(status=STATUS_STORED).all()
        assert {r.name for r in stored} == {"a.txt", "b.txt"}
        results = {i.name: i.result for i in sess.query(IngestItem).all()}
        assert results["copy_of_a.txt"] == "duplicate"
        assert results["empty.txt"] == "skipped_empty"
    finally:
        sess.close()
        engine.dispose()


def test_add_is_idempotent(make_config, archive, source, tmp_path, fake_crawler):
    """同じ内容を再投入しても2重に保存されない。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "crawler1.db"))
    _run(make_config(archive_root=archive, source_path=source))

    write_file(os.path.join(source, "a.txt"), b"AAA")  # 同じ内容をもう一度置く
    fake_crawler(source, str(tmp_path / "crawler2.db"))
    _run(make_config(archive_root=archive, source_path=source))

    sess, engine = _open(archive)
    try:
        assert sess.query(ArchiveFile).filter_by(status=STATUS_STORED).count() == 1
    finally:
        sess.close()
        engine.dispose()
    assert os.path.exists(os.path.join(source, "a.txt"))  # 2回目は残る


def test_dry_run_changes_nothing(make_config, archive, source, tmp_path, fake_crawler):
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "crawler.db"))
    config = make_config(archive_root=archive, source_path=source, dry_run=True)
    assert _run(config) == main.EXIT_OK

    assert os.path.exists(os.path.join(source, "a.txt"))
    sess, engine = _open(archive)
    try:
        assert sess.query(ArchiveFile).count() == 0
        assert sess.query(IngestItem).count() == 0
        assert sess.query(Ingest).one().dry_run == 1
    finally:
        sess.close()
        engine.dispose()


def test_single_file_source_needs_no_crawler(make_config, archive, tmp_path):
    path = write_file(str(tmp_path / "solo" / "x.txt"), b"solo")
    config = make_config(archive_root=archive, source_path=path)
    assert _run(config) == main.EXIT_OK
    # 単一ファイルは投入元フォルダ名を付けず、年月フォルダ直下
    assert os.path.exists(os.path.join(archive, f"{datetime.now():%Y-%m}", "x.txt"))


def test_add_keeps_source_structure_under_month(
    make_config, archive, source, tmp_path, fake_crawler
):
    """保存先は <YYYY-MM>/<投入元名>/<相対パス>。投入元の階層は年月フォルダの中に再現される。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")
    write_file(os.path.join(source, "deep", "nested", "b.txt"), b"BBB")
    fake_crawler(source, str(tmp_path / "crawler.db"))
    config = make_config(archive_root=archive, source_path=source)
    assert _run(config) == main.EXIT_OK

    month = f"{datetime.now():%Y-%m}"
    name = os.path.basename(source)
    assert os.path.exists(os.path.join(archive, month, name, "a.txt"))
    assert os.path.exists(os.path.join(archive, month, name, "deep", "nested", "b.txt"))


def test_add_skips_own_data_inside_source(make_config, archive, source, tmp_path, fake_crawler):
    """正本 DB・作業用 DB・ログが投入元の中にあっても取り込まない（ホームを投入元にした配布版）。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")
    data = os.path.join(source, "appdata")
    db = os.path.join(data, "archive.db")
    write_file(os.path.join(data, "db", "crawler.db"), b"crawler")
    write_file(os.path.join(data, "log", "old.log"), b"log")
    fake_crawler(source, str(tmp_path / "crawler.db"))
    config = make_config(
        archive_root=archive, source_path=source, archive_db=db,
        db_dir=os.path.join(data, "db"), log_dir=os.path.join(data, "log"),
    )
    assert _run(config) == main.EXIT_OK

    assert os.path.exists(db)  # 処理中の正本 DB は動かさない
    assert os.path.exists(os.path.join(data, "db", "crawler.db"))
    assert os.path.exists(os.path.join(data, "log", "old.log"))
    stored = [n for _, _, ns in os.walk(archive) for n in ns if n != "archive.id"]
    assert stored == ["a.txt"]


def test_add_skips_whole_data_folder_inside_source(
    make_config, archive, source, tmp_path, fake_crawler, monkeypatch
):
    """配布版のデータフォルダ全体（UI の設定・Electron のプロファイル ui/ も）を取り込まない。"""
    home = os.path.join(source, "Library", "akasyx-duplicate-detector")
    monkeypatch.setenv("AKASYX_PACKAGED", "1")
    monkeypatch.setenv("AKASYX_DETECTOR_HOME", home)
    write_file(os.path.join(source, "a.txt"), b"AAA")
    write_file(os.path.join(home, "ui", "settings.json"), b"{}")
    write_file(os.path.join(home, "ui", "Local Storage", "leveldb", "000003.log"), b"x")
    fake_crawler(source, str(tmp_path / "crawler.db"))
    assert _run(make_config(archive_root=archive, source_path=source)) == main.EXIT_OK

    assert os.path.exists(os.path.join(home, "ui", "settings.json"))
    assert os.path.exists(os.path.join(home, "ui", "Local Storage", "leveldb", "000003.log"))
    stored = [n for _, _, ns in os.walk(archive) for n in ns if n != "archive.id"]
    assert stored == ["a.txt"]


@pytest.mark.parametrize("key", ["db_dir", "log_dir"])
def test_rejects_work_data_inside_archive(make_config, archive, source, key):
    """作業用 DB・ログも保存フォルダの中には置かせない（verify が実体として拾うため）。"""
    config = make_config(archive_root=archive, source_path=source, **{key: os.path.join(archive, key)})
    with pytest.raises(PreflightError, match="inside the archive folder"):
        main.preflight(config)


_NO_CHMOD = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="権限で読めない状態を作れない環境",
)


@_NO_CHMOD
def test_rejects_unknown_archive_whose_content_is_unreadable(
    make_config, archive, source, tmp_path, fake_crawler
):
    """読めない配下しかない保存フォルダを「空」とみなして新規登録しない。"""
    other_db = _seed_with_db_a(make_config, archive, source, tmp_path, fake_crawler)
    month_dirs = [d for d in os.listdir(archive) if d != ".akasyx"]
    locked = os.path.join(archive, month_dirs[0])
    os.chmod(locked, 0)
    try:
        with pytest.raises(PreflightError, match="Cannot read part of the archive folder"):
            _run(make_config(mode=MODE_REPORT, archive_root=archive, archive_db=other_db))
    finally:
        os.chmod(locked, 0o755)


@_NO_CHMOD
def test_rejects_unreadable_archive_id(make_config, archive, source, tmp_path, fake_crawler):
    """archive.id が読めないのを「無い」とみなして登録し直さない。"""
    _seed_with_db_a(make_config, archive, source, tmp_path, fake_crawler)
    id_path = os.path.join(archive, ".akasyx", "archive.id")
    os.chmod(id_path, 0)
    try:
        with pytest.raises(PreflightError, match="Cannot read the archive folder ID"):
            _run(make_config(mode=MODE_REPORT, archive_root=archive))
    finally:
        os.chmod(id_path, 0o644)


def test_own_data_skips_are_reported(make_config, archive, source, tmp_path, fake_crawler, capsys):
    """除外した自データはサマリと CSV に残す（黙って件数を減らさない）。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")
    data = os.path.join(source, "appdata")
    write_file(os.path.join(data, "log", "old.log"), b"log")
    fake_crawler(source, str(tmp_path / "crawler.db"))
    config = make_config(
        archive_root=archive, source_path=source, archive_db=os.path.join(data, "archive.db"),
        db_dir=os.path.join(data, "db"), log_dir=os.path.join(data, "log"),
    )
    assert _run(config) == main.EXIT_OK
    out = capsys.readouterr().out
    assert "own data skipped: " in out
    csv_path = next(
        os.path.join(config.log_dir, n) for n in os.listdir(config.log_dir) if n.startswith("add_result")
    )
    with open(csv_path, encoding="utf-8") as f:
        assert "skipped_own_data" in f.read()


def test_own_data_is_skipped_when_source_is_given_via_symlink(
    make_config, archive, tmp_path, fake_crawler
):
    """投入元をシンボリックリンク経由で指定しても、実体の位置で指定した自データを取り込まない。"""
    real_src = tmp_path / "real_inbox"
    write_file(str(real_src / "a.txt"), b"AAA")
    data = real_src / "appdata"
    write_file(str(data / "log" / "old.log"), b"log")
    link = tmp_path / "inbox_link"
    os.symlink(real_src, link)
    fake_crawler(str(link), str(tmp_path / "crawler.db"))
    config = make_config(
        archive_root=archive, source_path=str(link), archive_db=str(data / "archive.db"),
        db_dir=str(data / "db"), log_dir=str(data / "log"),
    )
    assert _run(config) == main.EXIT_OK
    assert os.path.exists(data / "log" / "old.log")


def test_own_data_behind_symlink_inside_source_is_skipped_with_follow_symlinks(
    make_config, archive, source, tmp_path, fake_crawler
):
    """--follow-symlinks で、投入元の中の symlink 越しに見える自データも取り込まない。"""
    data = tmp_path / "appdata"
    write_file(str(data / "log" / "old.log"), b"log")
    write_file(os.path.join(source, "a.txt"), b"AAA")
    os.symlink(data, os.path.join(source, "dd"))
    config = make_config(
        archive_root=archive, source_path=source, archive_db=str(data / "archive.db"),
        db_dir=str(data / "db"), log_dir=str(data / "log"), follow_symlinks=True,
    )
    is_own = ingest_module._own_data_matcher(config)
    assert is_own(os.path.join(source, "dd", "log", "old.log"))
    assert is_own(os.path.join(source, "dd", "archive.db-wal"))
    assert not is_own(os.path.join(source, "a.txt"))


def test_own_data_skip_is_recorded_as_ingest_item(
    make_config, archive, source, tmp_path, fake_crawler
):
    """除外も他の結果と同じく ar_ingest_items に残す（ar_ingests の件数と揃う）。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")
    data = os.path.join(source, "appdata")
    write_file(os.path.join(data, "log", "old.log"), b"log")
    fake_crawler(source, str(tmp_path / "crawler.db"))
    config = make_config(
        archive_root=archive, source_path=source, archive_db=os.path.join(data, "archive.db"),
        db_dir=os.path.join(data, "db"), log_dir=os.path.join(data, "log"),
    )
    assert _run(config) == main.EXIT_OK
    sess, engine = _open(archive)
    try:
        record = sess.query(Ingest).filter(Ingest.mode == "add").one()
        items = sess.query(IngestItem).filter(IngestItem.ingest_id == record.id).all()
        assert record.total == len(items)
        assert any(i.result == "skipped_own_data" for i in items)
    finally:
        sess.close()
        engine.dispose()


def test_data_root_inside_archive_is_fine_when_nothing_is_written_there(
    make_config, archive, source, tmp_path, fake_crawler, monkeypatch
):
    """データフォルダが保存フォルダの中でも、正本 DB・作業用 DB・ログを外に出していれば断らない。"""
    fake_crawler(source, str(tmp_path / "crawler.db"))
    monkeypatch.setenv("AKASYX_PACKAGED", "1")
    monkeypatch.setenv("AKASYX_DETECTOR_HOME", os.path.join(archive, "appdata"))
    config = make_config(archive_root=archive, source_path=source)  # DB 類は tmp/dist 側
    main.preflight(config)


@pytest.mark.skipif(sys.platform not in ("darwin", "win32"), reason="大文字小文字を区別しない OS のみ")
def test_master_db_inside_archive_detected_despite_case(make_config, archive, source):
    """大文字小文字だけ違う表記でも、保存フォルダの中の正本 DB を見逃さない。"""
    swapped = archive.swapcase()
    config = make_config(
        archive_root=archive, source_path=source,
        archive_db=os.path.join(swapped, ".akasyx", "archive.db"),
    )
    with pytest.raises(PreflightError, match="inside the archive folder"):
        main.preflight(config)


def test_rejects_undecodable_archive_id(make_config, archive, source, tmp_path, fake_crawler):
    """文字化けした archive.id は異常終了せず、断る。"""
    _seed_with_db_a(make_config, archive, source, tmp_path, fake_crawler)
    with open(os.path.join(archive, ".akasyx", "archive.id"), "wb") as f:
        f.write(b"\xff\xfe\x00garbage")
    with pytest.raises(PreflightError, match="Cannot read the archive folder ID"):
        _run(make_config(mode=MODE_REPORT, archive_root=archive))


@_NO_CHMOD
def test_move_is_followed_even_if_old_location_is_unreadable(
    make_config, archive, source, tmp_path, fake_crawler
):
    """移動元に同名フォルダが残っていて archive.id が読めなくても、移動として扱う。"""
    import shutil

    _seed_with_db_a(make_config, archive, source, tmp_path, fake_crawler)
    moved = str(tmp_path / "archive_moved")
    shutil.copytree(archive, moved)
    id_path = os.path.join(archive, ".akasyx", "archive.id")
    os.chmod(id_path, 0)
    try:
        assert _run(make_config(mode=MODE_REPORT, archive_root=moved)) == main.EXIT_OK
    finally:
        os.chmod(id_path, 0o644)


def _seed_with_db_a(make_config, archive, source, tmp_path, fake_crawler):
    """正本 DB A で 1 件保存した保存フォルダを作り、別の正本 DB B のパスを返す。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "c1.db"))
    assert _run(make_config(archive_root=archive, source_path=source)) == main.EXIT_OK
    return str(tmp_path / "other_dist" / "archive.db")


def test_add_rejects_archive_unknown_to_master_db(
    make_config, archive, source, tmp_path, fake_crawler
):
    """別の正本 DB で使っていた保存フォルダ（uid が DB に無く中身がある）への add は断る。
    空の登録として続けると、既にある内容と同じファイルまで取り込んで重複を作るため。"""
    other_db = _seed_with_db_a(make_config, archive, source, tmp_path, fake_crawler)
    write_file(os.path.join(source, "again.txt"), b"AAA")  # 保存済みと同じ内容
    fake_crawler(source, str(tmp_path / "c2.db"))
    config = make_config(archive_root=archive, source_path=source, archive_db=other_db)
    with pytest.raises(PreflightError, match="not registered in the master DB"):
        _run(config)

    assert os.path.exists(os.path.join(source, "again.txt"))  # 何も動かしていない
    sess, engine = _open(archive)
    try:
        assert sess.query(Archive).count() == 0  # 新しい登録も作らない
    finally:
        sess.close()
        engine.dispose()


@pytest.mark.parametrize("mode", [MODE_VERIFY, MODE_REPORT, MODE_DELETE_DUPLICATES])
def test_other_commands_also_reject_archive_unknown_to_master_db(
    make_config, archive, source, tmp_path, fake_crawler, mode
):
    """add 以外も断り、登録を作らない。作ると次の add が「登録済み」として素通りし重複を取り込む。"""
    other_db = _seed_with_db_a(make_config, archive, source, tmp_path, fake_crawler)
    fake_crawler(archive, str(tmp_path / "v.db"))
    with pytest.raises(PreflightError, match="not registered in the master DB"):
        _run(make_config(mode=mode, archive_root=archive, archive_db=other_db))

    # その後の add も断られる（先に別コマンドを流して拒否を迂回できない）
    write_file(os.path.join(source, "again.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "c2.db"))
    with pytest.raises(PreflightError, match="not registered in the master DB"):
        _run(make_config(archive_root=archive, source_path=source, archive_db=other_db))
    assert os.path.exists(os.path.join(source, "again.txt"))


def test_rejects_master_db_inside_archive(make_config, archive, source, tmp_path, fake_crawler):
    """正本 DB を保存フォルダの中（.akasyx/archive.db 等）に置くと断る。旧 DB として改名されるため。"""
    for db in (
        os.path.join(archive, ".akasyx", "archive.db"),
        os.path.join(archive, "db", "archive.db"),
    ):
        config = make_config(archive_root=archive, source_path=source, archive_db=db)
        with pytest.raises(PreflightError, match="master DB is inside the archive folder"):
            main.preflight(config)


def test_rejects_copied_archive_while_original_exists(
    make_config, archive, source, tmp_path, fake_crawler
):
    """archive.id ごとコピーした保存フォルダは、元が残っていれば移動とみなさず断る。"""
    import shutil

    _seed_with_db_a(make_config, archive, source, tmp_path, fake_crawler)
    copy = str(tmp_path / "archive_copy")
    shutil.copytree(archive, copy)
    with pytest.raises(PreflightError, match="copy of another archive folder"):
        _run(make_config(mode=MODE_REPORT, archive_root=copy))

    sess, engine = _open(archive)
    try:
        assert sess.query(Archive).one().root_abs == os.path.abspath(archive)  # 書き換えない
    finally:
        sess.close()
        engine.dispose()


def test_moved_archive_is_followed_when_original_is_gone(
    make_config, archive, source, tmp_path, fake_crawler
):
    """元の場所に無ければ移動として扱い、登録上のパスを更新する。"""
    _seed_with_db_a(make_config, archive, source, tmp_path, fake_crawler)
    moved = str(tmp_path / "archive_moved")
    os.rename(archive, moved)
    assert _run(make_config(mode=MODE_REPORT, archive_root=moved)) == main.EXIT_OK

    sess, engine = _open(moved)
    try:
        assert sess.query(Archive).one().root_abs == os.path.abspath(moved)
    finally:
        sess.close()
        engine.dispose()


def test_add_allows_empty_archive_unknown_to_master_db(
    make_config, archive, source, tmp_path, fake_crawler
):
    """識別子だけ残った空の保存フォルダは、重複の心配が無いので新規登録して続ける。"""
    other_db = _seed_with_db_a(make_config, archive, source, tmp_path, fake_crawler)
    for dirpath, _, names in os.walk(archive):
        if ".akasyx" not in dirpath:
            for n in names:
                os.unlink(os.path.join(dirpath, n))
    write_file(os.path.join(source, "b.txt"), b"BBB")
    fake_crawler(source, str(tmp_path / "c2.db"))
    config = make_config(archive_root=archive, source_path=source, archive_db=other_db)
    assert _run(config) == main.EXIT_OK


def test_add_ignores_os_junk_and_prunes_it(
    make_config, archive, source, tmp_path, fake_crawler
):
    """.DS_Store は取り込まず、--prune-empty-dirs で .DS_Store だけ残ったフォルダも消える。"""
    write_file(os.path.join(source, "sub", "a.txt"), b"AAA")
    write_file(os.path.join(source, "sub", ".DS_Store"), b"junk")
    write_file(os.path.join(source, ".DS_Store"), b"junk")
    fake_crawler(source, str(tmp_path / "crawler.db"))
    config = make_config(archive_root=archive, source_path=source, prune_empty_dirs=True)
    assert _run(config) == main.EXIT_OK

    sess, engine = _open(archive)
    try:
        assert {r.name for r in sess.query(ArchiveFile).all()} == {"a.txt"}
    finally:
        sess.close()
        engine.dispose()
    assert not os.path.exists(os.path.join(source, "sub"))   # .DS_Store だけになった sub は消える
    assert os.path.isdir(source)                              # 投入元そのものは残す


def test_failed_item_returns_exit_code_2(
    make_config, archive, source, tmp_path, fake_crawler, monkeypatch
):
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "crawler.db"))

    import mover

    monkeypatch.setattr(
        mover, "safe_move", lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    )
    config = make_config(archive_root=archive, source_path=source)
    assert _run(config) == main.EXIT_HAS_FAILURES


def test_lock_prevents_concurrent_run(make_config, archive, source):
    from database import archive_lock

    config = make_config(archive_root=archive, source_path=source)
    with archive_lock(archive):
        with pytest.raises(PreflightError, match="in use by another process"):
            with archive_lock(archive):
                pass  # pragma: no cover


def test_cli_smoke(archive, source, tmp_path, monkeypatch, fake_crawler):
    """argparse からの経路（main.main）が通ること。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "crawler.db"))
    code = main.main(
        [
            "add", archive, source,
            "--db-dir", str(tmp_path / "db"),
            "--log-dir", str(tmp_path / "log"),
            "--archive-db", archive_db_path(tmp_path),
        ]
    )
    assert code == main.EXIT_OK


def _open(_archive_root_unused):
    """正本 DB を開く（引数は旧 API 互換のため残す。DB は保存フォルダの外にある）。"""
    from database import get_session

    return get_session(_OPEN_DB_PATH[0])


def test_build_crawler_db_roundtrip(source, tmp_path):
    """テスト用の crawler DB が read_files で読めること（フィクスチャ自身の検証）。"""
    import crawler_client

    write_file(os.path.join(source, "a.txt"), b"AAA")
    db = str(tmp_path / "c.db")
    scan_id = build_crawler_db(db, source)
    files = list(crawler_client.read_files(db, scan_id))
    assert [f.path_rel for f in files] == ["a.txt"]
    assert files[0].hash_algo == "sha256"
    assert isinstance(
        crawler_client.CrawlerScan(db, scan_id, "completed", source), CrawlerScan
    )
