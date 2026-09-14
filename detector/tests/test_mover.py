# test_mover.py - 衝突回避・3フェーズ移動・クラッシュ復旧（設計書 §7.2 〜 §7.4）
import os
from datetime import datetime, timezone

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


def test_pick_free_name_keeps_parent(session, archive):
    rel = mover.pick_free_name(session, archive, "写真2024/旅行", "IMG_0001.jpg")
    assert rel == "写真2024/旅行/IMG_0001.jpg"


def test_pick_free_name_avoids_existing_file(session, archive):
    write_file(os.path.join(archive, "sub", "a.txt"), b"x")
    assert mover.pick_free_name(session, archive, "sub", "a.txt") == "sub/a (2).txt"


def test_pick_free_name_avoids_db_reservation(session, archive):
    session.add(
        ArchiveFile(
            archive_id=1,
            filehash="a" * 64, hash_algo="sha256", size=1, name="a.txt",
            stored_path_rel="a.txt", status=STATUS_PENDING,
        )
    )
    session.commit()
    assert mover.pick_free_name(session, archive, "", "a.txt", archive_id=1) == "a (2).txt"


def test_pick_free_name_is_case_insensitive(session, archive):
    """大文字小文字を区別しない FS でも衝突しないよう casefold して判定する。"""
    session.add(
        ArchiveFile(
            archive_id=1,
            filehash="a" * 64, hash_algo="sha256", size=1, name="A.TXT",
            stored_path_rel="A.TXT", status=STATUS_STORED,
        )
    )
    session.commit()
    assert mover.pick_free_name(session, archive, "", "a.txt", archive_id=1) == "a (2).txt"


def test_pick_free_name_honours_extra_taken(session, archive):
    taken = {"a.txt", "a (2).txt"}
    rel = mover.pick_free_name(session, archive, "", "a.txt", extra_taken=taken)
    assert rel == "a (3).txt"


def test_pick_free_name_gives_up_eventually(session, archive, monkeypatch):
    monkeypatch.setattr(mover, "MAX_COLLISION_SUFFIX", 2)
    write_file(os.path.join(archive, "a.txt"), b"x")
    write_file(os.path.join(archive, "a (2).txt"), b"y")
    with pytest.raises(DetectorError):
        mover.pick_free_name(session, archive, "", "a.txt")


# --- 保存先レイアウト（年月 / 投入元の階層 / 件数上限の枝）------------------------


def _scanned_at(path, created):
    s = _scanned(path)
    s.created_at = created
    return s


def _fill(archive, rel_dir, n):
    """rel_dir 直下に n 個のダミーファイルを置く（人が手で入れた状態を模す）。"""
    for i in range(n):
        write_file(os.path.join(archive, rel_dir, f"filler_{i:04d}.bin"), b"x")


def test_month_dir_uses_local_time_of_created_at(tmp_path):
    """crawler の created_at は UTC naive。ローカル時刻に直してから年月を取る。"""
    src = write_file(str(tmp_path / "a.txt"), b"x")
    utc_naive = datetime(2026, 8, 31, 20, 0, 0)  # UTC 20:00 = JST 翌日 05:00
    expected = f"{utc_naive.replace(tzinfo=timezone.utc).astimezone():%Y-%m}"
    assert mover.month_dir_of(_scanned_at(src, utc_naive)) == expected


def test_month_dir_falls_back_to_modified_at(tmp_path, monkeypatch):
    s = _scanned(str(write_file(str(tmp_path / "a.txt"), b"x")))
    s.created_at = None
    s.modified_at = datetime(2024, 2, 10, 12, 0, tzinfo=timezone.utc)
    # 実体の birthtime を無効化して modified_at に落ちることを確かめる
    monkeypatch.setattr(mover.os, "stat", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    assert mover.month_dir_of(s) == "2024-02"


def test_month_dir_unknown_when_no_dates(tmp_path, monkeypatch):
    s = _scanned(str(write_file(str(tmp_path / "a.txt"), b"x")))
    s.created_at = None
    s.modified_at = None
    monkeypatch.setattr(mover.os, "stat", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    assert mover.month_dir_of(s) == mover.UNKNOWN_MONTH_DIR


def test_by_month_overflows_into_numbered_branches(session, make_config, tmp_path, archive):
    """上限（ここでは 3）に達したら 001、それも埋まれば 002 … に入れる。"""
    config = make_config(source_path=str(tmp_path), folder_limit=3)
    planner = mover.DestPlanner(session, config, None)
    when = datetime(2025, 3, 15)
    got = []
    for i in range(8):
        src = write_file(str(tmp_path / f"f{i}.txt"), bytes([i]))
        got.append(planner.resolve(_scanned_at(src, when)))
    assert got == [
        "2025-03/f0.txt", "2025-03/f1.txt", "2025-03/f2.txt",
        "2025-03/001/f3.txt", "2025-03/001/f4.txt", "2025-03/001/f5.txt",
        "2025-03/002/f6.txt", "2025-03/002/f7.txt",
    ]


def test_by_month_counts_existing_files_on_disk(session, make_config, tmp_path, archive):
    """人が手で置いたファイルも件数に含める（実体の数を見る）。"""
    config = make_config(source_path=str(tmp_path), folder_limit=3)
    _fill(archive, "2025-03", 3)         # 直下は満杯
    _fill(archive, "2025-03/001", 2)     # 001 はあと 1 件入る
    planner = mover.DestPlanner(session, config, None)
    when = datetime(2025, 3, 15)
    a = write_file(str(tmp_path / "a.txt"), b"a")
    b = write_file(str(tmp_path / "b.txt"), b"b")
    assert planner.resolve(_scanned_at(a, when)) == "2025-03/001/a.txt"
    assert planner.resolve(_scanned_at(b, when)) == "2025-03/002/b.txt"


def test_by_month_counts_db_reservations(session, make_config, tmp_path, archive):
    """実体が無くても pending 予約は件数に含める（クラッシュ途中の状態を想定）。"""
    config = make_config(source_path=str(tmp_path), folder_limit=2)
    for i in range(2):
        session.add(
            ArchiveFile(
                archive_id=1,
                filehash=f"{i:064x}", hash_algo="sha256", size=1, name=f"p{i}.txt",
                stored_path_rel=f"2025-03/p{i}.txt", status=STATUS_PENDING,
            )
        )
    session.commit()
    planner = mover.DestPlanner(session, config, None)
    src = write_file(str(tmp_path / "a.txt"), b"a")
    assert planner.resolve(_scanned_at(src, datetime(2025, 3, 1))) == "2025-03/001/a.txt"


def test_by_month_branch_number_grows_past_999(session, make_config, tmp_path, archive):
    """999 の次は 1000（桁が増えるだけで止まらない）。"""
    config = make_config(source_path=str(tmp_path), folder_limit=1)
    planner = mover.DestPlanner(session, config, None)
    # 直下と 001〜999 が全部満杯であるとキャッシュに直接教える（実体を 1000 個作らない）
    planner._counts["2025-03"] = 1
    for n in range(1, 1000):
        planner._counts[f"2025-03/{n:03d}"] = 1
    src = write_file(str(tmp_path / "a.txt"), b"a")
    assert planner.resolve(_scanned_at(src, datetime(2025, 3, 1))) == "2025-03/1000/a.txt"


def test_by_month_collision_stays_in_same_bucket(session, make_config, tmp_path, archive):
    config = make_config(source_path=str(tmp_path))
    write_file(os.path.join(archive, "2025-03", "a.txt"), b"other")
    planner = mover.DestPlanner(session, config, None)
    src = write_file(str(tmp_path / "a.txt"), b"a")
    assert planner.resolve(_scanned_at(src, datetime(2025, 3, 1))) == "2025-03/a (2).txt"


def test_tree_keeps_source_structure_under_month(session, make_config, tmp_path):
    config = make_config(source_path=str(tmp_path))
    planner = mover.DestPlanner(session, config, "写真2024")
    src = write_file(str(tmp_path / "旅行" / "IMG_0001.jpg"), b"x")
    s = _scanned_at(src, datetime(2025, 3, 15))
    s.path_rel = "旅行/IMG_0001.jpg"
    assert planner.resolve(s) == "2025-03/写真2024/旅行/IMG_0001.jpg"


def test_tree_without_dest_subdir(session, make_config, tmp_path):
    config = make_config(source_path=str(tmp_path))
    planner = mover.DestPlanner(session, config, None)
    src = write_file(str(tmp_path / "a" / "b.txt"), b"x")
    s = _scanned_at(src, datetime(2025, 3, 15))
    s.path_rel = "a/b.txt"
    assert planner.resolve(s) == "2025-03/a/b.txt"


def test_tree_single_file_at_month_root(session, make_config, tmp_path):
    """投入元が単一ファイル（path_rel にディレクトリ無し）なら年月フォルダ直下。"""
    config = make_config(source_path=str(tmp_path))
    planner = mover.DestPlanner(session, config, None)
    src = write_file(str(tmp_path / "solo.txt"), b"x")
    assert planner.resolve(_scanned_at(src, datetime(2025, 3, 15))) == "2025-03/solo.txt"


def test_tree_splits_same_folder_across_months(session, make_config, tmp_path):
    """同じ投入元フォルダでも作成月が違えば別の年月フォルダに入る（B 案の性質）。"""
    config = make_config(source_path=str(tmp_path))
    planner = mover.DestPlanner(session, config, "inbox")
    a = write_file(str(tmp_path / "d" / "a.txt"), b"a")
    b = write_file(str(tmp_path / "d" / "b.txt"), b"b")
    sa, sb = _scanned_at(a, datetime(2025, 3, 1)), _scanned_at(b, datetime(2025, 4, 1))
    sa.path_rel, sb.path_rel = "d/a.txt", "d/b.txt"
    assert planner.resolve(sa) == "2025-03/inbox/d/a.txt"
    assert planner.resolve(sb) == "2025-04/inbox/d/b.txt"


def test_tree_branches_at_leaf_folder(session, make_config, tmp_path):
    """枝分かれは葉フォルダ（ファイルが直接入る所）ごとに独立して起きる。"""
    config = make_config(source_path=str(tmp_path), folder_limit=2)
    planner = mover.DestPlanner(session, config, None)
    when = datetime(2025, 3, 1)
    got = []
    for d, i in [("x", 0), ("x", 1), ("x", 2), ("y", 0), ("x", 3), ("y", 1), ("y", 2)]:
        src = write_file(str(tmp_path / d / f"f{i}.txt"), f"{d}{i}".encode())
        s = _scanned_at(src, when)
        s.path_rel = f"{d}/f{i}.txt"
        got.append(planner.resolve(s))
    assert got == [
        "2025-03/x/f0.txt", "2025-03/x/f1.txt", "2025-03/x/001/f2.txt",
        "2025-03/y/f0.txt", "2025-03/x/001/f3.txt", "2025-03/y/f1.txt",
        "2025-03/y/001/f2.txt",
    ]


def test_plan_and_move_by_month_end_to_end(session, make_config, tmp_path, ingest_row, archive):
    config = make_config(source_path=str(tmp_path))
    planner = mover.DestPlanner(session, config, None)
    src = write_file(str(tmp_path / "inbox" / "a.txt"), b"hello")
    s = _scanned_at(src, datetime(2025, 3, 1))
    result = mover.plan_and_move(session, config, ingest_row.id, s, planner)
    assert result.ok
    assert result.stored_path_rel == "2025-03/a.txt"
    assert os.path.exists(os.path.join(archive, "2025-03", "a.txt"))
    assert not os.path.exists(src)


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
    # 作成直後のファイルなので今月の年月フォルダに、dest_subdir "inbox" の下に入る
    assert result.stored_path_rel == f"{datetime.now():%Y-%m}/inbox/a.txt"
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
        archive_id=1,
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


def test_prune_empty_dirs_treats_ds_store_only_as_empty(tmp_path):
    """Finder の .DS_Store しか無いフォルダは空として消す（macOS で必ず起きる）。"""
    root = tmp_path / "inbox"
    write_file(str(root / "a" / ".DS_Store"), b"junk")
    (root / "a" / "b").mkdir()
    write_file(str(root / ".DS_Store"), b"junk")  # root 直下のものは root ごと残す
    assert mover.prune_empty_dirs(str(root)) == 2
    assert not (root / "a").exists()
    assert root.is_dir()


def test_prune_empty_dirs_keeps_folders_with_real_files(tmp_path):
    root = tmp_path / "inbox"
    write_file(str(root / "a" / ".DS_Store"), b"junk")
    write_file(str(root / "a" / "keep.txt"), b"real")
    assert mover.prune_empty_dirs(str(root)) == 0
    assert (root / "a" / "keep.txt").exists()
    assert (root / "a" / ".DS_Store").exists()  # 実ファイルがあるフォルダは触らない
