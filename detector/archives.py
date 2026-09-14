# archives.py - 保存フォルダの登録・識別・旧 DB の取り込み（設計書 §4 / §8。v0.2.0）
#
# 正本 DB は 1 つで、保存フォルダは ar_archives の行として区別する。
# 識別は uid（保存フォルダ内の .akasyx/archive.id にも書く）で行い、絶対パスは「現在の場所」。
import contextlib
import logging
import os
import sqlite3
import uuid
from datetime import datetime

from database import archive_id_path, legacy_db_path, meta_dir, tmp_dir
from models import Archive, ArchiveFile, Base, Ingest, IngestItem, utcnow

logger = logging.getLogger(__name__)


def _read_uid(archive_root: str) -> str | None:
    try:
        with open(archive_id_path(archive_root), encoding="utf-8") as f:
            uid = f.read().strip()
    except OSError:
        return None
    return uid or None


def _write_uid(archive_root: str, uid: str) -> None:
    os.makedirs(meta_dir(archive_root), exist_ok=True)
    with open(archive_id_path(archive_root), "w", encoding="utf-8") as f:
        f.write(uid + "\n")


def resolve_archive(session, archive_root: str) -> Archive:
    """保存フォルダに対応する ar_archives 行を返します（無ければ登録）。

    1. `.akasyx/archive.id` があれば uid で探す。見つかれば、パスが変わっていれば更新する
       （保存フォルダを移動・改名した場合）
    2. 無ければ絶対パスで探す（識別子ファイルだけ消えた場合）。見つかれば識別子を書き戻す
    3. どちらも無ければ新規登録し、識別子を書く。v0.1.x の `.akasyx/archive.db` が
       残っていればその内容を取り込む（旧 DB は `.migrated-<日時>` に改名して残す）
    """
    root = os.path.abspath(archive_root)
    os.makedirs(tmp_dir(root), exist_ok=True)
    uid = _read_uid(root)

    row = None
    if uid:
        row = session.query(Archive).filter(Archive.uid == uid).first()
        if row is None:
            logger.warning(
                f"識別子 {uid} は DB に無いため、保存フォルダを新規に登録します"
                "（別の DB で使っていたフォルダかもしれません）"
            )
    if row is None:
        row = session.query(Archive).filter(Archive.root_abs == root).first()
        if row is not None and uid and row.uid != uid:
            row = None  # 同じ場所に別の保存フォルダが置かれた。パス一致では同一視しない

    if row is None:
        row = Archive(uid=uid or uuid.uuid4().hex, root_abs=root, last_used_at=utcnow())
        session.add(row)
        session.commit()
        _write_uid(root, row.uid)
        logger.info(f"保存フォルダを登録しました: #{row.id} {root}")
    else:
        if row.root_abs != root:
            logger.info(f"保存フォルダの場所が変わりました: {row.root_abs} → {root}")
            row.root_abs = root
        if not uid:
            _write_uid(root, row.uid)
        row.last_used_at = utcnow()
        session.commit()

    # v0.1.x の DB が保存フォルダに残っていれば取り込む（登録済みかどうかに関係なく）
    migrated = import_legacy_db(session, row, legacy_db_path(root))
    if migrated:
        logger.info(f"旧 DB から取り込み: {migrated}")
    return row


# --- v0.1.x → v0.2.0 移行 ---------------------------------------------------


def _columns(table_name: str) -> set[str]:
    return {c.name for c in Base.metadata.tables[table_name].columns}


def import_legacy_db(session, archive: Archive, legacy_path: str) -> dict | None:
    """保存フォルダ内に残る v0.1.x の archive.db を正本 DB へ取り込みます。

    3 テーブルを id の対応表を作りながらコピーし、archive_id を付ける。
    取り込み後、旧 DB は削除せず `archive.db.migrated-<日時>` に改名する（-wal / -shm も）。
    """
    if not os.path.isfile(legacy_path):
        return None
    logger.warning(f"v0.1.x の DB を見つけました。正本 DB へ取り込みます: {legacy_path}")

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

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        conn.close()

    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    for suffix in ("", "-wal", "-shm"):
        src = legacy_path + suffix
        if os.path.exists(src):
            with contextlib.suppress(OSError):
                os.replace(src, f"{legacy_path}.migrated-{stamp}{suffix}")
    return counts


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
