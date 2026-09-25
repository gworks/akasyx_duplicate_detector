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
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from config import OS_JUNK_FILES
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
# 枝フォルダ名。001, 002, … 999 の次は 1000（桁は増えてよい）
BRANCH_DIGITS = 3
UNKNOWN_MONTH_DIR = "unknown-date"


@dataclass
class MoveResult:
    ok: bool
    archive_file: ArchiveFile | None = None
    stored_path_rel: str | None = None
    # 予約が UNIQUE 制約で弾かれた = 直前に同一内容が登録された（duplicate に倒す）
    conflict: bool = False
    message: str | None = None


# --- 保存先パスの決定（§7.2）-------------------------------------------------


def _path_taken_in_db(session, archive_id: int | None, rel: str) -> bool:
    """保存フォルダ内のパスが DB 上で占有されているかを判定します。

    大文字小文字を区別しないファイルシステムを考慮し、SQLite の lower() で
    両辺を畳んで比較する（Python 側の lower と混ぜると非 ASCII で食い違う）。
    """
    if session is None or archive_id is None:
        return False
    row = (
        session.query(ArchiveFile.id)
        .filter(
            ArchiveFile.archive_id == archive_id,
            func.lower(ArchiveFile.stored_path_rel) == func.lower(rel),
            ArchiveFile.status.in_(PATH_HOLDING_STATUSES),
        )
        .first()
    )
    return row is not None


def pick_free_name(
    session,
    archive_root: str,
    parent: str,
    base: str,
    extra_taken: set[str] | None = None,
    archive_id: int | None = None,
) -> str:
    """parent（保存フォルダ内の相対ディレクトリ、'' で直下）に base を置ける名前を返します。

    衝突時は `名前 (2).ext` → `名前 (3).ext` … の順に空きを探す。
    判定は「実体の有無」「DB の占有」「呼び出し側が予約済みのもの（dry-run 用）」の3点。
    """
    rel = f"{parent}/{base}" if parent else base
    stem, ext = os.path.splitext(base)
    taken = extra_taken if extra_taken is not None else set()

    for n in range(1, MAX_COLLISION_SUFFIX + 1):
        candidate_base = base if n == 1 else f"{stem} ({n}){ext}"
        candidate = f"{parent}/{candidate_base}" if parent else candidate_base
        if candidate.casefold() in taken:
            continue
        if os.path.lexists(from_posix(archive_root, candidate)):
            continue
        if _path_taken_in_db(session, archive_id, candidate):
            continue
        return candidate

    raise DetectorError(
        f"No free destination name found (tried {MAX_COLLISION_SUFFIX}): {rel}"
    )


# --- 保存先レイアウト: <YYYY-MM>/<投入元名>/<相対パス>/[枝/]<ファイル名>（§7.2）---------


def month_dir_of(scanned) -> str:
    """ファイルの作成日から年月フォルダ名（YYYY-MM）を決めます。

    優先順位: crawler の created_at（macOS の st_birthtime）→ 実体を stat した birthtime
    → modified_at。どれも取れなければ UNKNOWN_MONTH_DIR に寄せる。
    crawler が書く日時は UTC の naive 文字列なので、UTC とみなしてローカル時刻に直してから
    年月を取る（月末深夜の撮影が翌月に入らないように）。
    """
    dt = scanned.created_at
    if dt is None:
        try:
            st = os.stat(scanned.path_abs)
        except OSError:
            st = None
        if st is not None:
            birthtime = getattr(st, "st_birthtime", None)
            if birthtime is not None:
                dt = datetime.fromtimestamp(birthtime, tz=timezone.utc)
    if dt is None:
        dt = scanned.modified_at
    if dt is None:
        return UNKNOWN_MONTH_DIR
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"{dt.astimezone():%Y-%m}"


def _count_files_on_disk(abs_dir: str) -> int:
    """ディレクトリ直下の通常ファイル数（隠しファイルと .part は数えない）。"""
    try:
        with os.scandir(abs_dir) as it:
            return sum(
                1
                for e in it
                if not e.name.startswith(".")
                and not e.name.endswith(PART_SUFFIX)
                and e.is_file(follow_symlinks=False)
            )
    except FileNotFoundError:
        return 0


def _count_files_in_db(session, archive_id: int | None, rel_dir: str) -> int:
    """DB 上でそのディレクトリ直下を占有している行数（pending の予約も含む）。"""
    if session is None or archive_id is None:
        return 0
    prefix = f"{rel_dir}/" if rel_dir else ""
    rows = (
        session.query(ArchiveFile.stored_path_rel)
        .filter(
            ArchiveFile.archive_id == archive_id,
            ArchiveFile.stored_path_rel.like(f"{prefix}%") if prefix else True,
            ArchiveFile.status.in_(PATH_HOLDING_STATUSES),
        )
        .all()
    )
    return sum(1 for (p,) in rows if p.rpartition("/")[0] == rel_dir)


class DestPlanner:
    """保存先の相対パスを決めます（設計書 §7.2）。

        <YYYY-MM>/[<dest_subdir>/]<投入元の相対ディレクトリ>/[<枝>/]<ファイル名>

    年月はファイルの作成日（ローカル時刻）。その中に投入元の相対パス構造を再現し、
    ファイルが直接入る葉フォルダの件数が folder_limit に達したら 001/, 002/, … の枝に入れる。
    1 実行のあいだフォルダごとの件数をキャッシュし、決めるたびに 1 足す。
    dry-run でも同じ経路を通るので、実体も DB も変えずに枝分かれまで再現できる。
    """

    def __init__(self, session, config, dest_subdir: str | None):
        self.session = session
        self.config = config
        self.archive_root = config.archive_root
        self.archive_id = getattr(config, "archive_id", None)
        self.dest_subdir = to_posix(dest_subdir) if dest_subdir else None
        self.folder_limit = max(1, int(getattr(config, "folder_limit", 500)))
        self._counts: dict[str, int] = {}

    def resolve(self, scanned, extra_taken: set[str] | None = None) -> str:
        parts = [month_dir_of(scanned)]
        if self.dest_subdir:
            parts.append(self.dest_subdir)
        rel_dir, _, base = to_posix(scanned.path_rel).rpartition("/")
        if rel_dir:
            parts.append(rel_dir)
        leaf = "/".join(parts)
        bucket = self._pick_bucket(leaf)
        rel = pick_free_name(
            self.session, self.archive_root, bucket, base or scanned.name, extra_taken,
            archive_id=self.archive_id,
        )
        self._counts[bucket] = self.count(bucket) + 1
        return rel

    def count(self, rel_dir: str) -> int:
        """そのフォルダ直下の件数（実体と DB の大きい方。以後はキャッシュを更新していく）。"""
        if rel_dir not in self._counts:
            on_disk = _count_files_on_disk(from_posix(self.archive_root, rel_dir))
            in_db = _count_files_in_db(self.session, self.archive_id, rel_dir)
            self._counts[rel_dir] = max(on_disk, in_db)
        return self._counts[rel_dir]

    def _pick_bucket(self, leaf: str) -> str:
        """葉フォルダ直下 → 001 → 002 … の順に、上限未満の最初のフォルダを返します。

        999 の次は 1000（ゼロ埋め 3 桁は自然に 4 桁へ伸びる）。
        """
        if self.count(leaf) < self.folder_limit:
            return leaf
        n = 1
        while True:
            branch = f"{leaf}/{n:0{BRANCH_DIGITS}d}"
            if self.count(branch) < self.folder_limit:
                return branch
            n += 1


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
                    f"Hash mismatch after copy (expected {expected_hash} / actual {actual})"
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
        logger.error(f"Move failed but the file exists at the destination: {dst}: {message}")
    else:
        session.delete(record)
        logger.warning(f"Move failed (not performed; reservation cancelled): {message}")
    session.commit()


def plan_and_move(
    session, config, ingest_id: int, scanned, planner: "DestPlanner | str | None"
) -> MoveResult:
    """1ファイルを保存フォルダへ移動します（3フェーズ — 設計書 §7.3）。

    planner には DestPlanner を渡す。後方互換のため dest_subdir（str / None）を直接渡すと
    その場で DestPlanner を作る。
    """
    archive_root = config.archive_root
    if not isinstance(planner, DestPlanner):
        planner = DestPlanner(session, config, planner)

    try:
        rel = planner.resolve(scanned)
    except DetectorError as e:
        return MoveResult(ok=False, message=str(e))

    # Phase 1: 予約（実体を触る前に commit する。ここで落ちても復旧が拾える）
    record = ArchiveFile(
        archive_id=config.archive_id,
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
            message=f"Size mismatch after move (expected {scanned.size} / actual {actual_size})",
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
        logger.warning(f"Removed {removed} partial copies")
    return removed


def recover_pending(session, config) -> dict:
    """起動時に pending 行を検査して安全な状態へ寄せます（設計書 §7.4）。"""
    counts = {"stored": 0, "reverted": 0, "failed": 0}
    archive_root = config.archive_root
    rows = (
        session.query(ArchiveFile)
        .filter(
            ArchiveFile.archive_id == config.archive_id,
            ArchiveFile.status == STATUS_PENDING,
        )
        .all()
    )
    if not rows:
        cleanup_tmp(archive_root)
        return counts

    logger.warning(f"Found {len(rows)} incomplete moves; attempting recovery")
    for row in rows:
        dst = from_posix(archive_root, row.stored_path_rel)
        src = row.origin_path_abs

        if os.path.lexists(dst):
            try:
                dst_hash = hashing.file_hash(dst)
            except OSError as e:
                row.status = STATUS_FAILED
                counts["failed"] += 1
                logger.error(f"Recovery: cannot read destination {dst}: {e}")
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
                logger.error(f"Recovery: destination content differs from the reservation (not deleted automatically): {dst}")
            continue

        # 保存先に実体が無い
        if src and os.path.lexists(src):
            session.delete(row)  # 移動前に落ちた。次回の add で通常どおり処理される
            counts["reverted"] += 1
        else:
            row.status = STATUS_FAILED
            counts["failed"] += 1
            logger.error(
                f"Recovery: file missing at both destination and source: {row.stored_path_rel} / {src}"
            )

    session.commit()
    cleanup_tmp(archive_root)
    logger.warning(
        f"Recovery result - completed: {counts['stored']}, "
        f"reverted: {counts['reverted']}, needs review: {counts['failed']}"
    )
    return counts


def _remove_source_if_same(src: str, expected_hash: str) -> None:
    """元ファイルのハッシュが一致するときだけ削除します（復旧時の後始末）。"""
    try:
        if hashing.file_hash(src) == expected_hash:
            os.unlink(src)
        else:
            logger.warning(f"Recovery: keeping source file because its content differs: {src}")
    except OSError as e:
        logger.warning(f"Recovery: cannot delete source file: {src}: {e}")


# --- 後始末 -------------------------------------------------------------------


# OS / Finder が勝手に作るメタデータ。これしか無いフォルダは「空」とみなして一緒に消す
JUNK_FILES = frozenset(OS_JUNK_FILES)


def prune_empty_dirs(root: str) -> int:
    """root 配下の空ディレクトリを削除します（root 自体は消さない）。

    `.DS_Store` 等の OS メタデータ（JUNK_FILES）しか残っていないフォルダも空として扱い、
    メタデータを消してからフォルダを消す。macOS では Finder で開いただけで .DS_Store が
    できるため、これを無視しないと「空になった投入元」がほぼ永久に残る。
    """
    if not os.path.isdir(root):
        return 0
    removed = 0
    root_real = os.path.realpath(root)
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        if os.path.realpath(dirpath) == root_real:
            continue
        try:
            entries = os.listdir(dirpath)
            if any(name not in JUNK_FILES for name in entries):
                continue
            for name in entries:
                os.unlink(os.path.join(dirpath, name))
            os.rmdir(dirpath)
            removed += 1
        except OSError:
            continue
    return removed
