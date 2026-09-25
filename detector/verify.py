# verify.py - 保存フォルダと DB の整合性チェック（設計書 §9）
#
# 「先に全件のハッシュ表を作ってから」判定するのが肝。パス単位で逐次判定すると、
# 人が動かしたファイルを missing + unregistered の2件に割ってしまう。
import logging
from datetime import datetime

import crawler_client
from config import OS_JUNK_FILES
from database import META_DIRNAME
from models import (
    DISPOSITION_QUARANTINE,
    OWNING_STATUSES,
    RESULT_ARCHIVE_DUPLICATE,
    RESULT_HASH_MISMATCH,
    RESULT_MISSING,
    RESULT_RELOCATED,
    RESULT_UNREGISTERED,
    STATUS_MISSING,
    STATUS_STORED,
    STATUS_UNREGISTERED,
    ArchiveFile,
    IngestItem,
    utcnow,
)
from utl.result_csv import create_csv, csv_update

logger = logging.getLogger(__name__)

VERIFY_OK = "ok"


def run_verify(session, config, ingest) -> tuple[str, dict]:
    """保存フォルダを再スキャンして DB と突き合わせます。戻り値は (ステータス, 内訳)。"""
    scan = crawler_client.run_crawler(
        config.archive_root, config, extra_excludes=(f"{META_DIRNAME}/", *OS_JUNK_FILES)
    )
    crawler_client.ensure_completed(scan)
    ingest.crawler_db_path = scan.db_path
    ingest.crawler_scan_id = scan.scan_id
    session.commit()

    scanned = list(crawler_client.read_files(scan.db_path, scan.scan_id))
    by_rel = {f.path_rel: f for f in scanned}
    by_hash: dict[tuple, list] = {}
    for f in scanned:
        if f.filehash and f.hash_algo:
            by_hash.setdefault((f.filehash, f.hash_algo), []).append(f)

    counters: dict[str, int] = {}
    findings: list[tuple[str, ArchiveFile, object, str | None]] = []
    claimed: set[str] = set()

    aid = config.archive_id
    owning = (
        session.query(ArchiveFile)
        .filter(ArchiveFile.archive_id == aid, ArchiveFile.status.in_(OWNING_STATUSES))
        .order_by(ArchiveFile.id)
        .all()
    )
    unresolved: list[ArchiveFile] = []

    # Pass 1: パスが一致する行を先に確定させる
    for row in owning:
        found = by_rel.get(row.stored_path_rel)
        if found is None:
            unresolved.append(row)
            continue
        claimed.add(found.path_rel)
        if found.filehash == row.filehash:
            row.verified_at = utcnow()
            counters[VERIFY_OK] = counters.get(VERIFY_OK, 0) + 1
        else:
            # 実体はあるが内容が違う。何が正しいかは機械には決められないので
            # DB のハッシュは書き換えず、人に見せる（設計書 §9）
            findings.append(
                (
                    RESULT_HASH_MISMATCH,
                    row,
                    found,
                    f"DB {row.filehash} / actual {found.filehash}",
                )
            )

    # Pass 2: 実体が別の場所で見つかる行（人が動かした）を relocated として吸収する
    for row in unresolved:
        key = (row.filehash, row.hash_algo)
        candidate = next(
            (f for f in by_hash.get(key, []) if f.path_rel not in claimed), None
        )
        if candidate is None:
            row.status = STATUS_MISSING
            findings.append((RESULT_MISSING, row, None, None))
            continue
        claimed.add(candidate.path_rel)
        old = row.stored_path_rel
        row.stored_path_rel = candidate.path_rel
        row.verified_at = utcnow()
        findings.append((RESULT_RELOCATED, row, candidate, f"{old} → {candidate.path_rel}"))

    session.commit()

    # Pass 3: DB に無い実体を拾う（復活・保存フォルダ内重複・純粋な未登録）
    owning_hashes = {
        (r.filehash, r.hash_algo)
        for r in session.query(ArchiveFile)
        .filter(ArchiveFile.archive_id == aid, ArchiveFile.status.in_(OWNING_STATUSES))
        .all()
    }
    seen_unowned: set[tuple] = set()
    for f in scanned:
        if f.path_rel in claimed:
            continue
        key = (f.filehash, f.hash_algo)

        revived = _revive_missing(session, aid, f, key, owning_hashes)
        if revived is not None:
            findings.append((RESULT_RELOCATED, revived, f, "revived from missing"))
            owning_hashes.add(key)
            continue

        row = _register_unregistered(session, f, ingest)
        duplicated = bool(f.filehash) and (
            key in owning_hashes or key in seen_unowned
        )
        if f.filehash:
            seen_unowned.add(key)
        kind = RESULT_ARCHIVE_DUPLICATE if duplicated else RESULT_UNREGISTERED
        findings.append((kind, row, f, None))

    session.commit()

    # 記録・処置予定フラグ
    ts_start = f"{datetime.now():%Y%m%d_%H%M%S}"
    csv_file = create_csv(config.log_dir, "verify_result", ts_start)
    flags = set(config.flag_quarantine)

    for kind, row, found, message in findings:
        counters[kind] = counters.get(kind, 0) + 1
        if kind in flags:
            row.disposition = DISPOSITION_QUARANTINE
        session.add(
            IngestItem(
                ingest_id=ingest.id,
                source_path_abs=(found.path_abs if found is not None else ""),
                source_path_rel=row.stored_path_rel,
                name=row.name,
                size=row.size,
                filehash=row.filehash,
                hash_algo=row.hash_algo,
                result=kind,
                archive_file_id=row.id,
                message=message,
            )
        )
        csv_update(
            csv_file,
            [
                row.name,
                (found.path_abs if found is not None else ""),
                kind,
                row.size,
                row.stored_path_rel,
                message or "",
            ],
        )

    session.commit()
    if flags:
        flagged = sum(counters.get(k, 0) for k in flags)
        logger.info(
            f"Set the disposition flag (disposition=quarantine) on {flagged} files. "
            "This command did not move any files"
        )
    logger.info(f"CSV report: {csv_file}")
    return "completed", counters


def _revive_missing(session, archive_id, found, key, owning_hashes) -> ArchiveFile | None:
    """missing 行と同一内容の実体が見つかったら復活させます。

    同じ内容を保持している owning 行が既にあるときは復活させない
    （部分 UNIQUE 索引に抵触するため。その実体は保存フォルダ内の重複として扱う）。
    """
    if not found.filehash or key in owning_hashes:
        return None
    row = (
        session.query(ArchiveFile)
        .filter(
            ArchiveFile.archive_id == archive_id,
            ArchiveFile.filehash == found.filehash,
            ArchiveFile.hash_algo == found.hash_algo,
            ArchiveFile.status == STATUS_MISSING,
        )
        .order_by(ArchiveFile.id)
        .first()
    )
    if row is None:
        return None
    row.status = STATUS_STORED
    row.stored_path_rel = found.path_rel
    row.verified_at = utcnow()
    row.disposition = None
    session.flush()
    return row


def _register_unregistered(session, found, ingest) -> ArchiveFile:
    """DB に無い実体を unregistered として登録します（既にあれば更新）。

    verify を繰り返しても行が増えないよう、同じパスの既存行を再利用する。
    """
    row = (
        session.query(ArchiveFile)
        .filter(
            ArchiveFile.archive_id == ingest.archive_id,
            ArchiveFile.stored_path_rel == found.path_rel,
            ArchiveFile.status == STATUS_UNREGISTERED,
        )
        .first()
    )
    if row is None:
        row = ArchiveFile(
            archive_id=ingest.archive_id,
            filehash=found.filehash or "",
            hash_algo=found.hash_algo or "",
            size=found.size,
            name=found.name,
            stored_path_rel=found.path_rel,
            mime_type=found.mime_type,
            ingest_id=ingest.id,
            status=STATUS_UNREGISTERED,
        )
        session.add(row)
    else:
        row.filehash = found.filehash or ""
        row.hash_algo = found.hash_algo or ""
        row.size = found.size
        row.ingest_id = ingest.id
    session.flush()
    return row


def archive_stats(session, archive_id: int) -> dict:
    """保存フォルダの状態サマリ（report 用）。"""
    rows = (
        session.query(ArchiveFile.status, ArchiveFile.size)
        .filter(ArchiveFile.archive_id == archive_id)
        .all()
    )
    stats: dict[str, dict] = {}
    for status, size in rows:
        bucket = stats.setdefault(status, {"count": 0, "size": 0})
        bucket["count"] += 1
        bucket["size"] += size or 0
    return stats
