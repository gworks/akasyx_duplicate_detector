# crawler_client.py - crawler の起動と fs_files の読み取り（設計書 §3 / §6.4）
#
# crawler は `package = false` のフラット構成（import collector）でライブラリ import に
# 向かないため、CLI としてサブプロセス起動し、出力された file_inventory.db を
# 読み取り専用で参照する。detector の判定ロジックは ScannedFile にしか依存させない。
import contextlib
import logging
import mimetypes
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

import stop_watch
from errors import PreflightError
from utl import hashing
from utl.helpers import connect_readonly, to_posix

logger = logging.getLogger(__name__)

CRAWLER_DB_FILENAME = "file_inventory.db"
CRAWLER_PKG_DIRNAME = "crawler"
# 配布版で同梱する crawler の実行形式の名前（akasyx_search の packaging/build_python.sh と同じ）
CRAWLER_BIN_NAME = "akasyx-crawler"
# 走査が中途半端な状態で移動を始めると、投入元の一部だけが移った状態になる（設計書 §3）
COMPLETED_STATUS = "completed"
# crawler の終わりを待ち直す間隔（秒）。この間隔で中断（KeyboardInterrupt）を受け取れる（#32）
WAIT_INTERVAL = 0.5
# 中断のとき、crawler が自分で止まるのを待つ時間（秒。macOS・Linux）
CRAWLER_STOP_GRACE = 2.0
# uv が先に終わった後、その下の python（crawler）が終わったかを見直す間隔（秒。macOS・Linux。#35）
GROUP_POLL_INTERVAL = 0.05
# Windows の signal には SIGKILL が無い（macOS・Linux の値。グループごと止めるときに使う。#35）
SIGKILL = getattr(signal, "SIGKILL", 9)


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
            f"akasyx_crawler not found: {pkg}\n"
            "Specify the repository path with --crawler-repo"
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
                f"Bundled crawler not found: {exe}\n"
                "Please reinstall the app"
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
            "uv not found; it is required to run the crawler (https://docs.astral.sh/uv/)"
        )
    return ["uv", "run", "main.py"], os.path.join(repo, CRAWLER_PKG_DIRNAME)


def _max_scan_id(db_path: str) -> int:
    """crawler DB の fs_scans の最大 id を返します（DB もテーブルも無ければ 0）。"""
    if not os.path.exists(db_path):
        return 0
    try:
        with contextlib.closing(connect_readonly(db_path)) as conn:
            row = conn.execute("SELECT MAX(id) FROM fs_scans").fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def _read_scan(db_path: str, after_id: int) -> tuple[int | None, str]:
    """after_id より後に作られた最新の fs_scans 行の (id, status) を返します。"""
    with contextlib.closing(connect_readonly(db_path)) as conn:
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
    logger.info(f"Running crawler: {' '.join(cmd)} (cwd={cwd})")
    # detector 側の VIRTUAL_ENV を持ち込むと uv が「プロジェクトの環境と違う」と警告する。
    # crawler は crawler 自身の .venv で動かすべきなので落としておく
    # 停止ファイルの場所も渡さない（detector の中断のためのもの。crawler は detector が止める。#32）
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", stop_watch.STOP_FILE_ENV)}
    # macOS・Linux では自前のプロセスグループ（新しいセッション）で起動する。中断のとき、uv だけでなくその下の python
    # （crawler）までグループごと止められるように（#35）。その代わり UI・端末からの SIGINT は crawler に届かないので、
    # detector が受けた中断を wait_crawler が crawler のグループに送り直す
    popen_kwargs = {} if _is_windows() else {"start_new_session": True}
    proc = None
    started = []  # 起動した crawler（SIGHUP・SIGTERM の送り直し先。起動の前から送り直しの用意をしておく）
    try:
        with _forward_termination(started):
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, **popen_kwargs)
            started.append(proc)
            returncode = wait_crawler(proc)
    except BaseException:
        # 起動してから wait_crawler が後片付けを引き受けるまでの間の中断でも、crawler を残さない（別のグループなので
        # 中断が届いていない）。wait_crawler が止めた後なら何もしない
        if proc is not None and not getattr(proc, "stopped_by_detector", False):
            stop_process_tree(proc)
        raise
    if returncode != 0:
        logger.warning(f"Crawler exited with non-zero status (code {returncode})")

    if not os.path.exists(db_path):
        raise PreflightError(f"Crawler did not produce a DB: {db_path}")

    scan_id, status = _read_scan(db_path, before)
    return CrawlerScan(
        db_path=db_path, scan_id=scan_id, status=status, root_dir=target
    )


def wait_crawler(proc, interval: float = WAIT_INTERVAL) -> int:
    """crawler の終わりを待ちます。中断（KeyboardInterrupt）が来たら crawler を止めてから伝えます（#32）。

    Windows では子プロセスを長く待つ wait は KeyboardInterrupt で抜けない（停止ファイルの見張りが起こす中断が、crawler が
    終わるまで届かない）ので、短い間隔で待ち直す。中断のときは、uv の下の python まで止める（stop_process_tree）。
    """
    # 猶予の間・止める間も SIGHUP・SIGTERM を crawler に送り直す（その間に detector だけが終わらないように）
    with _forward_termination([proc]):
        try:
            while True:
                try:
                    return proc.wait(timeout=interval)
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            # 中断（KeyboardInterrupt）でも、ほかの例外でも、crawler を残さない。
            # crawler も中断を受けていれば（Windows のコンソールの Ctrl+C。macOS・Linux では下で送る SIGINT）、
            # 自分で止まる。少し待ってから止める（すぐ kill すると crawler の後片付けを途中で切る）。待つ間に 2 度目の
            # 中断が来ても、crawler は必ず止める
            try:
                _forward_interrupt(proc)
                _wait_for_exit(proc, CRAWLER_STOP_GRACE)
            finally:
                stop_process_tree(proc)
                proc.stopped_by_detector = True  # run_crawler が止め直さない
            raise


def _wait_for_exit(proc, grace: float) -> None:
    """crawler が自分で止まるのを grace 秒まで待ちます。

    macOS・Linux では uv だけが先に終わっても、その下の python（crawler）が後片付けをしている間は待つ（グループに
    誰も居なくなるまで。#35）。
    """
    deadline = time.monotonic() + grace
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        return
    if _is_windows():
        return
    while time.monotonic() < deadline and _group_alive(proc):
        time.sleep(GROUP_POLL_INTERVAL)


def _group_alive(proc) -> bool:
    """crawler のプロセスグループに、まだ誰か居るか。"""
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 居る（macOS はゾンビだけのグループにも EPERM を返す。そのときは猶予の終わりまで待つだけ）
    except OSError:
        return False
    return True


@contextlib.contextmanager
def _forward_termination(procs: list):
    """macOS・Linux: crawler を待つ間に detector が SIGHUP・SIGTERM で終わるときは、crawler のグループにも送ります。

    crawler は自前のグループなので、端末を閉じたとき（SIGHUP）などに detector だけが終わり、crawler が残って
    crawler の DB に書き続けうる（#35）。送った後は元の扱いに任せる（今までどおりに終わる）。procs は送り先の crawler
    （起動の前から用意できるよう、起動したら足す）。外側で用意済みなら何もしない（二重に送らない）。
    起動（fork）の最中に届いたものは、送り先がまだ分からないので送れない。
    """
    if _is_windows() or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {}

    def forward(signum, frame):
        for proc in procs:
            _signal_group(proc, signum)
        handler = previous[signum]
        if callable(handler):
            # 元の扱いが Python の関数（終わらずに戻ることもある）なら、そのまま呼び、この後も送り直しを続ける
            handler(signum, frame)
            return
        signal.signal(signum, handler)  # 既定の扱いに戻して自分にも送り直す（今までどおりに終わる）
        os.kill(os.getpid(), signum)

    forward.forwards_to_crawler = True

    for name in ("SIGHUP", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        current = signal.getsignal(signum)
        if current == signal.SIG_IGN:
            continue  # 無視する設定（nohup など）。detector も終わらないので crawler にも送らない
        if getattr(current, "forwards_to_crawler", False):
            continue  # 外側（run_crawler）で用意済み
        previous[signum] = current if current is not None else signal.SIG_DFL
        signal.signal(signum, forward)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _is_windows() -> bool:
    return os.name == "nt"


def _signal_group(proc, sig) -> None:
    """crawler のプロセスグループ（run_crawler が自前のグループで起動したもの。pgid は uv の pid）へ送ります。"""
    try:
        os.killpg(proc.pid, sig)
    except OSError:
        pass  # グループにもう誰も居ない（macOS はゾンビだけのグループに EPERM を返すことがある）


def _forward_interrupt(proc) -> None:
    """macOS・Linux: detector が受けた中断を crawler のグループにも送ります（別のグループなので届いていない。#35）。"""
    if not _is_windows():
        _signal_group(proc, signal.SIGINT)


def stop_process_tree(proc) -> None:
    """子プロセスを、その下のプロセスまで止めます。

    Windows は taskkill で木ごと、macOS・Linux はプロセスグループごと（uv を止めても python が残るため。#35）。
    uv が先に終わっていても、その下の python が残りうるので、木・グループごと止める指示は必ず出す。
    """
    if _is_windows():
        subprocess.run(
            ["taskkill", "/pid", str(proc.pid), "/T", "/F"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        _signal_group(proc, SIGKILL)
    try:
        proc.kill()
    except OSError:
        pass  # もう終わっている
    try:
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        logger.warning(f"The crawler process did not stop in time (pid {proc.pid})")


def ensure_completed(scan: CrawlerScan) -> None:
    """スキャンが完走していなければ PreflightError で止めます（設計書 §3）。"""
    if scan.scan_id is None:
        raise PreflightError(
            "Crawler scan result not found (the crawler may have failed to run)"
        )
    if scan.status != COMPLETED_STATUS:
        raise PreflightError(
            f"Crawler scan did not complete (status={scan.status}). "
            "Import is not performed on an incomplete scan"
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


# 今回のスキャンで存在確認された active な行。read_files と count_files で同じ条件を使う
# （条件がずれると、UI の割合が 100% を超えたり届かないまま終わったりする。#18）
_SCANNED_ROWS = "FROM fs_files WHERE last_seen_scan_id = ? AND status = 'active'"


def count_files(db_path: str, scan_id: int) -> int:
    """read_files が返す件数を数えます（読み取り専用。読めなければ sqlite3.Error）。"""
    # sqlite3 の with は commit / rollback だけで接続を閉じないので、closing で閉じる
    with contextlib.closing(connect_readonly(db_path)) as conn:
        return int(conn.execute(f"SELECT COUNT(*) {_SCANNED_ROWS}", (scan_id,)).fetchone()[0])


def read_files(db_path: str, scan_id: int) -> Iterator[ScannedFile]:
    """今回のスキャンで存在確認された active な行を読み出します（読み取り専用）。"""
    sql = (
        "SELECT name, path_abs, path_rel, size, filehash, hash_algo, mime_type,"
        f" modified_at, created_at {_SCANNED_ROWS} ORDER BY path_abs"
    )
    # 途中で止まっても（ジェネレータを閉じたとき）接続を閉じる。sqlite3 の with だけでは閉じない
    with contextlib.closing(connect_readonly(db_path)) as conn:
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


def scan_single_file(path: str, st: os.stat_result | None = None) -> ScannedFile:
    """単一ファイルを crawler を使わずに直接読み取ります（設計書 §6.4）。

    1ファイルのためにサブプロセスを起こす意味がなく、除外指定の取りこぼしも避けられる。
    """
    # 呼び出し側が先に stat していればそれを使う（stat し直す間に消えて、実行全体が落ちないように。#18）
    if st is None:
        st = os.stat(path)
    name = os.path.basename(path)
    try:
        filehash = hashing.file_hash(path)
        algo = hashing.HASH_ALGO
        # ハッシュを取った後に stat し直し、前と同じ版かを確かめる。stat の後・ハッシュの間に書き換わる・差し替わると、
        # 大きさ・日時（前の版）とハッシュ・中身（後の版）が食い違った記録になるので、ハッシュ無しとして移動しない（#18）
        after = os.stat(path)
        if _file_version(after) != _file_version(st):
            logger.warning(f"The file changed while reading; not ingested this time: {path}")
            filehash, algo = None, None
    except OSError as e:
        # 読み取れなければ（ハッシュの間に消えた場合も）ハッシュ無しとして返す（判定側が skipped_nohash にする）
        logger.warning(f"Failed to compute hash: {path}: {e}")
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


def _file_version(st: os.stat_result) -> tuple:
    """同じ版のファイルかを見分ける組（大きさ・更新日時）。

    inode とデバイスは使わない。FUSE・クラウド同期・一部のネットワークのマウントでは、変わっていないファイルでも
    stat のたびに inode が変わることがあり、「読んでいる間に変わった」と取り違えて取り込めなくなるため。
    """
    return (st.st_size, st.st_mtime_ns)


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
