# database.py - archive.db の接続・セッション管理（設計書 §6.3）
import contextlib
import logging
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from errors import PreflightError
from models import Base

logger = logging.getLogger(__name__)

META_DIRNAME = ".akasyx"
LEGACY_DB_FILENAME = "archive.db"   # v0.1.x: 保存フォルダの中に置いていた正本（移行元）
ARCHIVE_ID_FILENAME = "archive.id"  # v0.2.0: 保存フォルダ側に置く識別子（中身は uid 1 行）
TMP_DIRNAME = "tmp"
LOCK_FILENAME = "lock"


def meta_dir(archive_root: str) -> str:
    """保存フォルダ内のメタ領域 `<archive>/.akasyx` を返します。"""
    return os.path.join(archive_root, META_DIRNAME)


def legacy_db_path(archive_root: str) -> str:
    """v0.1.x が保存フォルダの中に置いていた archive.db のパス（移行の入力）。"""
    return os.path.join(meta_dir(archive_root), LEGACY_DB_FILENAME)


def archive_id_path(archive_root: str) -> str:
    """保存フォルダ側の識別子ファイル `<archive>/.akasyx/archive.id` のパス。"""
    return os.path.join(meta_dir(archive_root), ARCHIVE_ID_FILENAME)


def tmp_dir(archive_root: str) -> str:
    """別ファイルシステム間コピーの一時領域を返します（*.part の置き場）。"""
    return os.path.join(meta_dir(archive_root), TMP_DIRNAME)


def get_session(path: str) -> tuple[Session, object]:
    """正本 DB（既定 <リポジトリルート>/dist/archive.db）に接続してセッションを返します。

    無ければテーブル・索引ごと作成する。crawler と同じく create_all で索引まで導出し、
    手動リストとの二重管理をしない。DB は保存フォルダの外・ローカルディスクに置く
    （設計書 §4。ネットワーク上の SQLite を避けるため — v0.2.0）。
    """
    path = os.path.abspath(path)
    is_new = not os.path.exists(path)
    if is_new:
        # 起動時に正本 DB が無ければ作る（初回起動・dist/ を消した後・別マシンでの初回）。
        # 中身のある既存の保存フォルダは、この DB に登録が無いので開くと断られる
        # （空の登録として扱うと重複を作るため。使っていた正本 DB を --archive-db で指定する）
        logger.warning(f"Master DB not found; creating a new one: {path}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # pragma: no cover - 接続時フック
        cur = dbapi_conn.cursor()
        # WAL: 移動処理中の頻繁な commit に強い
        cur.execute("PRAGMA journal_mode=WAL")
        # FULL: クラッシュ時に pending 行が失われると復旧できない。速度より耐久性
        cur.execute("PRAGMA synchronous=FULL")
        # SQLite は既定で FK が無効
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    if is_new and not os.path.exists(path):  # pragma: no cover - SQLite が作れなかった異常系
        raise PreflightError(f"Could not create the master DB: {path}")
    session = sessionmaker(bind=engine)()
    logger.info(f"DB {'created' if is_new else 'connected'}: {path}")
    return session, engine


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 他ユーザーのプロセスとして生きている
        return True
    except OSError:
        return True
    return True


@contextlib.contextmanager
def archive_lock(archive_root: str):
    """保存フォルダ単位の多重起動を防ぐロック（設計書 §12）。

    WAL でも書き込みは排他されるが、複数プロセスが同時に移動すると実体の整合が崩れる。
    ロックファイルに PID を書き、死んだプロセスのロックは引き継ぐ。
    """
    os.makedirs(meta_dir(archive_root), exist_ok=True)
    path = os.path.join(meta_dir(archive_root), LOCK_FILENAME)

    for attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            holder = ""
            try:
                with open(path, encoding="utf-8") as f:
                    holder = f.read().strip()
            except OSError:
                pass
            if attempt == 0 and holder.isdigit() and not _pid_alive(int(holder)):
                logger.warning(f"Taking over the lock from a dead process (PID {holder})")
                with contextlib.suppress(OSError):
                    os.unlink(path)
                continue
            raise PreflightError(
                f"The archive folder is in use by another process (PID {holder or 'unknown'}): {path}\n"
                "If no other instance is running, delete this lock file"
            )
    else:  # pragma: no cover - 上の for で必ず break か raise する
        raise PreflightError(f"Could not acquire the lock: {path}")

    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield path
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
