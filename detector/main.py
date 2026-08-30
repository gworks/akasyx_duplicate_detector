# main.py - エントリーポイント（設計書 §6.1 / §11）
import logging
import os
import sys
from datetime import datetime, timezone

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
    if not os.path.isdir(config.archive_root):
        raise PreflightError(f"保存用フォルダがありません: {config.archive_root}")
    if not os.access(config.archive_root, os.W_OK):
        raise PreflightError(f"保存用フォルダに書き込めません: {config.archive_root}")

    if config.mode == MODE_ADD:
        if not os.path.exists(config.source_path):
            raise PreflightError(f"投入元がありません: {config.source_path}")
        # 入れ子だと自分自身を取り込んでしまう。双方向に判定する
        if is_nested(config.archive_root, config.source_path) or is_nested(
            config.source_path, config.archive_root
        ):
            raise PreflightError(
                "保存用フォルダと投入元が入れ子（または同一）です:\n"
                f"  保存用フォルダ: {config.archive_root}\n"
                f"  投入元        : {config.source_path}"
            )
        if os.path.isdir(config.source_path):
            crawler_client.resolve_crawler_repo(config.crawler_repo)

    if config.mode == MODE_VERIFY:
        crawler_client.resolve_crawler_repo(config.crawler_repo)

    if config.mode == MODE_DELETE_DUPLICATES and config.trash_dir:
        if is_nested(config.archive_root, config.trash_dir):
            raise PreflightError(
                f"退避先が保存用フォルダの配下です: {config.trash_dir}"
            )
        os.makedirs(config.trash_dir, exist_ok=True)


def _summarize(config: DetectorConfig, counters: dict) -> str:
    """crawler の [実行サマリ] 形式を踏襲した1行サマリ（設計書 §7.5）。"""
    if config.mode == MODE_ADD:
        return (
            f"[実行サマリ] 移動: {counters.get(RESULT_MOVED, 0)}件, "
            f"重複(据え置き): {counters.get(RESULT_DUPLICATE, 0)}件, "
            f"空ファイル: {counters.get(RESULT_SKIPPED_EMPTY, 0)}件, "
            f"ハッシュ不明: {counters.get(RESULT_SKIPPED_NOHASH, 0)}件, "
            f"失敗: {counters.get(RESULT_FAILED, 0)}件"
        )
    parts = ", ".join(f"{k}: {v}件" for k, v in sorted(counters.items()))
    return f"[実行サマリ] {parts or '対象なし'}"


def _apply_counters(record: Ingest, counters: dict) -> None:
    record.total = sum(counters.values())
    record.moved = counters.get(RESULT_MOVED, 0)
    record.duplicated = counters.get(RESULT_DUPLICATE, 0)
    record.skipped = counters.get(RESULT_SKIPPED_EMPTY, 0) + counters.get(
        RESULT_SKIPPED_NOHASH, 0
    )
    record.failed = counters.get(RESULT_FAILED, 0) + counters.get("failed", 0)
    record.stats_json = json_dumps(counters)


def run(config: DetectorConfig) -> int:
    """1回の実行。戻り値はプロセス終了コード。"""
    preflight(config)

    with archive_lock(config.archive_root):
        session, _engine = get_session(config.archive_root)
        try:
            # 前回の中断分を先に片付ける（どのサブコマンドでも実施 — 設計書 §7.4）
            mover.recover_pending(session, config)

            record = Ingest(
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
                logger.exception("処理中に致命的なエラーが発生しました")
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
                summary += " ※実行は中断されました（サマリは処理済みぶんのみ）"
            elif status == "failed":
                summary += " ※致命的なエラーにより停止しました"
            if config.dry_run:
                summary += " ※dry-run（何も変更していません）"
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
    logger.info(f"{config.mode} 開始: 保存用フォルダ={config.archive_root}")

    try:
        return run(config)
    except PreflightError as e:
        logger.error(str(e))
        print(f"エラー: {e}", file=sys.stderr)
        return EXIT_REJECTED
    except DetectorError as e:
        logger.error(str(e))
        print(f"エラー: {e}", file=sys.stderr)
        return EXIT_FATAL
    except KeyboardInterrupt:
        logger.warning("ユーザー操作により中断されました")
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())
