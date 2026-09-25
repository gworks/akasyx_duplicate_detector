# crawler_client.py - crawler の起動と fs_files の読み取り（設計書 §3 / §6.4）
#
# crawler は `package = false` のフラット構成（import collector）でライブラリ import に
# 向かないため、CLI としてサブプロセス起動し、出力された file_inventory.db を
# 読み取り専用で参照する。detector の判定ロジックは ScannedFile にしか依存させない。
import logging
import mimetypes
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from errors import PreflightError
from utl import hashing
from utl.helpers import to_posix

logger = logging.getLogger(__name__)

CRAWLER_DB_FILENAME = "file_inventory.db"
CRAWLER_PKG_DIRNAME = "crawler"
# 配布版で同梱する crawler の実行形式の名前（akasyx_search の packaging/build_python.sh と同じ）
CRAWLER_BIN_NAME = "akasyx-crawler"
# 走査が中途半端な状態で移動を始めると、投入元の一部だけが移った状態になる（設計書 §3）
COMPLETED_STATUS = "completed"


@dataclass
class ScannedFile:
    """crawler の fs_files 1行から、detector が使う列だけを取り出したもの。"""

    name: str
    path_abs: str
    path_rel: str  # 走査ルートからの相対パス（POSIX 区切り）
    size: int
    filehash: str | None
    hash_algo: str | None
    mime_type: str | None = None
    modified_at: datetime | None = None
    # 作成日時（crawler は macOS/BSD で st_birthtime、Windows で st_ctime、Linux は None）
    created_at: datetime | None = None


@dataclass
class CrawlerScan:
    """crawler の1回の実行結果。"""

    db_path: str
    scan_id: int | None
    status: str
    root_dir: str


def resolve_crawler_repo(path: str) -> str:
    """crawler リポジトリのパスを検証して返します。見つからなければ PreflightError。"""
    pkg = os.path.join(path, CRAWLER_PKG_DIRNAME, "main.py")
    if not os.path.isfile(pkg):
        raise PreflightError(
            f"akasyx_crawler が見つかりません: {pkg}\n"
            "--crawler-repo でリポジトリのパスを指定してください"
        )
    return path


def crawler_executable(siblings_bin: str) -> str:
    """同梱した crawler の実行形式のパス（PyInstaller onedir: <bin>/akasyx-crawler/akasyx-crawler）。"""
    exe = CRAWLER_BIN_NAME + (".exe" if os.name == "nt" else "")
    return os.path.join(siblings_bin, CRAWLER_BIN_NAME, exe)


def check_crawler(config) -> None:
    """crawler を起動できるか確かめます（事前チェック用）。できなければ PreflightError。"""
    if config.siblings_bin:
        exe = crawler_executable(config.siblings_bin)
        if not os.access(exe, os.X_OK):
            raise PreflightError(
                f"同梱の crawler が見つかりません: {exe}\n"
                "アプリを入れ直してください"
            )
        return
    resolve_crawler_repo(config.crawler_repo)


def crawler_command(config) -> tuple[list[str], str]:
    """crawler の起動コマンドの先頭部分と cwd。

    開発時は `uv run main.py`（crawler リポジトリの crawler/ で）、配布版は同梱した実行形式。
    """
    if config.siblings_bin:
        exe = crawler_executable(config.siblings_bin)
        return [exe], os.path.dirname(exe)
    repo = resolve_crawler_repo(config.crawler_repo)
    if shutil.which("uv") is None:
        raise PreflightError(
            "uv が見つかりません。crawler の実行に必要です（https://docs.astral.sh/uv/）"
        )
    return ["uv", "run", "main.py"], os.path.join(repo, CRAWLER_PKG_DIRNAME)


def _max_scan_id(db_path: str) -> int:
    """crawler DB の fs_scans の最大 id を返します（DB もテーブルも無ければ 0）。"""
    if not os.path.exists(db_path):
        return 0
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT MAX(id) FROM fs_scans").fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def _read_scan(db_path: str, after_id: int) -> tuple[int | None, str]:
    """after_id より後に作られた最新の fs_scans 行の (id, status) を返します。"""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT id, status FROM fs_scans WHERE id > ? ORDER BY id DESC LIMIT 1",
            (after_id,),
        ).fetchone()
    if row is None:
        return None, "not_found"
    return int(row[0]), str(row[1])


def run_crawler(target: str, config, extra_excludes: tuple[str, ...] = ()) -> CrawlerScan:
    """crawler をサブプロセス実行し、生成されたスキャンを返します（設計書 §3）。

    ハッシュ関連のオプション（--no-hash / --hash-max-size）は渡さない。
    ハッシュが無いと重複判定ができないため。
    """
    base, cwd = crawler_command(config)
    db_path = os.path.join(config.db_dir, CRAWLER_DB_FILENAME)

    cmd = [
        *base, target,
        "--db-dir", config.db_dir,
        "--log-dir", config.log_dir,
        # crawler の既定は nested。投入元の .gitignore で対象が勝手に減るのを防ぐ
        "--gitignore-mode", "off",
    ]
    if not config.with_meta:
        cmd.append("--no-meta")
    if config.follow_symlinks:
        cmd.append("--follow-symlinks")
    for pattern in extra_excludes:
        cmd += ["--exclude", pattern]

    before = _max_scan_id(db_path)
    logger.info(f"crawler 実行: {' '.join(cmd)} (cwd={cwd})")
    # detector 側の VIRTUAL_ENV を持ち込むと uv が「プロジェクトの環境と違う」と警告する。
    # crawler は crawler 自身の .venv で動かすべきなので落としておく
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    proc = subprocess.run(cmd, cwd=cwd, check=False, env=env)
    if proc.returncode != 0:
        logger.warning(f"crawler が非ゼロ終了しました（コード {proc.returncode}）")

    if not os.path.exists(db_path):
        raise PreflightError(f"crawler が DB を生成しませんでした: {db_path}")

    scan_id, status = _read_scan(db_path, before)
    return CrawlerScan(
        db_path=db_path, scan_id=scan_id, status=status, root_dir=target
    )


def ensure_completed(scan: CrawlerScan) -> None:
    """スキャンが完走していなければ PreflightError で止めます（設計書 §3）。"""
    if scan.scan_id is None:
        raise PreflightError(
            "crawler のスキャン結果が見つかりません（crawler の実行に失敗した可能性があります）"
        )
    if scan.status != COMPLETED_STATUS:
        raise PreflightError(
            f"crawler のスキャンが完走していません（status={scan.status}）。"
            "不完全な走査では取り込みを行いません"
        )


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def read_files(db_path: str, scan_id: int) -> Iterator[ScannedFile]:
    """今回のスキャンで存在確認された active な行を読み出します（読み取り専用）。"""
    sql = (
        "SELECT name, path_abs, path_rel, size, filehash, hash_algo, mime_type,"
        " modified_at, created_at FROM fs_files"
        " WHERE last_seen_scan_id = ? AND status = 'active' ORDER BY path_abs"
    )
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        for row in conn.execute(sql, (scan_id,)):
            yield ScannedFile(
                name=row[0],
                path_abs=row[1],
                path_rel=to_posix(row[2] or row[0]),
                size=int(row[3] or 0),
                filehash=row[4],
                hash_algo=row[5],
                mime_type=row[6],
                modified_at=_parse_dt(row[7]),
                created_at=_parse_dt(row[8]),
            )


def scan_single_file(path: str) -> ScannedFile:
    """単一ファイルを crawler を使わずに直接読み取ります（設計書 §6.4）。

    1ファイルのためにサブプロセスを起こす意味がなく、除外指定の取りこぼしも避けられる。
    """
    st = os.stat(path)
    name = os.path.basename(path)
    try:
        filehash = hashing.file_hash(path)
        algo = hashing.HASH_ALGO
    except OSError as e:
        # 読み取れなければハッシュ無しとして返す（判定側が skipped_nohash にする）
        logger.warning(f"ハッシュ計算に失敗: {path}: {e}")
        filehash, algo = None, None
    return ScannedFile(
        name=name,
        path_abs=path,
        path_rel=name,
        size=st.st_size,
        filehash=filehash,
        hash_algo=algo,
        mime_type=mimetypes.guess_type(path)[0],
        modified_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
        created_at=birthtime_utc(st),
    )


def birthtime_utc(st: os.stat_result) -> datetime | None:
    """stat 結果から作成日時（UTC）を返します。crawler の utl/fileinfo.resolve_times と同じ規則。

    macOS/BSD は st_birthtime、Windows は st_ctime（作成日時の意味）、Linux は取得不可で None。
    """
    birthtime = getattr(st, "st_birthtime", None)
    if birthtime is not None:
        return datetime.fromtimestamp(birthtime, tz=timezone.utc)
    if os.name == "nt":
        return datetime.fromtimestamp(st.st_ctime, tz=timezone.utc)
    return None
