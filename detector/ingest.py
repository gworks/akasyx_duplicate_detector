# ingest.py - add の判定ループ（設計書 §7.1）
import logging
import os
from datetime import datetime

import crawler_client
import mover
from models import (
    OWNING_STATUSES,
    RESULT_DUPLICATE,
    RESULT_FAILED,
    RESULT_MOVED,
    RESULT_SKIPPED_EMPTY,
    RESULT_SKIPPED_NOHASH,
    ArchiveFile,
    IngestItem,
)
from utl.result_csv import create_csv, csv_update

logger = logging.getLogger(__name__)

COMMIT_INTERVAL = 100


def find_owning(session, filehash: str, hash_algo: str) -> ArchiveFile | None:
    """同一内容を保持している行を返します（同一 hash_algo 同士でのみ照合）。"""
    return (
        session.query(ArchiveFile)
        .filter(
            ArchiveFile.filehash == filehash,
            ArchiveFile.hash_algo == hash_algo,
            ArchiveFile.status.in_(OWNING_STATUSES),
        )
        .order_by(ArchiveFile.id)
        .first()
    )


def judge(
    session, scanned, config, virtual_hashes: set | None = None
) -> tuple[str, ArchiveFile | None, str | None]:
    """1ファイルの判定（設計書 §7.1 の判定表）。上から順に評価する。

    戻り値の RESULT_MOVED は「移動対象」の意味で、実際の移動は呼び出し側が行う。
    """
    if scanned.size < config.min_size:
        return (
            RESULT_SKIPPED_EMPTY,
            None,
            f"サイズ {scanned.size} が min_size {config.min_size} 未満",
        )

    if not scanned.filehash or not scanned.hash_algo:
        # crawler は読み取りエラー時に filehash=NULL で登録を続行する。
        # これを新規と誤判定すると重複が保存フォルダに紛れ込む（設計書 §7.1 #2）
        return RESULT_SKIPPED_NOHASH, None, "ハッシュが取得できず判定できません"

    key = (scanned.filehash, scanned.hash_algo)
    if virtual_hashes is not None and key in virtual_hashes:
        # dry-run で、同じ実行内の先行ファイルが取り込み対象になっている
        return RESULT_DUPLICATE, None, "同じ実行内の先行ファイルと内容が同じです"

    existing = find_owning(session, *key)
    if existing is not None:
        if existing.size != scanned.size:
            return (
                RESULT_FAILED,
                existing,
                f"ハッシュは一致するがサイズが違います（DB {existing.size} / 実体 {scanned.size}）",
            )
        return RESULT_DUPLICATE, existing, None

    return RESULT_MOVED, None, None


def _collect_files(config):
    """投入元を走査して ScannedFile のイテレータと crawler スキャン情報を返します。"""
    source = config.source_path
    if os.path.isdir(source):
        scan = crawler_client.run_crawler(source, config)
        crawler_client.ensure_completed(scan)
        return crawler_client.read_files(scan.db_path, scan.scan_id), scan
    # 単一ファイルは crawler を使わず直接読む（設計書 §6.4）
    return iter([crawler_client.scan_single_file(source)]), None


def resolve_dest_subdir(config) -> str | None:
    """保存先の第1階層名を決めます。

    --dest-subdir 未指定（None）なら投入元フォルダ名、`''` を渡されたら保存フォルダ直下。
    """
    if config.dest_subdir is not None:
        return config.dest_subdir or None
    if os.path.isdir(config.source_path):
        return os.path.basename(os.path.normpath(config.source_path))
    return None


def run_add(session, config, ingest) -> tuple[str, dict]:
    """投入元を取り込みます。戻り値は (実行ステータス, result 別件数)。"""
    files, scan = _collect_files(config)
    if scan is not None:
        ingest.crawler_db_path = scan.db_path
        ingest.crawler_scan_id = scan.scan_id
        session.commit()

    dest_subdir = resolve_dest_subdir(config)
    counters: dict[str, int] = {}
    reserved: set[str] = set()       # dry-run 用: 予約済みの保存先（casefold）
    virtual_hashes: set = set()      # dry-run 用: 取り込み予定の内容
    status = "completed"

    ts_start = f"{datetime.now():%Y%m%d_%H%M%S}"
    csv_file = create_csv(config.log_dir, "add_result", ts_start)
    processed = 0

    try:
        for scanned in files:
            processed += 1
            result, stored_rel, message = _process_one(
                session, config, ingest, scanned, dest_subdir, reserved, virtual_hashes
            )
            counters[result] = counters.get(result, 0) + 1
            csv_update(
                csv_file,
                [
                    scanned.name,
                    scanned.path_abs,
                    result,
                    scanned.size,
                    stored_rel or "",
                    message or "",
                ],
            )
            print(f"判定中: {processed}件 ({result})")
            if not config.dry_run and processed % COMMIT_INTERVAL == 0:
                session.commit()
    except KeyboardInterrupt:
        status = "interrupted"
        logger.warning("ユーザー操作により中断されました")
    finally:
        if not config.dry_run:
            session.commit()

    if config.prune_empty_dirs and not config.dry_run and os.path.isdir(config.source_path):
        removed = mover.prune_empty_dirs(config.source_path)
        if removed:
            logger.info(f"空になった投入元ディレクトリを {removed} 件削除しました")

    logger.info(f"CSV レポート: {csv_file}")
    return status, counters


def _process_one(
    session, config, ingest, scanned, dest_subdir, reserved, virtual_hashes
) -> tuple[str, str | None, str | None]:
    """1ファイルを判定し、必要なら移動して記録します。

    戻り値は (result, 保存先の相対パス, メッセージ)。
    """
    try:
        result, existing, message = judge(
            session, scanned, config, virtual_hashes if config.dry_run else None
        )
    except Exception as e:  # 1件の失敗で実行全体を止めない（設計書 §12）
        session.rollback()
        logger.exception(f"判定中に想定外のエラー: {scanned.path_abs}: {e}")
        result, existing, message = RESULT_FAILED, None, str(e)

    archive_file_id = existing.id if existing is not None else None
    stored_rel = None

    if result == RESULT_MOVED:
        if config.dry_run:
            try:
                stored_rel = mover.resolve_dest(
                    session,
                    config.archive_root,
                    dest_subdir,
                    scanned.path_rel,
                    extra_taken=reserved,
                )
                reserved.add(stored_rel.casefold())
                virtual_hashes.add((scanned.filehash, scanned.hash_algo))
            except Exception as e:
                result, message = RESULT_FAILED, str(e)
        else:
            move = mover.plan_and_move(session, config, ingest.id, scanned, dest_subdir)
            if move.ok:
                stored_rel = move.stored_path_rel
                archive_file_id = move.archive_file.id
            elif move.conflict:
                # 予約が UNIQUE で弾かれた = 直前に同一内容が登録された
                result = RESULT_DUPLICATE
                owner = find_owning(session, scanned.filehash, scanned.hash_algo)
                archive_file_id = owner.id if owner is not None else None
                message = "同一内容が直前に登録されました"
            else:
                result = RESULT_FAILED
                message = move.message

    if not config.dry_run:
        session.add(
            IngestItem(
                ingest_id=ingest.id,
                source_path_abs=scanned.path_abs,
                source_path_rel=scanned.path_rel,
                name=scanned.name,
                size=scanned.size,
                filehash=scanned.filehash,
                hash_algo=scanned.hash_algo,
                result=result,
                archive_file_id=archive_file_id,
                planned_path_rel=stored_rel,
                message=message,
            )
        )

    return result, stored_rel, message
