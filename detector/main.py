# main.py - エントリーポイント（設計書 §6.1 / §11）
import logging
import os
import sys
from datetime import datetime, timezone

import adopt
import archives
import config as config_module
import crawler_client
import dedupe
import ingest as ingest_module
import mover
import report
import verify
from config import DetectorConfig
from database import META_DIRNAME, archive_lock, get_session, legacy_db_path, master_db_lock
from errors import DetectorError, PreflightError
from models import (
    MODE_ADD,
    MODE_ADOPT,
    MODE_ARCHIVES,
    MODE_DELETE_DUPLICATES,
    MODE_REPORT,
    MODE_VERIFY,
    RESULT_DUPLICATE,
    RESULT_FAILED,
    RESULT_MOVED,
    RESULT_SKIPPED_EMPTY,
    RESULT_SKIPPED_NOHASH,
    RESULT_SKIPPED_IN_ARCHIVE,
    RESULT_SKIPPED_OWN_DATA,
    Ingest,
)
from utl.helpers import is_nested, json_dumps, path_key

logger = logging.getLogger(__name__)

# 終了コード（設計書 §11）
EXIT_OK = 0
EXIT_FATAL = 1
EXIT_HAS_FAILURES = 2
EXIT_REJECTED = 3

RUNNERS = {
    MODE_ADD: ingest_module.run_add,
    MODE_ADOPT: adopt.run_adopt,
    MODE_VERIFY: verify.run_verify,
    MODE_DELETE_DUPLICATES: dedupe.run_delete_duplicates,
    MODE_REPORT: report.run_report,
}


def check_own_data_placement(config: DetectorConfig) -> None:
    """detector 自身のデータを保存フォルダと重ねない（設計書 §12）。何も書き込まずに判定する。

    main() はログのフォルダ作成・ログファイルの初期化より前にこれを呼ぶ（断る前に保存フォルダの中へ
    ログを作ると、それ自体を verify が保存物として拾ってしまう）。
    """
    if config.archive_root:
        _check_overlap_with_archive(config)
    # 保存フォルダと重なる場合は上の具体的な案内を先に出す
    # 正本 DB が v0.1.x の保存フォルダの目印（<フォルダ>/.akasyx/archive.db）と同じ場所になるなら断る。
    # 判定は archives.has_archive_marker と同じ規則（ファイル名・大文字小文字の扱い）で、親がシンボリックリンクでも
    # 実体の位置で見る。目印にならない名前（.akasyx/custom.db 等）は断らない
    if any(
        path_key(legacy_db_path(os.path.dirname(os.path.dirname(p))), real=False) == path_key(p, real=False)
        for p in (os.path.abspath(config.archive_db), os.path.realpath(config.archive_db))
    ):
        raise PreflightError(
            f"The master DB cannot be placed at <folder>/{META_DIRNAME}/archive.db "
            f"(the marker of a v0.1.x archive folder): {config.archive_db}\n"
            "  Choose another location or file name (--archive-db)."
        )


def _check_overlap_with_archive(config: DetectorConfig) -> None:
    # 重なると verify が更新中の UI データ・DB・ログを保存物として記録し、
    # 正本 DB が .akasyx/archive.db だと旧 DB として取り込んで改名してしまう
    for _kind, label, path, movable in config_module.own_data_locations(config):
        if movable:
            if is_nested(config.archive_root, path):
                raise PreflightError(
                    f"The detector's {label} is inside the archive folder:\n"
                    f"  archive folder: {config.archive_root}\n"
                    f"  {label:<14}: {path}\n"
                    "  Put it outside the archive folder (--archive-db / --db-dir / --log-dir)."
                )
        elif is_nested(config.archive_root, path) or is_nested(path, config.archive_root):
            # データフォルダはオプションで移せないので、保存フォルダの選び直しを案内する。
            # 先頭で見るのは、既定のままホームを選んだ場合に「オプションで外へ」という直らない案内を
            # 出さないため。中に置くのも断る（データフォルダを消すと保存物まで消える）
            raise PreflightError(
                "The archive folder overlaps the detector's data folder "
                "(contains it, is inside it, or is the same):\n"
                f"  archive folder: {config.archive_root}\n"
                f"  data folder   : {path}\n"
                "  Choose a dedicated folder outside the data folder as the archive folder\n"
                "  (not your home folder)."
            )


def preflight(config: DetectorConfig) -> None:
    """実行前チェック。1件も処理しないまま断るケースをここに集める（設計書 §12）。"""
    if not config.archive_root:
        raise PreflightError("No archive folder specified")
    if not os.path.isdir(config.archive_root):
        raise PreflightError(f"Archive folder not found: {config.archive_root}")
    check_own_data_placement(config)
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
        # 投入元が別の保存フォルダの中なら断る（#6）。中の保存物が自分自身の重複と判定され、
        # delete-duplicates の対象になるため。登録ではなく実物の目印で見る（移動したまま開いていない・
        # 別の正本 DB・v0.1.x の保存フォルダも含む）。投入元の中にある保存フォルダも、すぐ下で走査の前に断る
        enclosing = archives.enclosing_archive(config.source_path)
        if enclosing is not None:
            raise PreflightError(
                "The source is inside an archive folder (a folder with .akasyx/archive.id):\n"
                f"  archive folder: {enclosing}\n"
                f"  source        : {config.source_path}"
            )
        if os.path.isdir(config.source_path):
            # 投入元の中に保存フォルダがあれば、走査（全ハッシュ）と取り込み先の登録の前に断る
            below = archives.archives_below(config.source_path, config.follow_symlinks)
            if below:
                listed = "\n".join(f"  {d}" for d in below)
                raise PreflightError(
                    "The source contains an archive folder (a folder with .akasyx/archive.id):\n"
                    f"{listed}\n"
                    "  Choose a source that does not include archive folders."
                )
            crawler_client.check_crawler(config)

    if config.mode in (MODE_VERIFY, MODE_ADOPT):
        crawler_client.check_crawler(config)

    if config.mode == MODE_DELETE_DUPLICATES and config.trash_dir:
        if is_nested(config.archive_root, config.trash_dir):
            raise PreflightError(
                f"The trash directory is inside the archive folder: {config.trash_dir}"
            )
        # 別の保存フォルダの中に退避すると、その保存フォルダの verify が未登録の保存物として拾う（#6）
        enclosing = archives.enclosing_archive(config.trash_dir)
        if enclosing is not None:
            raise PreflightError(
                "The trash directory is inside an archive folder:\n"
                f"  archive folder : {enclosing}\n"
                f"  trash directory: {config.trash_dir}"
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
            + (
                f", own data skipped: {counters[RESULT_SKIPPED_OWN_DATA]}"
                if counters.get(RESULT_SKIPPED_OWN_DATA)
                else ""
            )
            + (
                f", inside an archive folder (skipped): {counters[RESULT_SKIPPED_IN_ARCHIVE]}"
                if counters.get(RESULT_SKIPPED_IN_ARCHIVE)
                else ""
            )
        )
    parts = ", ".join(f"{k}: {v}" for k, v in sorted(counters.items()))
    return f"[Summary] {parts or 'nothing to process'}"


def _apply_counters(record: Ingest, counters: dict) -> None:
    record.total = sum(counters.values())
    record.moved = counters.get(RESULT_MOVED, 0)
    record.duplicated = counters.get(RESULT_DUPLICATE, 0)
    record.skipped = (
        counters.get(RESULT_SKIPPED_EMPTY, 0)
        + counters.get(RESULT_SKIPPED_NOHASH, 0)
        + counters.get(RESULT_SKIPPED_OWN_DATA, 0)
        + counters.get(RESULT_SKIPPED_IN_ARCHIVE, 0)
    )
    # RESULT_FAILED と delete-duplicates の "failed" は同じ文字列。2 回足すと件数が倍になる
    record.failed = counters.get(RESULT_FAILED, 0)
    record.stats_json = json_dumps(counters)


def run_archives(config: DetectorConfig) -> int:
    """登録済みの保存フォルダ一覧（保存フォルダの指定もロックも要らない）。--forget は登録を外す。

    正本 DB が無ければ作らない（一覧は読むだけ。UI から入力途中・打ち間違いのパスで呼ばれても DB やフォルダを残さない）。
    """
    if not os.path.isfile(config.archive_db):
        if config.forget_id is not None:
            raise PreflightError(f"Master DB not found: {config.archive_db}")
        if config.list_json:
            # 初回起動の「まだ無い」を、読めなかったこととは区別して返す
            print(json_dumps({"master_db": config.archive_db, "db_exists": False, "archives": []}))
        else:
            print(f"Master DB not found: {config.archive_db} (no archive folders are registered yet)")
        return EXIT_OK
    if config.forget_id is not None:
        with master_db_lock(config.archive_db):
            session, _engine = get_session(config.archive_db)
            try:
                archives.forget_archive(session, config.forget_id, expect_uid=config.expect_uid)
                return EXIT_OK
            finally:
                session.close()
    session, _engine = get_session(config.archive_db)
    try:
        rows = archives.list_archives(session)
        if config.list_json:
            _print_archives_json(session, config, rows)
            return EXIT_OK
        print(f"Master DB: {config.archive_db}")
        print("")
        print(f"■ Registered archive folders: {len(rows)}")
        for a, count, size in rows:
            exists = "" if os.path.isdir(a.root_abs) else "  * not found at this path"
            print(f"  #{a.id:<4} {a.root_abs}{exists}")
            used = _aware(a.last_used_at)
            used = f"{used.astimezone():%Y-%m-%d %H:%M}" if used else None
            print(f"        stored {count} files / {size} bytes / uid {a.uid} / last used {used or '-'}")
        return EXIT_OK
    finally:
        session.close()


def _aware(value: datetime | None) -> datetime | None:
    """DB の日時（タイムゾーン無しの UTC）を UTC 付きにします。"""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _print_archives_json(session, config: DetectorConfig, rows) -> None:
    """archives --json: UI が読む一覧（#11）。stdout には JSON だけを出す（ログは stderr）。"""
    forgotten = archives.forgotten_counts(session)
    pending = archives.pending_adoption_ids(session)
    out = {
        "master_db": config.archive_db,
        "db_exists": True,
        "archives": [
            {
                "id": a.id,
                "root": a.root_abs,
                "uid": a.uid,
                # テキストの一覧と同じ目安（forget 自体は archives.forget_archive が判定する）
                "present": os.path.isdir(a.root_abs),
                "stored_files": count,
                "stored_bytes": size,
                "forgotten_files": forgotten.get(a.id, 0),
                "pending_adoption": a.id in pending,
                "last_used_at": _aware(a.last_used_at),
            }
            for a, count, size in rows
        ],
    }
    print(json_dumps(out))


def run(config: DetectorConfig) -> int:
    """1回の実行。戻り値はプロセス終了コード。"""
    if config.mode == MODE_ARCHIVES:
        return run_archives(config)

    preflight(config)

    # 重複判定は全保存フォルダ共通なので、同じ正本 DB を使う実行は保存フォルダが違っても 1 本だけ（#6）
    with master_db_lock(config.archive_db), archive_lock(config.archive_root):
        session, _engine = get_session(config.archive_db)
        try:
            archive = archives.resolve_archive(
                session, config.archive_root, adopting=config.mode == MODE_ADOPT
            )
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
    try:
        # フォルダやログを作る前に判定する（断る前に保存フォルダの中へ書き込まない）
        check_own_data_placement(config)
    except PreflightError as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_REJECTED
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
