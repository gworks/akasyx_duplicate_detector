# dedupe.py - 据え置いた重複の後始末（設計書 §10）
#
# 削除は取り返しがつかないため、1件ごとに毎回ハッシュを再計算して2点検証する。
# --yes が無ければ検証結果を表示するだけで、何も削除しない。
import logging
import os
from datetime import datetime

import mover
from ingest import own_data_matcher
from database import tmp_dir
from models import (
    RESOLUTION_DELETED,
    RESOLUTION_GONE,
    RESOLUTION_TRASHED,
    RESULT_DUPLICATE,
    STATUS_STORED,
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


def check_item(session, config, item) -> tuple[str, str | None]:
    """削除前の2点検証（設計書 §10）。戻り値は (判定, メッセージ)。

    ① 投入元のファイルが存在し、現在のハッシュが記録と一致する
    ② 対応する保存フォルダ側の行が stored で、実体があり、ハッシュが一致する
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

    row = (
        session.get(ArchiveFile, item.archive_file_id)
        if item.archive_file_id
        else None
    )
    if row is None:
        row = (
            session.query(ArchiveFile)
            .filter(
                ArchiveFile.archive_id == config.archive_id,
                ArchiveFile.filehash == item.filehash,
                ArchiveFile.hash_algo == item.hash_algo,
                ArchiveFile.status == STATUS_STORED,
            )
            .first()
        )
    if row is None or row.status != STATUS_STORED or row.archive_id != config.archive_id:
        return CHECK_ARCHIVE_MISSING, "No matching record in the archive folder"

    dst = from_posix(config.archive_root, row.stored_path_rel)
    if not os.path.lexists(dst):
        return CHECK_ARCHIVE_MISSING, f"Archived file is missing: {row.stored_path_rel}"
    try:
        if hashing.file_hash(dst) != item.filehash:
            return CHECK_ARCHIVE_MISSING, "Archived file content does not match"
    except OSError as e:
        return CHECK_ARCHIVE_MISSING, f"Cannot read archived file: {e}"

    return CHECK_OK, None


def _trash_dest(config, item) -> str:
    """--trash-dir の退避先を決めます（投入元の相対パス構造を再現）。"""
    rel = to_posix(item.source_path_rel or item.name)
    return os.path.join(config.trash_dir, *[p for p in rel.split("/") if p])


def run_delete_duplicates(session, config, ingest) -> tuple[str, dict]:
    """据え置いた重複を検証して削除（または退避）します。"""
    items = _pending_items(session, config)
    counters: dict[str, int] = {}
    total_size = 0
    status = "completed"

    ts_start = f"{datetime.now():%Y%m%d_%H%M%S}"
    csv_file = create_csv(config.log_dir, "dedupe_result", ts_start)

    try:
        for item in items:
            verdict, message = check_item(session, config, item)
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
