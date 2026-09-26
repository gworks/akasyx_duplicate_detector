# archives.py - 保存フォルダの登録・識別・旧 DB の取り込み（設計書 §4 / §8。v0.2.0）
#
# 正本 DB は 1 つで、保存フォルダは ar_archives の行として区別する。
# 識別は uid（保存フォルダ内の .akasyx/archive.id にも書く）で行い、絶対パスは「現在の場所」。
import logging
import os
import sqlite3
import uuid
from datetime import datetime

from config import OS_JUNK_FILES
from database import META_DIRNAME, archive_id_path, legacy_db_path, meta_dir, tmp_dir
from errors import PreflightError
from models import Archive, ArchiveFile, Base, Ingest, IngestItem, LegacyImport, utcnow

logger = logging.getLogger(__name__)


def _read_uid(archive_root: str) -> str | None:
    """`.akasyx/archive.id` の uid。ファイルが無ければ None。

    読めない（権限・I/O エラー）のは「無い」と区別して断る。無いとみなすと別の保存フォルダとして
    登録したり識別子を書き直したりして、既にある内容と同じファイルを取り込んでしまう。
    """
    path = archive_id_path(archive_root)
    try:
        with open(path, encoding="utf-8") as f:
            uid = f.read().strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise PreflightError(f"Cannot read the archive folder ID: {path}: {e}") from e
    return uid or None


def _write_uid(archive_root: str, uid: str) -> None:
    os.makedirs(meta_dir(archive_root), exist_ok=True)
    with open(archive_id_path(archive_root), "w", encoding="utf-8") as f:
        f.write(uid + "\n")


def _has_stored_content(root: str) -> bool:
    """保存フォルダに実ファイルがあるか（.akasyx/ と OS のゴミファイルは数えない）。

    読めない配下があれば「空」とは言えないので断る（os.walk は既定で黙って飛ばす）。
    """

    def _unreadable(e: OSError):
        raise PreflightError(f"Cannot read part of the archive folder: {e.filename}: {e}") from e

    for dirpath, dirs, names in os.walk(root, onerror=_unreadable):
        if dirpath == root:
            dirs[:] = [d for d in dirs if d != META_DIRNAME]
        if any(n not in OS_JUNK_FILES for n in names):
            return True
    return False


def _is_live_copy_source(row: Archive, root: str) -> bool:
    """登録上の場所に、同じ uid の保存フォルダがまだ残っているか（= root はその複製）。

    シンボリックリンクや大文字小文字違いで同じ場所を指しているだけなら複製ではない。
    """
    old = row.root_abs
    if not os.path.isdir(old):
        return False
    if os.path.realpath(old) == os.path.realpath(root) or os.path.samefile(old, root):
        return False
    try:
        return _read_uid(old) == row.uid
    except PreflightError as e:
        # もう使っていない場所の不調で、移動した保存フォルダまで開けなくしない
        logger.warning(f"Could not check the previous location; treating as moved: {e}")
        return False


def resolve_archive(session, archive_root: str) -> Archive:
    """保存フォルダに対応する ar_archives 行を返します（無ければ登録）。

    1. `.akasyx/archive.id` があれば uid で探す。見つかれば、パスが変わっていれば更新する
       （保存フォルダを移動・改名した場合）
    2. 無ければ絶対パスで探す（識別子ファイルだけ消えた場合）。見つかれば識別子を書き戻す
    3. どちらも無ければ新規登録し、識別子を書く。v0.1.x の `.akasyx/archive.db` が
       残っていればその内容を取り込む（旧 DB は `.migrated-<日時>` に改名して残す）

    次の 2 つは PreflightError で断る（どのサブコマンドでも。登録を作らないため）:
    - 識別子があるのに DB に無く、中にファイルがある: 別の正本 DB で使われていた保存フォルダを
      空の登録として扱うと、既にある内容と同じファイルまで取り込んで重複を作る。
      add 以外で登録を許すと、その後の add が「登録済み」として素通りするので全コマンドで断る
    - 同じ uid の元の保存フォルダがまだ別の場所にある: フォルダの複製。移動とみなすと
      2 つのフォルダが 1 つの登録を交互に書き換える
    """
    root = os.path.abspath(archive_root)
    os.makedirs(tmp_dir(root), exist_ok=True)
    uid = _read_uid(root)

    row = None
    if uid:
        row = session.query(Archive).filter(Archive.uid == uid).first()
    if row is None:
        row = session.query(Archive).filter(Archive.root_abs == root).first()
        if row is not None and uid and row.uid != uid:
            row = None  # 同じ場所に別の保存フォルダが置かれた。パス一致では同一視しない

    if row is None and uid and _has_stored_content(root):
        raise PreflightError(
            "This archive folder is not registered in the master DB, but it already contains files:\n"
            f"  archive folder: {root} (ID {uid})\n"
            "  It may have been used with a different master DB (e.g. development vs. packaged app,\n"
            "  another computer). Using it as a new archive could store duplicates of files\n"
            "  already in it. Specify the master DB it was used with via --archive-db."
        )
    if row is None and uid:
        logger.warning(f"ID {uid} is not in the DB; the archive folder is empty, registering it as new")
    if row is None:
        row = Archive(uid=uid or uuid.uuid4().hex, root_abs=root, last_used_at=utcnow())
        session.add(row)
        session.commit()
        _write_uid(root, row.uid)
        logger.info(f"Registered archive folder: #{row.id} {root}")
    else:
        if row.root_abs != root and _is_live_copy_source(row, root):
            raise PreflightError(
                "This archive folder looks like a copy of another archive folder (same ID):\n"
                f"  this folder    : {root}\n"
                f"  registered one : {row.root_abs} (ID {row.uid})\n"
                "  To use the copy as a separate archive, delete its .akasyx/archive.id first.\n"
                "  If you moved the folder, remove or rename the old one."
            )
        if row.root_abs != root:
            logger.info(f"Archive folder location changed: {row.root_abs} -> {root}")
            row.root_abs = root
        if not uid:
            _write_uid(root, row.uid)
        row.last_used_at = utcnow()
        session.commit()

    # v0.1.x の DB が保存フォルダに残っていれば取り込む（登録済みかどうかに関係なく）
    migrated = import_legacy_db(session, row, legacy_db_path(root))
    if migrated:
        logger.info(f"Imported from legacy DB: {migrated}")
    return row


# --- v0.1.x → v0.2.0 移行 ---------------------------------------------------


def _columns(table_name: str) -> set[str]:
    return {c.name for c in Base.metadata.tables[table_name].columns}


def import_legacy_db(session, archive: Archive, legacy_path: str) -> dict | None:
    """保存フォルダ内に残る v0.1.x の archive.db を正本 DB へ取り込みます。

    3 テーブルを id の対応表を作りながらコピーし、archive_id を付ける。
    取り込み後、旧 DB は削除せず `archive.db.migrated-<日時>` に改名する（-wal / -shm も）。
    取り込み済みの印（ar_legacy_imports）をデータと同じトランザクションで書くので、
    改名に失敗して旧 DB が残っても次回は再取り込みせず改名だけやり直す。
    """
    if not os.path.isfile(legacy_path):
        return None
    # WAL に残っている分を本体へ書き戻す。読み取り（mode=ro）でも改名後に残す控えでも、
    # 本体 1 ファイルだけで内容が揃うようにする
    checkpointed = _checkpoint_legacy(legacy_path)
    done = session.query(LegacyImport).filter_by(archive_id=archive.id).first()
    if done is not None:
        logger.warning(
            f"The v0.1.x DB was already imported at {done.imported_at}; "
            f"retrying only the rename: {legacy_path}"
        )
        _rename_legacy(legacy_path, checkpointed)
        return None
    logger.warning(f"Found a v0.1.x DB; importing it into the master DB: {legacy_path}")

    conn = sqlite3.connect(f"file:{legacy_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    counts = {"ingests": 0, "files": 0, "items": 0}
    try:
        ingest_ids: dict[int, int] = {}
        file_ids: dict[int, int] = {}

        cols = _columns("ar_ingests") - {"id", "archive_id"}
        for r in conn.execute("SELECT * FROM ar_ingests ORDER BY id"):
            data = {k: r[k] for k in r.keys() if k in cols}
            _fix_datetimes(data)
            obj = Ingest(archive_id=archive.id, **data)
            session.add(obj)
            session.flush()
            ingest_ids[r["id"]] = obj.id
            counts["ingests"] += 1

        cols = _columns("ar_archive_files") - {"id", "archive_id"}
        for r in conn.execute("SELECT * FROM ar_archive_files ORDER BY id"):
            data = {k: r[k] for k in r.keys() if k in cols}
            _fix_datetimes(data)
            if data.get("ingest_id") is not None:
                data["ingest_id"] = ingest_ids.get(data["ingest_id"])
            obj = ArchiveFile(archive_id=archive.id, **data)
            session.add(obj)
            session.flush()
            file_ids[r["id"]] = obj.id
            counts["files"] += 1

        cols = _columns("ar_ingest_items") - {"id"}
        for r in conn.execute("SELECT * FROM ar_ingest_items ORDER BY id"):
            data = {k: r[k] for k in r.keys() if k in cols}
            _fix_datetimes(data)
            data["ingest_id"] = ingest_ids.get(data.get("ingest_id"))
            if data.get("archive_file_id") is not None:
                data["archive_file_id"] = file_ids.get(data["archive_file_id"])
            if data["ingest_id"] is None:
                continue  # 実行行の無い孤児は捨てる（v0.1 では起きないはず）
            session.add(IngestItem(**data))
            counts["items"] += 1

        session.add(LegacyImport(archive_id=archive.id, legacy_path=legacy_path))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        conn.close()

    _rename_legacy(legacy_path, checkpointed)
    return counts


def _checkpoint_legacy(legacy_path: str) -> bool:
    """旧 DB の WAL を本体へ書き戻します（-wal が無ければ何もしない）。成否を返す。"""
    if not os.path.exists(legacy_path + "-wal"):
        return True
    try:
        conn = sqlite3.connect(legacy_path)
        try:
            busy, _log, _done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning(f"Could not checkpoint the v0.1.x DB's WAL: {legacy_path}: {e}")
        return False
    return busy == 0


def _rename_legacy(legacy_path: str, checkpointed: bool) -> None:
    """取り込み済みの旧 DB を `.migrated-<日時>` に改名します。

    本体から改名し、失敗したらそこで止めて次回に再試行する（本体が残るので再試行される）。
    本体の後の -wal / -shm は、WAL を書き戻せていれば中身は本体にあるので、改名に失敗しても
    そのまま残す（次回は本体が無いので再試行されない。書き戻せていなければその旨を警告する）。
    """
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    try:
        os.replace(legacy_path, f"{legacy_path}.migrated-{stamp}")
    except OSError as e:
        logger.warning(f"Could not rename the imported v0.1.x DB (will retry next run): {legacy_path}: {e}")
        return
    for suffix in ("-wal", "-shm"):
        src = legacy_path + suffix
        if os.path.exists(src):
            try:
                os.replace(src, f"{legacy_path}.migrated-{stamp}{suffix}")
            except OSError as e:
                note = (
                    "its contents were already written back to the DB"
                    if checkpointed
                    else "the kept copy may lack the changes that were only in it"
                )
                logger.warning(f"Could not rename {src}; left in place ({note}): {e}")


_DT_COLUMNS = {
    "started_at", "finished_at", "origin_modified_at", "verified_at",
    "created_at", "updated_at", "resolved_at", "last_used_at",
}


def _fix_datetimes(data: dict) -> None:
    """sqlite3 が文字列で返す TIMESTAMP を datetime に直します（SQLAlchemy の型検査を通すため）。"""
    for key in list(data):
        if key in _DT_COLUMNS and isinstance(data[key], str):
            try:
                data[key] = datetime.fromisoformat(data[key])
            except ValueError:
                data[key] = None


def list_archives(session) -> list[tuple[Archive, int, int]]:
    """report/archives 用: (保存フォルダ, stored 件数, 合計サイズ) の一覧。"""
    from sqlalchemy import func
    from models import STATUS_STORED

    rows = session.query(Archive).order_by(Archive.id).all()
    out = []
    for a in rows:
        count, size = (
            session.query(func.count(ArchiveFile.id), func.coalesce(func.sum(ArchiveFile.size), 0))
            .filter(ArchiveFile.archive_id == a.id, ArchiveFile.status == STATUS_STORED)
            .one()
        )
        out.append((a, int(count), int(size)))
    return out
