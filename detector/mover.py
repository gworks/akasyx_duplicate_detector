# mover.py - 移動・衝突回避・クラッシュ復旧（設計書 §7.2 〜 §7.4）
#
# 実体を書き換える権限を持つのはこのモジュールだけ。
# 「検証してから消す」原則（設計書 §1）をここで守り切る。
import contextlib
import logging
import os
import shutil
import uuid
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from database import tmp_dir
from errors import DetectorError
from models import (
    PATH_HOLDING_STATUSES,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_STORED,
    ArchiveFile,
    utcnow,
)
from utl import hashing
from utl.helpers import from_posix, to_posix

logger = logging.getLogger(__name__)

MAX_COLLISION_SUFFIX = 999  # 無限ループを作らない（設計書 §7.2）
PART_SUFFIX = ".part"


@dataclass
class MoveResult:
    ok: bool
    archive_file: ArchiveFile | None = None
    stored_path_rel: str | None = None
    # 予約が UNIQUE 制約で弾かれた = 直前に同一内容が登録された（duplicate に倒す）
    conflict: bool = False
    message: str | None = None


# --- 保存先パスの決定（§7.2）-------------------------------------------------


def _path_taken_in_db(session, rel: str) -> bool:
    """保存フォルダ内のパスが DB 上で占有されているかを判定します。

    大文字小文字を区別しないファイルシステムを考慮し、SQLite の lower() で
    両辺を畳んで比較する（Python 側の lower と混ぜると非 ASCII で食い違う）。
    """
    if session is None:
        return False
    row = (
        session.query(ArchiveFile.id)
        .filter(
            func.lower(ArchiveFile.stored_path_rel) == func.lower(rel),
            ArchiveFile.status.in_(PATH_HOLDING_STATUSES),
        )
        .first()
    )
    return row is not None


def resolve_dest(
    session,
    archive_root: str,
    dest_subdir: str | None,
    path_rel: str,
    extra_taken: set[str] | None = None,
) -> str:
    """保存フォルダ内の相対パス（POSIX）を、衝突を避けて決定します。

    衝突時は `名前 (2).ext` → `名前 (3).ext` … の順に空きを探す。
    判定は「実体の有無」「DB の占有」「呼び出し側が予約済みのもの（dry-run 用）」の3点。
    """
    rel = to_posix(path_rel)
    if dest_subdir:
        rel = f"{to_posix(dest_subdir)}/{rel}"
    parent, _, base = rel.rpartition("/")
    stem, ext = os.path.splitext(base)
    taken = extra_taken if extra_taken is not None else set()

    for n in range(1, MAX_COLLISION_SUFFIX + 1):
        candidate_base = base if n == 1 else f"{stem} ({n}){ext}"
        candidate = f"{parent}/{candidate_base}" if parent else candidate_base
        if candidate.casefold() in taken:
            continue
        if os.path.lexists(from_posix(archive_root, candidate)):
            continue
        if _path_taken_in_db(session, candidate):
            continue
        return candidate

    raise DetectorError(
        f"保存先の空きが見つかりません（{MAX_COLLISION_SUFFIX} 件まで試行）: {rel}"
    )


# --- 実体の移動（§7.3 Phase 2）------------------------------------------------


def same_filesystem(src: str, dst_dir: str) -> bool:
    """src と保存先ディレクトリが同一ファイルシステム上にあるかを判定します。"""
    return os.stat(src).st_dev == os.stat(dst_dir).st_dev


def safe_move(src: str, dst: str, expected_hash: str | None, tmp_root: str) -> None:
    """src を dst へ安全に移動します。失敗時、元ファイルは必ず残ります。

    - 同一ファイルシステム: os.replace（アトミック）
    - 別ファイルシステム  : copy → ハッシュ再計算で検証 → **検証通過後にのみ** 元を削除

    shutil.move は内部が copy+delete でコピー内容を検証しないため使わない（設計書 §7.3）。
    """
    dst_dir = os.path.dirname(dst)
    os.makedirs(dst_dir, exist_ok=True)

    if same_filesystem(src, dst_dir):
        os.replace(src, dst)
        return

    os.makedirs(tmp_root, exist_ok=True)
    part = os.path.join(tmp_root, f"{uuid.uuid4().hex}{PART_SUFFIX}")
    try:
        shutil.copy2(src, part)
        if expected_hash is not None:
            actual = hashing.file_hash(part)
            if actual != expected_hash:
                raise DetectorError(
                    f"コピー後のハッシュが一致しません（期待 {expected_hash} / 実際 {actual}）"
                )
        os.replace(part, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(part)
        raise
    # ★ここまで来て初めて元ファイルを消す
    os.unlink(src)


# --- 3フェーズ移動（§7.3）-----------------------------------------------------


def _abandon_reservation(session, record: ArchiveFile, dst: str, message: str) -> None:
    """移動に失敗した予約行を後始末します。

    実体が保存先に無ければ「未実施」なので行を消して次回やり直せるようにし、
    実体があるなら人の確認が要るので failed で残す。
    """
    if os.path.lexists(dst):
        record.status = STATUS_FAILED
        logger.error(f"移動に失敗したが保存先に実体があります: {dst}: {message}")
    else:
        session.delete(record)
        logger.warning(f"移動に失敗（未実施として予約を取り消し）: {message}")
    session.commit()


def plan_and_move(
    session, config, ingest_id: int, scanned, dest_subdir: str | None
) -> MoveResult:
    """1ファイルを保存フォルダへ移動します（3フェーズ — 設計書 §7.3）。"""
    archive_root = config.archive_root

    try:
        rel = resolve_dest(session, archive_root, dest_subdir, scanned.path_rel)
    except DetectorError as e:
        return MoveResult(ok=False, message=str(e))

    # Phase 1: 予約（実体を触る前に commit する。ここで落ちても復旧が拾える）
    record = ArchiveFile(
        filehash=scanned.filehash,
        hash_algo=scanned.hash_algo,
        size=scanned.size,
        name=scanned.name,
        stored_path_rel=rel,
        origin_path_abs=scanned.path_abs,
        origin_root=config.source_path,
        origin_modified_at=scanned.modified_at,
        mime_type=scanned.mime_type,
        ingest_id=ingest_id,
        status=STATUS_PENDING,
    )
    session.add(record)
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        # 同一内容が直前に登録された、または保存先パスが埋まった
        return MoveResult(ok=False, conflict=True, message=str(e.orig))

    # Phase 2: 移動
    dst = from_posix(archive_root, rel)
    try:
        safe_move(scanned.path_abs, dst, scanned.filehash, tmp_dir(archive_root))
    except (OSError, DetectorError) as e:
        _abandon_reservation(session, record, dst, str(e))
        return MoveResult(ok=False, message=str(e))

    # Phase 3: 確定
    try:
        actual_size = os.stat(dst).st_size
    except OSError as e:
        _abandon_reservation(session, record, dst, str(e))
        return MoveResult(ok=False, message=str(e))

    if actual_size != scanned.size:
        record.status = STATUS_FAILED
        session.commit()
        return MoveResult(
            ok=False,
            message=f"移動後のサイズが一致しません（期待 {scanned.size} / 実際 {actual_size}）",
        )

    record.status = STATUS_STORED
    record.verified_at = utcnow()
    session.commit()
    return MoveResult(ok=True, archive_file=record, stored_path_rel=rel)


# --- クラッシュ復旧（§7.4）----------------------------------------------------


def cleanup_tmp(archive_root: str) -> int:
    """`.akasyx/tmp/*.part` を無条件で削除します。

    dst へ os.replace された時点で .part は消えているため、残っていれば必ず失敗の残骸。
    """
    root = tmp_dir(archive_root)
    if not os.path.isdir(root):
        return 0
    removed = 0
    for name in os.listdir(root):
        if not name.endswith(PART_SUFFIX):
            continue
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(root, name))
            removed += 1
    if removed:
        logger.warning(f"作りかけのコピー {removed} 件を削除しました")
    return removed


def recover_pending(session, config) -> dict:
    """起動時に pending 行を検査して安全な状態へ寄せます（設計書 §7.4）。"""
    counts = {"stored": 0, "reverted": 0, "failed": 0}
    archive_root = config.archive_root
    rows = (
        session.query(ArchiveFile).filter(ArchiveFile.status == STATUS_PENDING).all()
    )
    if not rows:
        cleanup_tmp(archive_root)
        return counts

    logger.warning(f"未完了の移動が {len(rows)} 件あります。復旧を試みます")
    for row in rows:
        dst = from_posix(archive_root, row.stored_path_rel)
        src = row.origin_path_abs

        if os.path.lexists(dst):
            try:
                dst_hash = hashing.file_hash(dst)
            except OSError as e:
                row.status = STATUS_FAILED
                counts["failed"] += 1
                logger.error(f"復旧: 保存先を読めません {dst}: {e}")
                continue

            if dst_hash == row.filehash:
                # 移動は完了していた。別FS コピー後に中断していれば元が残っている
                if src and os.path.lexists(src):
                    _remove_source_if_same(src, row.filehash)
                row.status = STATUS_STORED
                row.verified_at = utcnow()
                counts["stored"] += 1
            else:
                row.status = STATUS_FAILED
                counts["failed"] += 1
                logger.error(f"復旧: 保存先の内容が予約と違います（自動では消しません）: {dst}")
            continue

        # 保存先に実体が無い
        if src and os.path.lexists(src):
            session.delete(row)  # 移動前に落ちた。次回の add で通常どおり処理される
            counts["reverted"] += 1
        else:
            row.status = STATUS_FAILED
            counts["failed"] += 1
            logger.error(
                f"復旧: 保存先にも元にも実体がありません: {row.stored_path_rel} / {src}"
            )

    session.commit()
    cleanup_tmp(archive_root)
    logger.warning(
        f"復旧結果 — 完了扱い: {counts['stored']}件, "
        f"取り消し: {counts['reverted']}件, 要確認: {counts['failed']}件"
    )
    return counts


def _remove_source_if_same(src: str, expected_hash: str) -> None:
    """元ファイルのハッシュが一致するときだけ削除します（復旧時の後始末）。"""
    try:
        if hashing.file_hash(src) == expected_hash:
            os.unlink(src)
        else:
            logger.warning(f"復旧: 元ファイルの内容が違うため残します: {src}")
    except OSError as e:
        logger.warning(f"復旧: 元ファイルを削除できません: {src}: {e}")


# --- 後始末 -------------------------------------------------------------------


def prune_empty_dirs(root: str) -> int:
    """root 配下の空ディレクトリを削除します（root 自体は消さない）。"""
    if not os.path.isdir(root):
        return 0
    removed = 0
    root_real = os.path.realpath(root)
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        if os.path.realpath(dirpath) == root_real:
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                removed += 1
        except OSError:
            continue
    return removed
