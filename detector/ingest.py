# ingest.py - add の判定ループ（設計書 §7.1）
import logging
import os
from datetime import datetime

import crawler_client
import mover
from config import OS_JUNK_FILES, own_data_dirs
from models import (
    OWNING_STATUSES,
    RESULT_DUPLICATE,
    RESULT_FAILED,
    RESULT_MOVED,
    RESULT_SKIPPED_EMPTY,
    RESULT_SKIPPED_NOHASH,
    RESULT_SKIPPED_OWN_DATA,
    ArchiveFile,
    IngestItem,
)
from utl.helpers import is_nested, key_within, path_key
from utl.result_csv import create_csv, csv_update

logger = logging.getLogger(__name__)

COMMIT_INTERVAL = 100


def find_owning(session, archive_id: int, filehash: str, hash_algo: str) -> ArchiveFile | None:
    """同一内容を保持している行を返します（同じ保存フォルダ・同一 hash_algo 同士でのみ照合）。"""
    return (
        session.query(ArchiveFile)
        .filter(
            ArchiveFile.archive_id == archive_id,
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
            f"size {scanned.size} is below min_size {config.min_size}",
        )

    if not scanned.filehash or not scanned.hash_algo:
        # crawler は読み取りエラー時に filehash=NULL で登録を続行する。
        # これを新規と誤判定すると重複が保存フォルダに紛れ込む（設計書 §7.1 #2）
        return RESULT_SKIPPED_NOHASH, None, "Cannot classify: hash unavailable"

    key = (scanned.filehash, scanned.hash_algo)
    if virtual_hashes is not None and key in virtual_hashes:
        # dry-run で、同じ実行内の先行ファイルが取り込み対象になっている
        return RESULT_DUPLICATE, None, "Same content as an earlier file in this run"

    existing = find_owning(session, config.archive_id, *key)
    if existing is not None:
        if existing.size != scanned.size:
            return (
                RESULT_FAILED,
                existing,
                f"Hash matches but size differs (DB {existing.size} / actual {scanned.size})",
            )
        return RESULT_DUPLICATE, existing, None

    return RESULT_MOVED, None, None


_SQLITE_SIDECARS = ("", "-wal", "-shm", "-journal")


def own_data_matcher(config, source: str | None = None):
    """detector 自身のデータ（データフォルダ全体・正本 DB・作業用 DB・ログ）かを判定する関数を返します。

    ホームフォルダを投入元にすると、配布版の既定データフォルダも投入元の中に入る。
    処理中の正本 DB や UI の設定・Electron のプロファイル（`ui/`）を移動すると履歴や設定を
    失い、開いたまま書き込まれて保存後の実体がハッシュと食い違うため、取り込み対象から外す。

    crawler のパスは投入元の表記のまま来るので、1 ファイルごとに realpath は呼ばない。
    代わりに自データ側を「そのままの表記」「実体の位置」「投入元の表記に写した位置」の
    キーにしておき、ファイル側は文字列の比較だけで判定する。
    """
    source = source if source is not None else config.source_path
    src_abs, src_real = os.path.abspath(source), os.path.realpath(source)

    def keys(path):
        found = {path_key(path, real=False), path_key(path)}
        real = os.path.realpath(path)
        if is_nested(src_real, real):
            # 投入元がシンボリックリンク経由（/var → /private/var 等）でも投入元の表記で突き合わせる
            found.add(path_key(os.path.join(src_abs, os.path.relpath(real, src_real)), real=False))
        return found

    dirs = set().union(*(keys(d) for d in own_data_dirs(config)))
    db_files = {k + sfx for k in keys(config.archive_db) for sfx in _SQLITE_SIDECARS}

    def matches(p):
        return p in db_files or any(key_within(d, p) for d in dirs)

    def is_own(path):
        if matches(path_key(path, real=False)):
            return True
        # --follow-symlinks だと投入元の中の symlink（~/dd → データフォルダ等）越しに自データが
        # 見えるので、そのときだけファイル側も実体の位置で比べる（既定では crawler は辿らない）
        return config.follow_symlinks and matches(path_key(path))

    return is_own


def _collect_files(config):
    """投入元を走査して ScannedFile のイテレータと crawler スキャン情報を返します。"""
    source = config.source_path
    if os.path.isdir(source):
        # .DS_Store 等の OS メタデータは保存する価値が無く、取り込むと保存フォルダが汚れる
        scan = crawler_client.run_crawler(source, config, extra_excludes=OS_JUNK_FILES)
        crawler_client.ensure_completed(scan)
        return crawler_client.read_files(scan.db_path, scan.scan_id), scan
    # 単一ファイルは crawler を使わず直接読む（設計書 §6.4）
    return iter([crawler_client.scan_single_file(source)]), None


def resolve_dest_subdir(config) -> str | None:
    """年月フォルダの下に置く階層名を決めます（設計書 §7.2）。

    --dest-subdir が明示されていればそれ（`''` は「付けない」）。
    未指定なら投入元フォルダ名。投入元が単一ファイルなら付けない。
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

    planner = mover.DestPlanner(session, config, resolve_dest_subdir(config))
    counters: dict[str, int] = {}
    reserved: set[str] = set()       # dry-run 用: 予約済みの保存先（casefold）
    virtual_hashes: set = set()      # dry-run 用: 取り込み予定の内容
    status = "completed"

    ts_start = f"{datetime.now():%Y%m%d_%H%M%S}"
    csv_file = create_csv(config.log_dir, "add_result", ts_start)
    processed = 0
    is_own = own_data_matcher(config)

    try:
        for scanned in files:
            processed += 1
            if is_own(scanned.path_abs):
                # 取り込まないが、CSV とサマリには残す（黙って減らすと件数が合わず取りこぼしに見える）
                result, stored_rel = RESULT_SKIPPED_OWN_DATA, None
                message = "The detector's own data (master DB / work DB / logs / UI data); not ingested"
                logger.warning(f"Skipped the detector's own data inside the source: {scanned.path_abs}")
                _record_item(session, config, ingest, scanned, result, None, None, message)
            else:
                result, stored_rel, message = _process_one(
                    session, config, ingest, scanned, planner, reserved, virtual_hashes
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
            print(f"Progress: {processed} files ({result})")
            if not config.dry_run and processed % COMMIT_INTERVAL == 0:
                session.commit()
    except KeyboardInterrupt:
        status = "interrupted"
        logger.warning("Interrupted by user")
    finally:
        if not config.dry_run:
            session.commit()

    if config.prune_empty_dirs and not config.dry_run and os.path.isdir(config.source_path):
        removed = mover.prune_empty_dirs(config.source_path, keep=is_own)
        if removed:
            logger.info(f"Removed {removed} empty source directories")

    logger.info(f"CSV report: {csv_file}")
    return status, counters


def _process_one(
    session, config, ingest, scanned, planner, reserved, virtual_hashes
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
        logger.exception(f"Unexpected error while classifying: {scanned.path_abs}: {e}")
        result, existing, message = RESULT_FAILED, None, str(e)

    archive_file_id = existing.id if existing is not None else None
    stored_rel = None

    if result == RESULT_MOVED:
        if config.dry_run:
            try:
                stored_rel = planner.resolve(scanned, extra_taken=reserved)
                reserved.add(stored_rel.casefold())
                virtual_hashes.add((scanned.filehash, scanned.hash_algo))
            except Exception as e:
                result, message = RESULT_FAILED, str(e)
        else:
            move = mover.plan_and_move(session, config, ingest.id, scanned, planner)
            if move.ok:
                stored_rel = move.stored_path_rel
                archive_file_id = move.archive_file.id
            elif move.conflict:
                # 予約が UNIQUE で弾かれた = 直前に同一内容が登録された
                result = RESULT_DUPLICATE
                owner = find_owning(
                    session, config.archive_id, scanned.filehash, scanned.hash_algo
                )
                archive_file_id = owner.id if owner is not None else None
                message = "Identical content was registered just before"
            else:
                result = RESULT_FAILED
                message = move.message

    _record_item(session, config, ingest, scanned, result, archive_file_id, stored_rel, message)
    return result, stored_rel, message


def _record_item(session, config, ingest, scanned, result, archive_file_id, stored_rel, message):
    """判定を ar_ingest_items に 1 行残します（dry-run では書かない）。"""
    if config.dry_run:
        return
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
