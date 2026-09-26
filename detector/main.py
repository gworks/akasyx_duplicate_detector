# main.py - エントリーポイント（設計書 §6.1 / §11）
import logging
import os
import sys
from datetime import datetime, timezone

import archives
import config as config_module
import crawler_client
import dedupe
import ingest as ingest_module
import mover
import report
import verify
from config import DetectorConfig
from database import archive_lock, get_session
from errors import DetectorError, PreflightError
from models import (
    MODE_ADD,
    MODE_ARCHIVES,
    MODE_DELETE_DUPLICATES,
    MODE_REPORT,
    MODE_VERIFY,
    RESULT_DUPLICATE,
    RESULT_FAILED,
    RESULT_MOVED,
    RESULT_SKIPPED_EMPTY,
    RESULT_SKIPPED_NOHASH,
    Ingest,
)
from utl.helpers import is_nested, json_dumps

logger = logging.getLogger(__name__)

# 終了コード（設計書 §11）
EXIT_OK = 0
EXIT_FATAL = 1
EXIT_HAS_FAILURES = 2
EXIT_REJECTED = 3

RUNNERS = {
    MODE_ADD: ingest_module.run_add,
    MODE_VERIFY: verify.run_verify,
    MODE_DELETE_DUPLICATES: dedupe.run_delete_duplicates,
    MODE_REPORT: report.run_report,
}


def preflight(config: DetectorConfig) -> None:
    """実行前チェック。1件も処理しないまま断るケースをここに集める（設計書 §12）。"""
    if not config.archive_root:
        raise PreflightError("No archive folder specified")
    if not os.path.isdir(config.archive_root):
        raise PreflightError(f"Archive folder not found: {config.archive_root}")
    # 正本 DB が保存フォルダの中にあると、.akasyx/archive.db なら旧 DB として取り込んで
    # 改名してしまい（次回は空の DB から始まる）、それ以外の場所でも verify が拾ってしまう
    if is_nested(config.archive_root, config.archive_db):
        raise PreflightError(
            "The master DB is inside the archive folder:\n"
            f"  archive folder: {config.archive_root}\n"
            f"  master DB     : {config.archive_db}\n"
            "  Put the master DB outside the archive folder (--archive-db)."
        )
    if not os.access(config.archive_root, os.W_OK):
        raise PreflightError(f"Archive folder is not writable: {config.archive_root}")

    if config.mode == MODE_ADD:
        if not os.path.exists(config.source_path):
            raise PreflightError(f"Source not found: {config.source_path}")
        # 入れ子だと自分自身を取り込んでしまう。双方向に判定する
        if is_nested(config.archive_root, config.source_path) or is_nested(
            config.source_path, config.archive_root
        ):
            raise PreflightError(
                "The archive folder and the source are nested (or the same):\n"
                f"  archive folder: {config.archive_root}\n"
                f"  source        : {config.source_path}"
            )
        if os.path.isdir(config.source_path):
            crawler_client.check_crawler(config)

    if config.mode == MODE_VERIFY:
        crawler_client.check_crawler(config)

    if config.mode == MODE_DELETE_DUPLICATES and config.trash_dir:
        if is_nested(config.archive_root, config.trash_dir):
            raise PreflightError(
                f"The trash directory is inside the archive folder: {config.trash_dir}"
            )
        os.makedirs(config.trash_dir, exist_ok=True)


def _summarize(config: DetectorConfig, counters: dict) -> str:
    """crawler の [実行サマリ] 形式を踏襲した1行サマリ（設計書 §7.5）。"""
    if config.mode == MODE_ADD:
        return (
            f"[Summary] moved: {counters.get(RESULT_MOVED, 0)}, "
            f"duplicates (left in place): {counters.get(RESULT_DUPLICATE, 0)}, "
            f"empty files: {counters.get(RESULT_SKIPPED_EMPTY, 0)}, "
            f"no hash: {counters.get(RESULT_SKIPPED_NOHASH, 0)}, "
            f"failed: {counters.get(RESULT_FAILED, 0)}"
        )
    parts = ", ".join(f"{k}: {v}" for k, v in sorted(counters.items()))
    return f"[Summary] {parts or 'nothing to process'}"


def _apply_counters(record: Ingest, counters: dict) -> None:
    record.total = sum(counters.values())
    record.moved = counters.get(RESULT_MOVED, 0)
    record.duplicated = counters.get(RESULT_DUPLICATE, 0)
    record.skipped = counters.get(RESULT_SKIPPED_EMPTY, 0) + counters.get(
        RESULT_SKIPPED_NOHASH, 0
    )
    record.failed = counters.get(RESULT_FAILED, 0) + counters.get("failed", 0)
    record.stats_json = json_dumps(counters)


def run_archives(config: DetectorConfig) -> int:
    """登録済みの保存フォルダ一覧（保存フォルダの指定もロックも要らない）。"""
    session, _engine = get_session(config.archive_db)
    try:
        rows = archives.list_archives(session)
        print(f"Master DB: {config.archive_db}")
        print("")
        print(f"■ Registered archive folders: {len(rows)}")
        for a, count, size in rows:
            exists = "" if os.path.isdir(a.root_abs) else "  * not found at this path"
            print(f"  #{a.id:<4} {a.root_abs}{exists}")
            used = a.last_used_at
            if used is not None:
                if used.tzinfo is None:
                    used = used.replace(tzinfo=timezone.utc)
                used = f"{used.astimezone():%Y-%m-%d %H:%M}"
            print(f"        stored {count} files / {size} bytes / uid {a.uid} / last used {used or '-'}")
        return EXIT_OK
    finally:
        session.close()


def run(config: DetectorConfig) -> int:
    """1回の実行。戻り値はプロセス終了コード。"""
    if config.mode == MODE_ARCHIVES:
        return run_archives(config)

    preflight(config)

    with archive_lock(config.archive_root):
        session, _engine = get_session(config.archive_db)
        try:
            archive = archives.resolve_archive(session, config.archive_root)
            config.archive_id = archive.id

            # 前回の中断分を先に片付ける（どのサブコマンドでも実施 — 設計書 §7.4）
            mover.recover_pending(session, config)

            record = Ingest(
                archive_id=archive.id,
                mode=config.mode,
                source_root=config.source_path,
                archive_root=config.archive_root,
                dry_run=1 if config.dry_run else 0,
                status="running",
                app_version=config_module.app_version(),
                config_json=json_dumps(config_module.config_snapshot(config)),
            )
            session.add(record)
            session.commit()

            status = "failed"
            counters: dict = {}
            try:
                status, counters = RUNNERS[config.mode](session, config, record)
            except PreflightError:
                session.delete(record)
                session.commit()
                raise
            except Exception:
                logger.exception("A fatal error occurred during processing")
            finally:
                if status == "failed":
                    # 例外で抜けた場合、未コミットの変更を捨ててから実行行を書き戻す
                    session.rollback()
                record.status = status
                record.finished_at = datetime.now(timezone.utc)
                _apply_counters(record, counters)
                session.commit()

            if config.mode == MODE_REPORT:
                # report は読み取り専用。集計はレポート本文に出ているのでサマリは要らない
                return EXIT_OK

            summary = _summarize(config, counters)
            if status == "interrupted":
                summary += " * run was interrupted (summary covers processed files only)"
            elif status == "failed":
                summary += " * stopped due to a fatal error"
            if config.dry_run:
                summary += " * dry-run (nothing was changed)"
            logger.info(summary)
            print(summary)

            if status == "failed":
                return EXIT_FATAL
            if record.failed:
                return EXIT_HAS_FAILURES
            return EXIT_OK
        finally:
            session.close()


def main(argv: list[str] | None = None) -> int:
    config = config_module.parse_arguments(argv)
    config_module.setup_directories(config)
    config_module.setup_logging(config)
    logger.info(
        f"{config.mode} started: archive folder={config.archive_root or '-'} / master DB={config.archive_db}"
    )

    try:
        return run(config)
    except PreflightError as e:
        logger.error(str(e))
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_REJECTED
    except DetectorError as e:
        logger.error(str(e))
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_FATAL
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())
