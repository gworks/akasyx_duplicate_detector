# adopt.py - 中身のある保存フォルダを実体から登録する（#4 / #5）
#
# 中身があるのに記録が 1 件も無い保存フォルダ（既存の写真フォルダを初めて保存フォルダにした、
# 正本 DB を失った 等）の実体を走査し、stored として登録する。ファイルは動かさない。
# 同じ内容は全保存フォルダで 1 つだけ stored にし（archives.promote_many）、2 つ目以降は
# unregistered にして archive_duplicate として報告する。
#
# 途中で落ちても半端な記録を残さないよう、走査が終わってから全件を 1 トランザクションで書く。
# 落ちたら登録だけ残るが、記録が 0 件なので adopt 以外のコマンドは断ったまま（やり直せる）。
import dataclasses
import logging
import shutil
import tempfile
from datetime import datetime

import archives
import crawler_client
from config import OS_JUNK_FILES
from database import META_DIRNAME
from errors import PreflightError
from models import (
    RESULT_ADOPTED,
    RESULT_ARCHIVE_DUPLICATE,
    RESULT_SKIPPED_EMPTY,
    STATUS_UNREGISTERED,
    Archive,
    ArchiveFile,
    IngestItem,
    PendingAdoption,
)
from utl.result_csv import create_csv, csv_update

logger = logging.getLogger(__name__)


def run_adopt(session, config, ingest) -> tuple[str, dict]:
    """保存フォルダの実体を登録します。戻り値は (実行ステータス, result 別件数)。

    全部か無しか: 読めない場所・ハッシュを取れないファイルが 1 つでもあれば何も登録せずに断る。
    一部だけ登録して確定すると、記録があるので adopt をやり直せず、登録し損ねた内容と同じファイルを
    あとの add が重複として止めずに取り込む。直してから adopt をやり直せばよい。
    """
    # adopt 待ちにしてから始める（登録済みの保存フォルダに手で入れたファイルを登録する場合も。途中で落ちたら
    # 他のコマンドは断る）。全件の登録と同じコミットで外す
    if session.get(PendingAdoption, config.archive_id) is None:
        session.add(PendingAdoption(archive_id=config.archive_id))
        session.commit()
    archives.check_tree_readable(config.archive_root)
    # 使い捨ての作業用 DB で走査する。共有の作業用 DB だと、以前の走査で読めなかったファイルを crawler が
    # 「同じ stat で 2 回失敗したので取り直さない」と覚えていて、権限を直しても adopt が断られ続ける
    work_dir = tempfile.mkdtemp(prefix="adopt-", dir=config.db_dir)
    try:
        scan = crawler_client.run_crawler(
            config.archive_root,
            dataclasses.replace(config, db_dir=work_dir),
            extra_excludes=(f"{META_DIRNAME}/", *OS_JUNK_FILES),
        )
        crawler_client.ensure_completed(scan)
        scanned = sorted(
            crawler_client.read_files(scan.db_path, scan.scan_id), key=lambda f: f.path_rel
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    # 作業用 DB は消すので、実行記録には残さない（残すと存在しないパスを指す）
    ingest.crawler_scan_id = scan.scan_id
    session.commit()

    # verify が未登録として登録した行は同じパスを占めている（パスの一意索引）。新しく作らずに使い回す
    existing = {
        r.stored_path_rel: r
        for r in session.query(ArchiveFile).filter(
            ArchiveFile.archive_id == config.archive_id,
            ArchiveFile.status == STATUS_UNREGISTERED,
        )
    }
    counters: dict[str, int] = {}
    decisions: list[tuple[object, str, ArchiveFile | None, str | None]] = []
    first_of: dict[tuple[str, str], ArchiveFile] = {}
    unreadable: list[str] = []

    try:
        for f in scanned:
            if f.size < config.min_size:
                # 0 バイトのファイルは add でも取り込まない（保存価値が無く、全部が同じハッシュになる）
                decisions.append((f, RESULT_SKIPPED_EMPTY, None, f"size {f.size} is below min_size"))
                continue
            if not f.filehash or not f.hash_algo:
                # 読めなかった。1 つでもあれば全体を断る（下で）
                unreadable.append(f.path_abs)
                continue
            row = existing.get(f.path_rel)
            if row is None:
                row = ArchiveFile(archive_id=config.archive_id, stored_path_rel=f.path_rel)
                session.add(row)
            row.filehash = f.filehash
            row.hash_algo = f.hash_algo
            row.size = f.size
            row.name = f.name
            row.origin_modified_at = f.modified_at
            row.mime_type = f.mime_type
            row.ingest_id = ingest.id
            row.status = STATUS_UNREGISTERED
            key = (f.filehash, f.hash_algo)
            if key in first_of:
                first = first_of[key]
                decisions.append(
                    (f, RESULT_ARCHIVE_DUPLICATE, row, f"Same content as {first.stored_path_rel}")
                )
            else:
                first_of[key] = row
                decisions.append((f, RESULT_ADOPTED, row, None))

        if unreadable:
            session.rollback()
            listed = "\n".join(f"  {p}" for p in unreadable[:5])
            more = f"\n  ... and {len(unreadable) - 5} more" if len(unreadable) > 5 else ""
            raise PreflightError(
                f"{len(unreadable)} files could not be read, so adopt did not register anything:\n"
                f"{listed}{more}\n"
                "  Fix the permissions (or remove these files) and run adopt again."
            )

        promoted = {id(r) for r in archives.promote_many(session, list(first_of.values()))}
        for i, (f, result, row, message) in enumerate(decisions):
            first = first_of[(row.filehash, row.hash_algo)] if row is not None else None
            if first is not None and id(first) not in promoted:
                # 組の最初も含めて、同じ内容が別の保存フォルダに stored（全体で 1 つ — #6）。
                # 組の 2 つ目以降も、この中の unregistered ではなく本当の保存先を示す
                decisions[i] = (f, RESULT_ARCHIVE_DUPLICATE, row, _elsewhere(session, row))
        session.flush()

        ts_start = f"{datetime.now():%Y%m%d_%H%M%S}"
        csv_file = create_csv(config.log_dir, "adopt_result", ts_start)
        for f, result, row, message in decisions:
            counters[result] = counters.get(result, 0) + 1
            session.add(
                IngestItem(
                    ingest_id=ingest.id,
                    source_path_abs=f.path_abs,
                    source_path_rel=f.path_rel,
                    name=f.name,
                    size=f.size,
                    filehash=f.filehash,
                    hash_algo=f.hash_algo,
                    result=result,
                    archive_file_id=row.id if row is not None else None,
                    planned_path_rel=f.path_rel,
                    message=message,
                )
            )
            csv_update(csv_file, [f.name, f.path_abs, result, f.size, f.path_rel, message or ""])
        pending = session.get(PendingAdoption, config.archive_id)
        if pending is not None:
            session.delete(pending)
        session.commit()
    except KeyboardInterrupt:
        session.rollback()
        logger.warning("Interrupted by user; nothing was registered (run adopt again)")
        return "interrupted", {}

    logger.info(f"CSV report: {csv_file}")
    return "completed", counters


def _elsewhere(session, row: ArchiveFile) -> str:
    other = (
        session.query(ArchiveFile)
        .filter(
            ArchiveFile.filehash == row.filehash,
            ArchiveFile.hash_algo == row.hash_algo,
            ArchiveFile.archive_id != row.archive_id,
            ArchiveFile.status == "stored",
        )
        .order_by(ArchiveFile.id)
        .first()
    )
    if other is None:  # pragma: no cover - promote_many が断った以上、通常は見つかる
        return "Same content is stored in another archive folder"
    archive = session.get(Archive, other.archive_id)
    root = archive.root_abs if archive is not None else f"#{other.archive_id}"
    return f"Same content already in archive folder {root}: {other.stored_path_rel}"
