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
DB_FILENAME = "archive.db"
TMP_DIRNAME = "tmp"
LOCK_FILENAME = "lock"


def meta_dir(archive_root: str) -> str:
    """保存フォルダ内のメタ領域 `<archive>/.akasyx` を返します。"""
    return os.path.join(archive_root, META_DIRNAME)


def db_path(archive_root: str) -> str:
    """archive.db のパスを返します。

    DB を保存フォルダの中に置くのは、フォルダごと別ディスクへ移動・バックアップしても
    正本が付いてくるようにするため（設計書 §4）。
    """
    return os.path.join(meta_dir(archive_root), DB_FILENAME)


def tmp_dir(archive_root: str) -> str:
    """別ファイルシステム間コピーの一時領域を返します（*.part の置き場）。"""
    return os.path.join(meta_dir(archive_root), TMP_DIRNAME)


def get_session(archive_root: str) -> tuple[Session, object]:
    """archive.db に接続してセッションを返します。無ければテーブル・索引ごと作成します。

    crawler と同じく create_all で索引まで導出し、手動リストとの二重管理をしない。
    """
    os.makedirs(tmp_dir(archive_root), exist_ok=True)
    path = db_path(archive_root)
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
    session = sessionmaker(bind=engine)()
    logger.info(f"DB 接続: {path}")
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
                logger.warning(f"死んだプロセス（PID {holder}）のロックを引き継ぎます")
                with contextlib.suppress(OSError):
                    os.unlink(path)
                continue
            raise PreflightError(
                f"保存フォルダは他のプロセス（PID {holder or '不明'}）が使用中です: {path}\n"
                "多重起動でなければ、このロックファイルを削除してください"
            )
    else:  # pragma: no cover - 上の for で必ず break か raise する
        raise PreflightError(f"ロックを取得できません: {path}")

    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield path
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
