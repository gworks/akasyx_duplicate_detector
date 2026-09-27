# dedupe.py - 据え置いた重複の後始末（設計書 §10）
#
# 削除は取り返しがつかないため、1件ごとに毎回ハッシュを再計算して2点検証する。
# --yes が無ければ検証結果を表示するだけで、何も削除しない。
import logging
import os
from datetime import datetime

from sqlalchemy import case

import archives
import mover
from ingest import own_data_matcher
from database import tmp_dir
from errors import PreflightError
from models import (
    RESOLUTION_DELETED,
    RESOLUTION_GONE,
    RESOLUTION_TRASHED,
    RESULT_DUPLICATE,
    STATUS_STORED,
    Archive,
    ArchiveFile,
    Ingest,
    IngestItem,
    utcnow,
)
from utl import hashing
from utl.helpers import from_posix, to_posix
from utl.result_csv import create_csv, csv_update

logger = logging.getLogger(__name__)

# 検証結果（レポートの内訳キー）
CHECK_OK = "verified"
CHECK_GONE = "gone"
CHECK_SOURCE_CHANGED = "source_changed"
CHECK_ARCHIVE_MISSING = "archive_missing"
# 実物のある保存フォルダ（別の保存フォルダ・NAS 等）に今つながっていない。つながってから実行し直せば消せる
CHECK_ARCHIVE_UNAVAILABLE = "archive_unavailable"
# 消す対象が保存済みの実物そのもの（投入元が保存フォルダの中だった等）。消すと唯一の実物を失う
CHECK_SAME_FILE = "same_file"


def _pending_items(session, config):
    """未処置の duplicate を取り出します（dry-run の実行は対象外）。"""
    query = (
        session.query(IngestItem)
        .join(Ingest, IngestItem.ingest_id == Ingest.id)
        .filter(
            Ingest.archive_id == config.archive_id,
            IngestItem.result == RESULT_DUPLICATE,
            IngestItem.resolution.is_(None),
            Ingest.dry_run == 0,
        )
    )
    if config.ingest_id is not None:
        query = query.filter(IngestItem.ingest_id == config.ingest_id)
    return query.order_by(IngestItem.id).all()


def check_item(session, config, item, owner_roots: dict | None = None) -> tuple[str, str | None]:
    """削除前の2点検証（設計書 §10）。戻り値は (判定, メッセージ)。

    ① 投入元のファイルが存在し、現在のハッシュが記録と一致する
    ② 同じ内容の stored 行があり（どの保存フォルダでもよい — #6）、実体があり、ハッシュが一致する
       実体がある保存フォルダにつながっていなければ消さない。消す対象が実体そのものでも消さない
    owner_roots は保存フォルダごとの場所の確認結果のキャッシュ（1 回の実行で共有する）
    """
    src = item.source_path_abs
    if not src or not os.path.lexists(src):
        return CHECK_GONE, "Source file no longer exists"

    try:
        actual = hashing.file_hash(src)
    except OSError as e:
        return CHECK_SOURCE_CHANGED, f"Cannot read source file: {e}"
    if actual != item.filehash:
        return CHECK_SOURCE_CHANGED, f"Source file content has changed (now {actual})"

    rows = _stored_candidates(session, config, item)
    if not rows:
        return CHECK_ARCHIVE_MISSING, "No matching record in any archive folder"

    # 実物は、つながっている保存フォルダのものを使う（今の保存フォルダを優先 — #6）
    cache = owner_roots if owner_roots is not None else {}
    unavailable = None
    for row in rows:
        root, reason = _owner_root(session, config, row, cache)
        if reason is None:
            break
        unavailable = unavailable or reason
    else:
        return CHECK_ARCHIVE_UNAVAILABLE, unavailable

    dst = from_posix(root, row.stored_path_rel)
    if not os.path.lexists(dst):
        return CHECK_ARCHIVE_MISSING, f"Archived file is missing: {dst}"
    try:
        if os.path.samefile(src, dst):
            return CHECK_SAME_FILE, f"The source is the archived file itself: {dst}"
    except OSError as e:
        return CHECK_ARCHIVE_MISSING, f"Cannot read archived file: {e}"
    try:
        if hashing.file_hash(dst) != item.filehash:
            return CHECK_ARCHIVE_MISSING, "Archived file content does not match"
    except OSError as e:
        return CHECK_ARCHIVE_MISSING, f"Cannot read archived file: {e}"

    return CHECK_OK, None


def _stored_candidates(session, config, item) -> list:
    """検証に使える stored 行（判定時に参照した行 → 今の保存フォルダ → 他の保存フォルダの順）。"""
    rows = (
        session.query(ArchiveFile)
        .filter(
            ArchiveFile.filehash == item.filehash,
            ArchiveFile.hash_algo == item.hash_algo,
            ArchiveFile.status == STATUS_STORED,
        )
        .order_by(
            case((ArchiveFile.id == (item.archive_file_id or 0), 0), else_=1),
            case((ArchiveFile.archive_id == config.archive_id, 0), else_=1),
            ArchiveFile.id,
        )
        .all()
    )
    return rows


def _owner_root(session, config, row, cache: dict) -> tuple[str, str | None]:
    """保存済みの実物がある保存フォルダの場所を返します。戻り値は (場所, つながっていない理由)。

    今の実行の保存フォルダなら、起動時に確かめた場所をそのまま使う。別の保存フォルダは登録上の場所に
    あり、かつ `.akasyx/archive.id` の uid が一致するときだけ使う（外した NAS の跡に別のフォルダが
    あっても、それを実物の置き場と取り違えない）。結果は保存フォルダごとに cache に残し、
    ネットワーク上の場所を 1 件ごとに問い合わせない。
    """
    if row.archive_id == config.archive_id:
        return config.archive_root, None
    if row.archive_id not in cache:
        cache[row.archive_id] = _check_owner_root(session, row.archive_id)
    return cache[row.archive_id]


def _check_owner_root(session, archive_id: int) -> tuple[str, str | None]:
    archive = session.get(Archive, archive_id)
    if archive is None:  # pragma: no cover - FK があるので通常は起きない
        return "", f"Archive folder #{archive_id} is not registered"
    root = archive.root_abs
    try:
        present = archives.is_at_registered_location(archive)
    except PreflightError as e:
        return root, f"Cannot read the archive folder that holds the file: {e}"
    if not present:
        return root, f"The archive folder that holds the file is not available: {root}"
    return root, None


def _trash_dest(config, item) -> str:
    """--trash-dir の退避先を決めます（投入元の相対パス構造を再現）。"""
    rel = to_posix(item.source_path_rel or item.name)
    return os.path.join(config.trash_dir, *[p for p in rel.split("/") if p])


def run_delete_duplicates(session, config, ingest) -> tuple[str, dict]:
    """据え置いた重複を検証して削除（または退避）します。"""
    items = _pending_items(session, config)
    owner_roots: dict = {}
    counters: dict[str, int] = {}
    total_size = 0
    status = "completed"

    ts_start = f"{datetime.now():%Y%m%d_%H%M%S}"
    csv_file = create_csv(config.log_dir, "dedupe_result", ts_start)

    try:
        for item in items:
            verdict, message = check_item(session, config, item, owner_roots)
            counters[verdict] = counters.get(verdict, 0) + 1

            if verdict == CHECK_GONE:
                item.resolution = RESOLUTION_GONE
                item.resolved_at = utcnow()
            elif verdict == CHECK_OK:
                total_size += item.size or 0
                if config.assume_yes:
                    ok, message = _dispose(config, item)
                    if ok:
                        item.resolution = (
                            RESOLUTION_TRASHED if config.trash_dir else RESOLUTION_DELETED
                        )
                        item.resolved_at = utcnow()
                    else:
                        counters[CHECK_OK] -= 1
                        counters["failed"] = counters.get("failed", 0) + 1
                        verdict = "failed"

            csv_update(
                csv_file,
                [
                    item.name,
                    item.source_path_abs,
                    verdict,
                    item.size,
                    item.planned_path_rel or "",
                    message or "",
                ],
            )
            session.commit()
    except KeyboardInterrupt:
        status = "interrupted"
        logger.warning("Interrupted by user")
    finally:
        session.commit()

    if config.assume_yes and config.prune_empty_dirs and items:
        # 空ディレクトリの掃除は、元の取り込み実行が対象にした投入元ルートの中だけで行う
        ingest_ids = {i.ingest_id for i in items}
        roots = session.query(Ingest.source_root).filter(Ingest.id.in_(ingest_ids)).all()
        for (root,) in roots:
            if root:
                # 投入元の中にある detector 自身のデータフォルダ（ui/ 等）の空フォルダは消さない
                mover.prune_empty_dirs(root, keep=own_data_matcher(config, root))

    if not config.assume_yes:
        print(
            f"\nTo delete: {counters.get(CHECK_OK, 0)} files / {total_size} bytes total "
            f"(add --yes to actually delete)"
        )
    logger.info(f"CSV report: {csv_file}")
    return status, counters


def _dispose(config, item) -> tuple[bool, str | None]:
    """検証を通った1件を削除または退避します。"""
    src = item.source_path_abs
    try:
        if config.trash_dir:
            dst = _trash_dest(config, item)
            if os.path.lexists(dst):
                stem, ext = os.path.splitext(dst)
                dst = f"{stem}.{item.id}{ext}"
            mover.safe_move(src, dst, item.filehash, tmp_dir(config.archive_root))
            return True, f"Moved to trash: {dst}"
        os.unlink(src)
        return True, None
    except OSError as e:
        logger.error(f"Failed to dispose of file: {src}: {e}")
        return False, str(e)
