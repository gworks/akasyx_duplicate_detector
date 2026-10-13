# main.py - エントリーポイント（設計書 §6.1 / §11）
import errno
import json
import logging
import os
import stat
import sys
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy import inspect as sa_inspect

import adopt
import archives
import config as config_module
import crawler_client
import dedupe
import ingest as ingest_module
import mover
import report
import stop_watch
import verify
from config import DetectorConfig
from database import (
    META_DIRNAME,
    archive_lock,
    commit_after_interrupt,
    get_session,
    legacy_db_path,
    master_db_lock,
)
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
    COUNTS_UNAVAILABLE,
    PATH_HOLDING_STATUSES,
    STATUS_STORED,
    STATUS_UNREGISTERED,
    ArchiveFile,
    Ingest,
    IngestItem,
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
        sources = add_sources(config)
        # 複数のファイルは、投入元の確認を各ファイルの番に行う（_run_files。無い・フォルダ・重複・置き場所で断るものは、
        # その 1 件の失敗として残し、ほかは取り込む。列で待つ間に 1 件消えた・動いたために、まとめた全部を断らない。#37）
        if len(sources) == 1:
            _preflight_source(config, sources[0])
        elif not _any_importable(config, sources):
            # 取り込めるファイル（保存フォルダ・ほかの保存フォルダの外のふつうのファイル）が 1 つも無い。1 件のときと同じく事前に断る（保存フォルダの
            # 登録・ロックの前に。残りの確認は各ファイルの番に行う）
            if not any(os.path.exists(s) for s in sources):
                raise PreflightError(
                    "Source not found: none of the given files exist:\n" + "\n".join(f"  {s}" for s in sources)
                )
            raise PreflightError(
                "No file to import: none of the given sources is a file outside the archive folders:\n"
                + "\n".join(f"  {s}" for s in sources)
            )

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


def _any_importable(config: DetectorConfig, sources: list[str]) -> bool:
    """取り込めそうなファイルが 1 つでもあるか（ふつうのファイルで、取り込み先とも、ほかの保存フォルダとも重ならない）。事前に
    断るかを決めるだけの目安（各ファイルの番に確かめ直す）。"""
    lookup = archives.ArchiveLookup()  # 保存フォルダの目印の問い合わせを、ファイルの間で使い回す
    return any(
        os.path.isfile(source)
        and not is_nested(config.archive_root, source)
        and archives.enclosing_archive(source, lookup) is None
        for source in sources
    )


def add_sources(config: DetectorConfig) -> list[str]:
    """add の投入元すべて（CLI からは source_paths。1 件だけ指定する呼び出し方では source_path）。"""
    return list(config.source_paths) or [config.source_path]


def _preflight_source(config: DetectorConfig, source: str, lookup=None, st: os.stat_result | None = None) -> None:
    """add の投入元 1 件の事前チェック。lookup は保存フォルダの目印の問い合わせ（複数の投入元で使い回す）。
    st は呼び出し側が取った stat（あることと種類はそれで決める。確かめている間にフォルダに替わっても、フォルダの検査に入らない）。"""
    if st is None and not os.path.exists(source):
        raise PreflightError(f"Source not found: {source}")
    # 入れ子だと自分自身を取り込んでしまう。双方向に判定する
    if is_nested(config.archive_root, source) or is_nested(
        source, config.archive_root
    ):
        raise PreflightError(
            "The archive folder and the source are nested (or the same):\n"
            f"  archive folder: {config.archive_root}\n"
            f"  source        : {source}"
        )
    # 投入元が別の保存フォルダの中なら断る（#6）。中の保存物が自分自身の重複と判定され、
    # delete-duplicates の対象になるため。登録ではなく実物の目印で見る（移動したまま開いていない・
    # 別の正本 DB・v0.1.x の保存フォルダも含む）。投入元の中にある保存フォルダも、すぐ下で走査の前に断る
    enclosing = archives.enclosing_archive(source, lookup)
    if enclosing is not None:
        raise PreflightError(
            "The source is inside an archive folder (a folder with .akasyx/archive.id):\n"
            f"  archive folder: {enclosing}\n"
            f"  source        : {source}"
        )
    if (stat.S_ISDIR(st.st_mode) if st is not None else os.path.isdir(source)):
        # 投入元の中に保存フォルダがあれば、走査（全ハッシュ）と取り込み先の登録の前に断る
        below = archives.archives_below(source, config.follow_symlinks)
        if below:
            listed = "\n".join(f"  {d}" for d in below)
            raise PreflightError(
                "The source contains an archive folder (a folder with .akasyx/archive.id):\n"
                f"{listed}\n"
                "  Choose a source that does not include archive folders."
            )
        crawler_client.check_crawler(config)


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


def _counts_are_saved(config: DetectorConfig) -> bool:
    """この実行の判定が 1 件ずつ ar_ingest_items に保存されるか（add だけ。dry-run は書かない）。

    delete-duplicates は前の取り込みの行を書き換え、verify は ok の件数を保存しないので、DB からは数え直せない。
    """
    return config.mode == MODE_ADD and not config.dry_run


def _saved_counters(session, record: Ingest) -> dict:
    """この実行の、DB に保存された判定（ar_ingest_items）を result 別に数えます。

    移動したファイルは、保存の記録（ar_archive_files）が 1 件ごとに確定している。判定の記録はその後の commit で確定するので、
    最後の commit の最中の中断では欠けうる。移動の件数は保存の記録の方からも数え、多い方を使う。
    """
    rows = (
        session.query(IngestItem.result, func.count())
        .filter(IngestItem.ingest_id == record.id)
        .group_by(IngestItem.result)
        .all()
    )
    counters = {result: count for result, count in rows}
    stored = (
        session.query(func.count(ArchiveFile.id))
        .filter(ArchiveFile.ingest_id == record.id, ArchiveFile.status == STATUS_STORED)
        .scalar()
    )
    if stored > counters.get(RESULT_MOVED, 0):
        counters[RESULT_MOVED] = stored
    return counters


def _interrupted_before_listing(config: DetectorConfig, record: Ingest, interrupted_outside: bool) -> bool:
    """取り込みが一覧作成の段階で中断されたか（crawler のスキャンが記録される前。移動はまだ始まっていない）。"""
    return (
        interrupted_outside
        and config.mode == MODE_ADD
        and record.crawler_scan_id is None
        and os.path.isdir(config.source_path)
    )


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


def _run_record(
    session, config: DetectorConfig, archive, runner, created: list | None = None
) -> tuple[str, dict, Ingest, bool]:
    """実行の記録（ar_ingests）を 1 つ作り、runner で処理して書き戻します。

    戻り値は (実行ステータス, result 別件数, 実行の記録, ループの外で中断を受けたか)。created を渡すと、作った記録を足す
    （書き戻す前に中断が抜けたとき、呼び出し側が記録を running のまま残さないように。#37）。
    """
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
    if created is not None:
        created.append(record)  # commit の前に覚える（commit の直後の中断でも、呼び出し側が閉じられるように）
    session.commit()

    status = "failed"
    counters: dict = {}
    # 各処理のループの外（一覧作成の段階・ループの後の後片付けの commit など）で中断を受けたか（#34）
    interrupted_outside = False
    try:
        status, counters = runner(session, config, record)
    except PreflightError:
        # 途中の書き込みを捨ててから消す（記録の削除と一緒に確定させない。DB の例外で断りを置き換えない）
        session.rollback()
        session.delete(record)
        session.commit()
        if created is not None:
            created.remove(record)  # 消した記録を呼び出し側の片付けで触らない
        raise
    except KeyboardInterrupt:
        # 致命的エラーにせず中断として記録する（UI は中断として扱う。記録と食い違わせない。#34）
        status = "interrupted"
        interrupted_outside = True
        logger.warning("Interrupted by user")
    except Exception:
        logger.exception("A fatal error occurred during processing")
    finally:
        if status == "failed" or interrupted_outside:
            # 例外で抜けた場合、未コミットの変更を捨ててから実行行を書き戻す。ループの外の中断も同じ
            # （どこまで済んだか分からない途中の状態を確定させない。確定済みの記録は残る）
            session.rollback()
        elif status == "interrupted":
            # 中断が DB の読み書きの途中に入ると、セッションは rollback するまで使えない（dry-run の判定の
            # クエリなど、各処理の後片付けが立て直さない経路もある。#32）
            commit_after_interrupt(session)
        if interrupted_outside and _counts_are_saved(config):
            # 各処理が数えた件数は失われたので、DB に保存された記録から数え直す（0 件と記録しない）
            counters = _saved_counters(session, record)
            if not _interrupted_before_listing(config, record, interrupted_outside):
                # 確定していなかった判定の記録は上の rollback で捨てた（最後の commit の最中の中断など）。黙って
                # 欠けさせない（一覧作成の段階なら判定はまだ無い）
                logger.warning(
                    "Interrupted outside the processing loop: judgments not yet saved to the DB at that "
                    "point (if any) were discarded (moved files are saved; see the CSV for the judged files)"
                )
        record.status = status
        record.finished_at = datetime.now(timezone.utc)
        _apply_counters(record, counters)
        if interrupted_outside and not _counts_are_saved(config) and not _interrupted_before_listing(
            config, record, interrupted_outside
        ):
            # 0 件と読める記録を残さない（report の履歴も「分からない」と出す）
            record.stats_json = json_dumps({COUNTS_UNAVAILABLE: True})
        session.commit()
    return status, counters, record, interrupted_outside


def _run_files(session, config: DetectorConfig, archive) -> int:
    """複数のファイルを 1 回の add で順に取り込みます（#37）。

    実行の記録はファイルごと（1 件ずつ取り込んだときと同じ。履歴・delete-duplicates の掃除の範囲が変わらない）。
    出力は 1 回の実行の形（CSV・件数・サマリは 1 つ）。投入元の確認（無い・フォルダ・重複・置き場所）は各ファイルの番に
    行う。ファイルごとの事情（それらと、そのファイルの処理の予期しない例外）はそのファイルの結果の行に残し、残りを続ける。
    中断・取り込みの途中の断り（PreflightError。正本 DB・ほかの保存フォルダが読めないなど環境の断りで、残りのファイルでも
    繰り返す）・ファイルの処理の外の例外では、そこで止めてサマリを出す。

    不変条件: 取り込みを始めた（記録を作った）ファイルは、結果の行（CSV と Progress）をちょうど 1 つ持つ。ただし中断された
    ファイルは移動していないので行を持たない。始めていないファイルは「結果の行の無いファイル」で、UI（Progress の行を
    数える）と同じ数え方になる（中断したら UI はそれらを列に戻す）。
    """
    sources = add_sources(config)
    status = "completed"
    by_record: dict[int, dict] = {}  # 記録の id → そのファイルの件数（1 回の代入で入れる。中断で二重に数えない）
    turned_away = 0           # 記録を作らずに結果の行を出した（取り込みを始めなかった）ファイルの数（すべて失敗）
    fatal = 0                 # 予期しない例外で終わったファイルの数
    crashed = False           # 取り込みの途中の断り・ファイルの処理の外の例外で止まった
    refused = False           # 取り込みの途中の環境の断りで止まった
    refused_first = False     # 1 件も取り込みを始めないうちに環境の断りで止まった
    created: list[Ingest] = []  # 作った実行の記録
    counts_unavailable = False  # 件数が分からないファイルがある（dry-run などでループの外の中断）
    batch = None
    seen: dict = {}             # 取り込みを始めたファイルのキー → そのパス（同じファイルの 2 度目を見分ける）
    # 今のファイル: 番号・番が来たときの結果の行の数・作った記録の数（処理の外で止まったとき、そのファイルに行を出すため）
    turn = {"source": None, "rows": 0, "records": 0}

    def turn_away(source: str, message: str) -> None:
        nonlocal turned_away
        ingest_module.record_failed(batch, source, message)
        turned_away += 1

    def finish_without_row(source: str, record, message: str) -> dict:
        # 取り込みを始めたが結果の行が無いまま止まったファイルに、行を出して件数を返す（記録の件数もそろえる）
        stored = _stored_by(session, record)
        if stored is not None and stored.status == STATUS_STORED:
            ingest_module.record_result(batch, source, RESULT_MOVED, stored.stored_path_rel, message, stored.size)
            counts = _saved_counters(session, record) if _counts_are_saved(config) else {RESULT_MOVED: 1}
        else:
            if stored is not None and stored.status == STATUS_UNREGISTERED:
                # 片付けで移動を終えたが、同じ内容がほかの保存フォルダにあった（元の場所にはもう無い）
                message += (
                    "; the file was moved into the archive folder but left unregistered "
                    f"({stored.stored_path_rel}) because the same content is stored in another archive folder"
                )
            elif stored is not None:
                message += (
                    "; the file was moved into the archive folder but not confirmed "
                    f"({stored.stored_path_rel}); the next run finishes or undoes it"
                )
            ingest_module.record_failed(batch, source, message)
            counts = {RESULT_FAILED: 1}
        _apply_counters(record, counts)
        session.commit()
        return counts

    def forget_if_stayed(identity, source: str, counts: dict) -> None:
        # 元の場所に残った（重複・除外・取り込めなかった）なら、同じファイルの後のパスもふつうに判定する（移動の途中を
        # 片付けた後に見る）。dry-run で移動の予定なら、実際の実行と同じく移動したものとして扱う（予定を同じにする）
        if identity is None or not os.path.lexists(source):
            return
        if config.dry_run and counts.get(RESULT_MOVED):
            return
        seen.pop(identity, None)

    def recount(record) -> dict:
        # 予期しない例外で終わったファイルの件数は失われている。保存された記録から数え直し、記録にも書く（サマリ・CSV・履歴を
        # そろえる）。数え直せない（dry-run）なら分からないとする
        nonlocal counts_unavailable
        if not _counts_are_saved(config):
            counts_unavailable = True
            return {}
        saved = _saved_counters(session, record)
        _apply_counters(record, saved)
        session.commit()
        return saved

    try:
        batch = ingest_module.start_batch(config, len(sources))
        identities = _file_identities(sources)  # 同じファイルを 2 度渡されたかを、実体（デバイスと inode）で見る
        for index, source in enumerate(sources):
            turn.update(source=source, rows=batch.processed, records=len(created))
            # 自データ・保存フォルダの判定、記録の source_root・origin_root は、このファイルを投入元として扱う
            config.source_path = source
            try:
                st = _check_turn(session, config, archive, batch, source, identities[index], seen, turn_away)
            except Exception as e:
                # 番の確認の予期しない例外（読めないフォルダ・DB の一時的なエラーなど）も、そのファイルの行にして続ける。
                # DB の例外でセッションが壊れていたら立て直す（次のファイルの記録を書けるように）
                logger.exception(f"Could not check the file: {source}")
                session.rollback()
                turn_away(source, f"Could not check this file: {e}")
                continue
            if st is None:
                continue
            identity = identities[index]
            if identity is not None:
                seen[identity] = source  # 取り込みを始めるものだけ覚える（断った 1 度目のために、2 度目を断らない）
            before = batch.processed
            # このファイルが結果の行を出す前に予期しない例外で終わったら、ファイルをまたいで持つもの（dry-run の予定（保存先・内容・
            # フォルダの件数）、つながっていない保存フォルダの重複の件数）をその前に戻す（取り込まなかったファイルの分で、後の
            # ファイルの判定や最後の知らせをずらさない。行を出したなら、その行の予定は残す）
            plan = batch.save_plan(config.dry_run)
            try:
                one_status, one_counters, record, outside = _run_record(
                    session, config, archive,
                    lambda sess, cfg, rec, st=st: ingest_module.run_add(sess, cfg, rec, batch, st),
                    created,
                )
            except PreflightError as e:
                # 取り込みの途中の断り（_run_record は記録を消した）。環境の断りなので、残りのファイルでも繰り返す。ここで止める。
                # 1 件も取り込みを始めていなければ、1 件のときと同じく「断った」で終える
                logger.error(str(e))
                print(f"Error: {e}", file=sys.stderr)
                crashed = True
                refused = True
                refused_first = not created
                if batch.processed == before:
                    batch.restore_plan(plan)  # 取り込まなかったファイルの分を戻す（最後の知らせをずらさない）
                    turn_away(source, str(e).replace("\n", " "))
                break
            if outside and not _counts_are_saved(config):
                counts_unavailable = True
            if one_status == "failed":
                # そのファイルの処理の予期しない例外。そのファイルの結果にして続ける
                fatal += 1
                # 移動の途中（pending）を残していたら、次の実行の初めと同じく片付けてから結果を決める（片付けで保存済みに
                # なったなら「移動した」と出す）
                _recover_after_error(session, config)
                if batch.processed == before:
                    batch.restore_plan(plan)  # 行を出す前に止まった（取り込まなかった）ファイルの分を戻す
                    counts = finish_without_row(source, record, "A fatal error occurred while importing this file (see the log)")
                else:
                    counts = recount(record)
                by_record[record.id] = counts
                forget_if_stayed(identity, source, counts)
                continue
            by_record[record.id] = one_counters
            if one_status == "interrupted":
                status = "interrupted"
                break
            forget_if_stayed(identity, source, one_counters)
    except KeyboardInterrupt:
        # 実行の記録を作る・書き戻す最中や、ファイルの間（各ファイルの処理の外）の中断。中断したファイルは行を持たない
        status = "interrupted"
        logger.warning("Interrupted by user")
        try:
            counts_unavailable |= _close_cut_records(session, config, created, by_record)
        except (Exception, KeyboardInterrupt):
            # 片付けの最中の例外・中断。サマリは出す
            logger.exception("Could not close the run records")
    except Exception:
        # ファイルの処理の外（記録の書き戻し・結果の行の書き込み・CSV の作成など）の例外。そこで止め、取り込んだ分のサマリを
        # 出す。書き戻しきれなかった記録は失敗として閉じ、取り込みを始めていた今のファイルには行を出す
        crashed = True
        logger.exception("A fatal error occurred during processing")
        try:
            counts_unavailable |= _close_cut_records(session, config, created, by_record, "failed")
            cut = created[turn["records"]:]
            if batch is not None and cut and batch.processed == turn["rows"] and _saved(cut[-1]):
                # ファイルごとの例外のときと同じく、移動の途中（pending）を片付けてから結果を決める（例外の出た場所で変えない）
                _recover_after_error(session, config)
                by_record[cut[-1].id] = finish_without_row(
                    turn["source"], cut[-1], "Stopped by a fatal error while importing this file (see the log)"
                )
        except (Exception, KeyboardInterrupt):
            # 片付けの最中の例外・中断。サマリは出す
            logger.exception("Could not close the run records")
    finally:
        config.source_path = sources[0]
    if batch is not None:
        try:
            # 段階は、全部のファイルが最後まで済んだときだけ進める（致命的エラーのファイルがあれば、1 件のときと同じく進めない）
            ingest_module.finish_batch(session, batch, status == "completed" and not crashed and not fatal)
        except KeyboardInterrupt:
            # 後片付け（段階・つながっていない保存フォルダの知らせ）の最中の中断。取り込みは済んでいる
            status = "interrupted"
            logger.warning("Interrupted by user")
            try:
                commit_after_interrupt(session)
            except (Exception, KeyboardInterrupt):
                logger.exception("Could not save after the interrupt")
        except Exception:
            # 後片付けの例外（取り込みは済んでいる）。サマリは出す。中断した実行なら終わり方は中断のまま（UI が残りを列に戻す）
            if status != "interrupted":
                crashed = True
            logger.exception("A fatal error occurred while finishing")

    counters: dict = {RESULT_FAILED: turned_away} if turned_away else {}
    for one in by_record.values():
        for key, value in one.items():
            counters[key] = counters.get(key, 0) + value
    summary = _summarize(config, counters)
    # 始めていない = 結果の行の無いファイル（UI と同じ数え方）
    left = len(sources) - (batch.processed if batch is not None else 0)
    if status == "interrupted":
        not_started = f"; {left} files were not started" if left else ""
        summary += f" * run was interrupted (summary covers processed files only{not_started})"
        if counts_unavailable:
            summary += " * counts of the interrupted file are not available (see the CSV)"
    elif counts_unavailable:
        summary += " * counts of a file that stopped with an error are not available (see the CSV)"
    if fatal:
        summary += f" * a fatal error occurred in {fatal} files (see the log; the other files were processed)"
    if crashed:
        why = "stopped because the run was refused (see the error)" if refused else "stopped due to a fatal error"
        summary += f" * {why}" + (f" ({left} files were not started)" if left else "")
    if config.dry_run:
        summary += " * dry-run (nothing was changed)"
    logger.info(summary)
    print(summary)
    if crashed and refused_first:
        return EXIT_REJECTED
    # 中断で終わったなら、ファイルごとの致命的エラーは失敗あり（その行がある）として返す（終了コードで実行全体の致命的
    # エラーと取り違えない。UI は中断として残りを列に戻す）
    if crashed or (fatal and status != "interrupted"):
        return EXIT_FATAL
    if fatal or counters.get(RESULT_FAILED, 0):
        return EXIT_HAS_FAILURES
    return EXIT_OK


def _saved(record) -> bool:
    """記録が DB に保存されているか（commit が失敗すると、rollback の後も id が残ることがある）。"""
    state = sa_inspect(record)
    return record.id is not None and (state.persistent or state.detached)


def _check_turn(session, config: DetectorConfig, archive, batch, source: str, identity, seen: dict, turn_away):
    """ファイルの番の確認（同じファイルの 2 度目・無い・フォルダ・置き場所）。取り込むならその stat、断ったら None
    （turn_away で結果の行を出した）。"""
    if identity is not None and identity in seen:
        # 2 度目は失敗（1 度目が移動した後なので、無いとは言わない）
        turn_away(source, f"The same file is given more than once (also as {seen[identity]})")
        return None
    # stat はここで 1 度だけ取り、取り込みはそれを使う（この後に消えても、その 1 件の結果になる）
    try:
        st = os.stat(source)
    except OSError as e:
        turn_away(source, _missing_message(session, archive, source, e))
        return None
    if stat.S_ISDIR(st.st_mode):
        turn_away(source, "Several sources can be given only for files (give one folder per run)")
        return None
    if not stat.S_ISREG(st.st_mode):
        turn_away(source, "Not a regular file")
        return None
    try:
        # 置き場所（保存フォルダとの入れ子・保存フォルダの中）を確かめてから取り込む（あることは上の stat で分かっている）
        _preflight_source(config, source, batch.lookup, st)
    except PreflightError as e:
        # stat の後に消えたなら、無いファイルと同じ理由にする（前に取り込んだものの案内を出す）
        gone = not os.path.lexists(source)
        turn_away(source, _missing_message(session, archive, source, OSError(2, "No such file or directory"))
                  if gone else str(e).replace("\n", " "))
        return None
    return st


def _recover_after_error(session, config: DetectorConfig) -> None:
    """予期しない例外で終わったファイルが保存フォルダに移動の途中（pending）を残したら、次の実行の初めと同じく片付けます
    （後のファイルの保存先・判定を、途中の記録の上で決めない。#37）。"""
    try:
        session.rollback()
        mover.recover_pending(session, config)
    except Exception:
        logger.exception("Could not recover the pending record of the failed file (the next run recovers it)")


def _file_identities(sources: list[str]) -> list:
    """各ファイルを同じパスと見なすためのキー（同じ実体で、実体の位置が大文字小文字を除いて同じ）。無いものは None。

    同じファイルを 2 度渡されたかを見る（大文字小文字違い・リンク経由の同じパス）。区別するボリュームの別のファイルは実体が
    違い、ハードリンク（同じ実体の別のパス）はパスが違うので、どちらも別に扱う（ハードリンクは 1 件ずつのときと同じく重複と
    判定される）。最初にまとめて取る（1 度目を移動した後では、2 度目を stat できない）。
    """
    identities = []
    for source in sources:
        try:
            st = os.stat(source)
        except OSError:
            identities.append(None)
            continue
        # パスは path_key（実体の位置。macOS・Windows は大文字小文字をそろえる）で比べる
        identities.append((st.st_dev, st.st_ino, path_key(source)))
    return identities


def _stored_by(session, record) -> ArchiveFile | None:
    """その実行で保存フォルダへ移したファイルの記録（保存済み・移動の途中＝pending・片付けで未登録になった）。無ければ None。"""
    return (
        session.query(ArchiveFile)
        .filter(ArchiveFile.ingest_id == record.id, ArchiveFile.status.in_(PATH_HOLDING_STATUSES))
        .order_by(ArchiveFile.id.desc())
        .first()
    )


def _missing_message(session, archive, source: str, error: OSError) -> str:
    """順番が来たときに無いファイルの理由。この保存フォルダへ取り込み済みなら、そう書く（中断の直前に取り込み終えたファイルが
    列に戻された場合など。黙って外さず、失敗とも取り違えさせない。#37）。読めないだけ（権限など）なら DB は引かない。"""
    if error.errno != errno.ENOENT:
        return f"Source not found or not readable: {error.strerror or error}"
    stored = (
        session.query(ArchiveFile)
        .filter(
            ArchiveFile.origin_path_abs == source,
            ArchiveFile.archive_id == archive.id,
            ArchiveFile.status == STATUS_STORED,
        )
        .order_by(ArchiveFile.id.desc())
        .first()
    )
    if stored is not None:
        # 同じパスの別のファイル（前に取り込んだもの）かもしれないので、言い切らない
        return (
            "No longer at this path; a file from this path was imported earlier as "
            f"{stored.stored_path_rel} (check that it is the same file)"
        )
    return f"Source not found or not readable: {error.strerror or error}"


def _close_cut_records(
    session, config: DetectorConfig, created: list, by_record: dict, status: str = "interrupted"
) -> bool:
    """各ファイルの処理の外で止まったとき、件数を入れていない記録を片付けます（#37）。

    書き戻しきれなかった記録（running のまま）は status（中断・失敗）として閉じ、件数は DB に保存された記録から数え直す
    （_run_record と同じ）。書き戻し終えていた記録は、記録した件数を使う。どちらも by_record に入れる。数え直せない
    （dry-run）なら件数が分からないと記録し、True を返す（サマリに出す）。途中の書き込みは捨てる。
    """
    session.rollback()
    unavailable = False
    for record in created:
        if not _saved(record) or record.id in by_record:
            continue  # 保存されなかった（commit の前・最中の中断や失敗）・件数は入れ済み
        if record.status != "running":
            saved = json.loads(record.stats_json) if record.stats_json else {}
            if not isinstance(saved, dict) or saved.get(COUNTS_UNAVAILABLE):
                unavailable = True
                continue
            by_record[record.id] = saved
            continue
        record.status = status
        record.finished_at = datetime.now(timezone.utc)
        if _counts_are_saved(config):
            saved = _saved_counters(session, record)
            _apply_counters(record, saved)
            by_record[record.id] = saved
        else:
            record.stats_json = json_dumps({COUNTS_UNAVAILABLE: True})
            unavailable = True
    commit_after_interrupt(session)
    return unavailable


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

            if config.mode == MODE_ADD and len(add_sources(config)) > 1:
                return _run_files(session, config, archive)

            status, counters, record, interrupted_outside = _run_record(
                session, config, archive, RUNNERS[config.mode]
            )

            if config.mode == MODE_REPORT:
                # report は読み取り専用。集計はレポート本文に出ているのでサマリは要らない。中断したときは、本文が途中までで
                # あることだけ出す（#34）
                if status == "interrupted":
                    print("* report was interrupted (the output may be incomplete)")
                return EXIT_OK

            summary = _summarize(config, counters)
            if status == "interrupted" and _interrupted_before_listing(config, record, interrupted_outside):
                summary += " * run was interrupted while listing the source (no files were moved)"
            elif interrupted_outside and _counts_are_saved(config):
                summary += " * run was interrupted (summary covers the results saved to the DB)"
            elif interrupted_outside:
                # 件数を DB から数え直せない（delete-duplicates・verify・dry-run）。0 件と読める形で出さない
                summary = "[Summary] counts are not available * run was interrupted (see the CSV and the log)"
            elif status == "interrupted":
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

    # UI が停止ファイルで中断を頼めるようにする（Windows では SIGINT が detector に届かない。#32）。中断を受け止める
    # try の直前に立て、終わったら止める（その外で KeyboardInterrupt を起こさない）
    watch = stop_watch.start_from_env()
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
    finally:
        if watch is not None:
            watch.stop()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # 実行が終わる間際に届いた中断（停止ファイルの見張りが止まる直前に起こしたものなど）。中断として終わる
        sys.exit(EXIT_FATAL)
