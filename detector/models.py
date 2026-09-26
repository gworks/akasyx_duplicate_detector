# models.py - SQLAlchemy ORM モデル（設計書 §8）
from datetime import datetime, timezone

from sqlalchemy import (
    TIMESTAMP,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --- ar_ingests.mode ---------------------------------------------------------
MODE_ADD = "add"
MODE_VERIFY = "verify"
MODE_DELETE_DUPLICATES = "delete-duplicates"
MODE_REPORT = "report"
MODE_ARCHIVES = "archives"  # 登録済みの保存フォルダ一覧（DB 全体を見る。保存フォルダ指定なし）

# --- ar_archive_files.status -------------------------------------------------
STATUS_PENDING = "pending"            # 移動を予約したが未完了（§7.3 Phase 1〜2）
STATUS_STORED = "stored"              # 実体があり DB と一致（正常）
STATUS_MISSING = "missing"            # DB にはあるが実体が無い（削除はしない）
STATUS_UNREGISTERED = "unregistered"  # 実体はあるが DB に無かった（verify が事後登録）
STATUS_QUARANTINED = "quarantined"    # 隔離フォルダへ移動済み（将来拡張）
STATUS_FAILED = "failed"              # 復旧で判断がつかなかった（人の確認待ち）

# 内容を「保持している」状態。重複判定と部分 UNIQUE 索引の対象（設計書 §8）。
# missing / unregistered を外すことで、消えたファイルと同内容のものを後から再登録できる。
OWNING_STATUSES = (STATUS_PENDING, STATUS_STORED)
# 保存フォルダ内のパスを占有している状態（パスの二重予約を防ぐ）
PATH_HOLDING_STATUSES = (STATUS_PENDING, STATUS_STORED, STATUS_UNREGISTERED)

_OWNING_SQL = "status IN ('pending','stored')"
_PATH_HOLDING_SQL = "status IN ('pending','stored','unregistered')"

# --- ar_archive_files.disposition（処置予定フラグ — 設計書 §8 / §9）-----------
DISPOSITION_QUARANTINE = "quarantine"  # 別フォルダ（隔離先）へ移動する予定
DISPOSITION_ADOPT = "adopt"            # unregistered を正式登録する予定
DISPOSITION_DELETE = "delete"          # 削除する予定

# --- ar_ingest_items.result --------------------------------------------------
RESULT_MOVED = "moved"                        # add: 保存フォルダへ移動した
RESULT_DUPLICATE = "duplicate"                # add: 内容重複のため投入元に残した
RESULT_SKIPPED_EMPTY = "skipped_empty"        # add: 0 バイト（min_size 未満）
RESULT_SKIPPED_NOHASH = "skipped_nohash"      # add: ハッシュが無く判定不能
RESULT_FAILED = "failed"                      # add / 復旧: 処理に失敗した
RESULT_ARCHIVE_DUPLICATE = "archive_duplicate"  # verify: 保存フォルダ内の重複
RESULT_HASH_MISMATCH = "hash_mismatch"        # verify: DB と実体のハッシュが違う
RESULT_RELOCATED = "relocated"                # verify: 場所が変わっていた
RESULT_MISSING = "missing"                    # verify: 実体が見つからなかった
RESULT_UNREGISTERED = "unregistered"          # verify: 実体はあるが DB に無かった

# --- ar_ingest_items.resolution（後始末の結果 — 設計書 §10）------------------
RESOLUTION_DELETED = "deleted"
RESOLUTION_TRASHED = "trashed"
RESOLUTION_KEPT = "kept"
RESOLUTION_GONE = "gone"  # 処置しようとしたら既に元ファイルが無かった


class Archive(Base):
    """ar_archives — 保存フォルダ（1 保存フォルダ = 1 行。v0.2.0）。

    DB は保存フォルダの外（ローカルディスク）に 1 つだけ置き、複数の保存フォルダを
    この表で区別する。識別子は uid（保存フォルダ内の `.akasyx/archive.id` にも書く）で、
    root_abs は「現在の絶対パス」。フォルダを移動しても uid が同じなら同じ行に繋がる。
    """

    __tablename__ = "ar_archives"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String, unique=True)
    root_abs: Mapped[str] = mapped_column(Text, index=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, default=utcnow, onupdate=utcnow
    )
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)


class Ingest(Base):
    """ar_ingests — 実行の記録（1回の実行 = 1行。crawler の fs_scans に相当）。"""

    __tablename__ = "ar_ingests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    archive_id: Mapped[int | None] = mapped_column(
        ForeignKey("ar_archives.id"), index=True, nullable=True
    )
    mode: Mapped[str] = mapped_column(String)
    source_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    archive_root: Mapped[str] = mapped_column(Text)
    crawler_db_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    crawler_scan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dry_run: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    # running / completed / interrupted / failed
    status: Mapped[str] = mapped_column(String, default="running")
    total: Mapped[int] = mapped_column(Integer, default=0)
    moved: Mapped[int] = mapped_column(Integer, default=0)
    duplicated: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    stats_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    app_version: Mapped[str | None] = mapped_column(String, nullable=True)


class ArchiveFile(Base):
    """ar_archive_files — 保存フォルダの正本（1内容 = 1行）。"""

    __tablename__ = "ar_archive_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # どの保存フォルダのものか（v0.2.0）。重複判定・パス占有はこの単位で閉じる
    archive_id: Mapped[int] = mapped_column(ForeignKey("ar_archives.id"), index=True)
    # 重複判定の主キー。hash_algo が異なるもの同士は照合しない（crawler §14）
    filehash: Mapped[str] = mapped_column(Text, index=True)
    hash_algo: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(Text)
    # 保存フォルダからの相対パス（POSIX 区切り）
    stored_path_rel: Mapped[str] = mapped_column(Text, index=True)
    origin_path_abs: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_modified_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP, nullable=True
    )
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    ingest_id: Mapped[int | None] = mapped_column(
        ForeignKey("ar_ingests.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, index=True, default=STATUS_PENDING)
    # 処置予定フラグ。v0.1.0 では立てるだけで実移動は行わない（§9 / §16）
    disposition: Mapped[str | None] = mapped_column(
        String, index=True, nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        # 重複判定の要。実装にバグがあっても二重登録を DB が弾く（設計書 §8）
        Index(
            "ux_archive_files_content",
            "archive_id",
            "filehash",
            "hash_algo",
            unique=True,
            sqlite_where=text(_OWNING_SQL),
        ),
        # 保存先パスの二重予約を防ぐ（保存フォルダ単位）
        Index(
            "ux_archive_files_path",
            "archive_id",
            "stored_path_rel",
            unique=True,
            sqlite_where=text(_PATH_HOLDING_SQL),
        ),
        # 衝突回避（§7.2）は casefold して突き合わせるため、小文字の式索引を張る
        Index("ix_archive_files_path_lower", "archive_id", text("lower(stored_path_rel)")),
    )


class IngestItem(Base):
    """ar_ingest_items — 判定の記録・監査（1ファイル1判定 = 1行）。"""

    __tablename__ = "ar_ingest_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingest_id: Mapped[int] = mapped_column(ForeignKey("ar_ingests.id"), index=True)
    source_path_abs: Mapped[str] = mapped_column(Text, index=True)
    source_path_rel: Mapped[str] = mapped_column(Text, default="")
    name: Mapped[str] = mapped_column(Text, default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    filehash: Mapped[str | None] = mapped_column(Text, index=True, nullable=True)
    hash_algo: Mapped[str | None] = mapped_column(String, nullable=True)
    result: Mapped[str] = mapped_column(String, index=True)
    archive_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("ar_archive_files.id"), nullable=True
    )
    planned_path_rel: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=utcnow)

    __table_args__ = (
        # delete-duplicates の対象抽出（result='duplicate' AND resolution IS NULL）
        Index("ix_ingest_items_result_resolution", "result", "resolution"),
    )


class LegacyImport(Base):
    """ar_legacy_imports — v0.1.x の archive.db を取り込み済みの保存フォルダ（v0.2.0 移行用）。

    取り込みデータと同じトランザクションで書く。旧 DB の改名が失敗したり commit 直後に
    落ちたりして旧 DB が残っても、この行があれば再取り込みせず改名だけやり直す。
    """

    __tablename__ = "ar_legacy_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    archive_id: Mapped[int] = mapped_column(ForeignKey("ar_archives.id"), unique=True)
    legacy_path: Mapped[str] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=utcnow)
