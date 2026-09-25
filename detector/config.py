# config.py - CLI 引数解析・設定管理（設計書 §6.2 / §11）
import argparse
import dataclasses
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime

from models import (
    MODE_ADD,
    MODE_ARCHIVES,
    MODE_DELETE_DUPLICATES,
    MODE_REPORT,
    MODE_VERIFY,
    RESULT_ARCHIVE_DUPLICATE,
    RESULT_HASH_MISMATCH,
    RESULT_MISSING,
    RESULT_UNREGISTERED,
)

# 保存先レイアウト（設計書 §7.2）: <YYYY-MM>/<投入元フォルダ名>/<投入元の相対パス>/[001/…]/<ファイル名>
# 1 フォルダに直接置くファイル数の上限。超えたら 001, 002, … の枝に入れる
DEFAULT_FOLDER_LIMIT = 500
# OS / Finder が勝手に作るメタデータ。crawler の走査から除外し（gitignore 記法・名前一致）、
# --prune-empty-dirs ではこれしか無いフォルダを空とみなす
OS_JUNK_FILES = (".DS_Store", "Thumbs.db", "desktop.ini")

QUARANTINE_KINDS = (
    RESULT_MISSING,
    RESULT_UNREGISTERED,
    RESULT_ARCHIVE_DUPLICATE,
    RESULT_HASH_MISMATCH,
)


def repo_root() -> str:
    """リポジトリルートを main.py の位置（detector/）の1階層上として導出します。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _version_file() -> str:
    """version.txt の場所。PyInstaller で固めた実行形式では同梱した写し（--add-data）を読む。"""
    bundle = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle:
        return os.path.join(bundle, "version.txt")
    return os.path.join(repo_root(), "version.txt")


def app_version() -> str:
    """バージョンの正本 <リポジトリルート>/version.txt を返します。

    detector/pyproject.toml の [project] version は配布メタデータで、CI
    （.github/scripts/bump_version.sh）が version.txt と同期する。
    読めない場合は起動を止めず "0.0.0" を返す（表示が 0.0.0 なら配置漏れを疑う）。
    """
    try:
        with open(_version_file(), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "0.0.0"


# 配布版のデータフォルダ名。akasyx_search の ~/Library/Application Support/akasyx/ とは分ける
# （search のデータ移動は akasyx/ の中身を丸ごと移し、削除手順も akasyx/ ごと消すため）
APP_DATA_NAME = "akasyx-duplicate-detector"


def is_packaged() -> bool:
    """配布版（PyInstaller で固めた実行形式。開発中に試すときは AKASYX_PACKAGED=1）か。"""
    return bool(getattr(sys, "frozen", False)) or os.environ.get("AKASYX_PACKAGED") == "1"


def app_home() -> str:
    """配布版のデータフォルダ。AKASYX_DETECTOR_HOME で差し替えられる（テスト用）。

    macOS: ~/Library/Application Support/akasyx-duplicate-detector/
    Windows: %LOCALAPPDATA%/akasyx-duplicate-detector/
    その他: $XDG_DATA_HOME/akasyx-duplicate-detector/（既定 ~/.local/share）
    """
    if os.environ.get("AKASYX_DETECTOR_HOME"):
        return os.path.abspath(os.environ["AKASYX_DETECTOR_HOME"])
    if sys.platform == "darwin":
        return os.path.expanduser(f"~/Library/Application Support/{APP_DATA_NAME}")
    if sys.platform == "win32":
        return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_DATA_NAME)
    return os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), APP_DATA_NAME
    )


def data_root() -> str:
    """正本 DB・作業用 DB・ログを置くフォルダ。

    配布版は app_home()（.app の中は書き込めないため）、開発時は <リポジトリルート>/dist/。
    """
    return app_home() if is_packaged() else os.path.join(repo_root(), "dist")


def _default_db_dir() -> str:
    return os.path.join(data_root(), "db")


def _default_log_dir() -> str:
    return os.path.join(data_root(), "log")


def _default_archive_db() -> str:
    """正本 DB の既定 <データフォルダ>/archive.db。

    保存フォルダの外・ローカルディスクに置く（設計書 §4 / §15）。データフォルダは data_root() を参照。
    """
    return os.path.join(data_root(), "archive.db")


def _default_siblings_bin() -> str:
    """同梱した兄弟の実行形式の置き場（配布版で UI が AKASYX_SIBLINGS_BIN に渡す）。

    空なら開発時の扱いで、crawler は --crawler-repo のソースを `uv run` で起動する。
    """
    return os.environ.get("AKASYX_SIBLINGS_BIN", "")


def _default_crawler_repo() -> str:
    """兄弟ディレクトリの akasyx_crawler を既定とします。"""
    return os.path.join(os.path.dirname(repo_root()), "akasyx_crawler")


@dataclass
class DetectorConfig:
    """実行時設定（設計書 §6.2）。"""

    mode: str
    archive_root: str | None
    source_path: str | None = None
    # 正本 DB のパス（全保存フォルダ共通・1 ファイル）
    archive_db: str = field(default_factory=_default_archive_db)
    # 実行時に resolve_archive() が埋める。ar_archives.id
    archive_id: int | None = None
    dest_subdir: str | None = None
    crawler_repo: str = field(default_factory=_default_crawler_repo)
    # 配布版: <siblings_bin>/akasyx-crawler/akasyx-crawler（PyInstaller onedir）を起動する
    siblings_bin: str = field(default_factory=_default_siblings_bin)
    db_dir: str = field(default_factory=_default_db_dir)
    log_dir: str = field(default_factory=_default_log_dir)
    dry_run: bool = False
    # 1 フォルダに直接置くファイル数の上限。超えたら 001, 002, … の枝に入れる（設計書 §7.2）
    folder_limit: int = DEFAULT_FOLDER_LIMIT
    # 既定 1 = 0 バイトファイルを移動対象から外す（設計書 §7.1 #1）
    min_size: int = 1
    with_meta: bool = False
    follow_symlinks: bool = False
    prune_empty_dirs: bool = False
    # verify
    flag_quarantine: list[str] = field(default_factory=list)
    # delete-duplicates
    ingest_id: int | None = None
    trash_dir: str | None = None
    assume_yes: bool = False


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--archive-db", default=None, metavar="PATH",
        help="path to the master DB (default: <data folder>/archive.db)",
    )
    parser.add_argument(
        "--crawler-repo", default=None, metavar="PATH",
        help="path to the akasyx_crawler repository (default: ../akasyx_crawler)",
    )
    parser.add_argument(
        "--db-dir", default=None,
        help="output directory for crawler DBs (default: <data folder>/db)",
    )
    parser.add_argument(
        "--log-dir", default=None,
        help="output directory for logs and CSV reports (default: <data folder>/log)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="akasyx_duplicate_detector",
        description="Duplicate-detecting archiver: builds a set of files with no "
        "duplicate content in an archive folder (design spec v0.1.0)",
        epilog=f"Data folder (default location for the master DB, working DBs and logs): {data_root()}",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {app_version()}"
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    # --- add -----------------------------------------------------------------
    p_add = sub.add_parser(MODE_ADD, help="scan a source and import its files into the archive folder")
    p_add.add_argument("archive_root", help="archive folder")
    p_add.add_argument("source_path", help="source file or folder")
    p_add.add_argument(
        "--folder-limit", type=int, default=DEFAULT_FOLDER_LIMIT, metavar="N",
        help="max number of files placed directly in one folder; overflow goes into "
        f"001, 002, ... subfolders (default: {DEFAULT_FOLDER_LIMIT})",
    )
    p_add.add_argument(
        "--dest-subdir", default=None, metavar="NAME",
        help="subfolder name under the year-month folder (default: source folder name; "
        "'' for none). Files go to <YYYY-MM>/<NAME>/<path relative to source>/",
    )
    p_add.add_argument(
        "--dry-run", action="store_true",
        help="classify only; do not move files or update the DB",
    )
    p_add.add_argument(
        "--min-size", type=int, default=1, metavar="BYTES",
        help="do not move files smaller than this (default: 1 = skip empty files)",
    )
    p_add.add_argument(
        "--with-meta", action="store_true",
        help="have the crawler also extract format-specific metadata "
        "(requires the crawler's --extra meta)",
    )
    p_add.add_argument(
        "--follow-symlinks", action="store_true", help="follow symbolic links"
    )
    p_add.add_argument(
        "--prune-empty-dirs", action="store_true",
        help="remove source directories left empty after moving "
        "(folders containing only OS metadata such as .DS_Store count as empty)",
    )
    _add_common_args(p_add)

    # --- verify --------------------------------------------------------------
    p_ver = sub.add_parser(MODE_VERIFY, help="check consistency between the archive folder and the DB")
    p_ver.add_argument("archive_root", help="archive folder")
    p_ver.add_argument(
        "--flag-quarantine", nargs="+", default=[], choices=QUARANTINE_KINDS,
        metavar="KIND",
        help="flag findings for later action (disposition=quarantine). "
        f"Kinds: {' / '.join(QUARANTINE_KINDS)}. "
        "This command never moves any files",
    )
    p_ver.add_argument(
        "--follow-symlinks", action="store_true", help="follow symbolic links"
    )
    _add_common_args(p_ver)

    # --- delete-duplicates ---------------------------------------------------
    p_del = sub.add_parser(
        MODE_DELETE_DUPLICATES, help="delete (with verification) duplicates that add left in the source"
    )
    p_del.add_argument("archive_root", help="archive folder")
    p_del.add_argument(
        "--ingest-id", type=int, default=None,
        help="limit to a specific ingest run (default: all pending)",
    )
    p_del.add_argument(
        "--trash-dir", default=None, metavar="DIR",
        help="move to DIR instead of deleting (preserving paths relative to the source)",
    )
    p_del.add_argument(
        "--prune-empty-dirs", action="store_true",
        help="remove source directories left empty after deletion",
    )
    p_del.add_argument(
        "--yes", action="store_true",
        help="actually delete (without this, only list verification results)",
    )
    _add_common_args(p_del)

    # --- report --------------------------------------------------------------
    p_rep = sub.add_parser(MODE_REPORT, help="show archive folder status and run history")
    p_rep.add_argument("archive_root", help="archive folder")
    p_rep.add_argument(
        "--ingest-id", type=int, default=None, help="show the breakdown of a specific run"
    )
    _add_common_args(p_rep)

    # --- archives ------------------------------------------------------------
    p_arc = sub.add_parser(
        MODE_ARCHIVES, help="list archive folders registered in the master DB"
    )
    _add_common_args(p_arc)

    return parser


def parse_arguments(argv: list[str] | None = None) -> DetectorConfig:
    """CLI 引数を解析して DetectorConfig を返します。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "folder_limit", DEFAULT_FOLDER_LIMIT) < 1:
        parser.error("--folder-limit must be 1 or greater")

    return DetectorConfig(
        mode=args.mode,
        archive_root=(
            os.path.abspath(args.archive_root)
            if getattr(args, "archive_root", None)
            else None
        ),
        archive_db=os.path.abspath(args.archive_db or _default_archive_db()),
        source_path=(
            os.path.abspath(args.source_path)
            if getattr(args, "source_path", None)
            else None
        ),
        dest_subdir=getattr(args, "dest_subdir", None),
        crawler_repo=os.path.abspath(args.crawler_repo or _default_crawler_repo()),
        siblings_bin=_default_siblings_bin(),
        db_dir=args.db_dir or _default_db_dir(),
        log_dir=args.log_dir or _default_log_dir(),
        dry_run=getattr(args, "dry_run", False),
        folder_limit=getattr(args, "folder_limit", DEFAULT_FOLDER_LIMIT),
        min_size=getattr(args, "min_size", 1),
        with_meta=getattr(args, "with_meta", False),
        follow_symlinks=getattr(args, "follow_symlinks", False),
        prune_empty_dirs=getattr(args, "prune_empty_dirs", False),
        flag_quarantine=list(getattr(args, "flag_quarantine", [])),
        ingest_id=getattr(args, "ingest_id", None),
        trash_dir=(
            os.path.abspath(args.trash_dir)
            if getattr(args, "trash_dir", None)
            else None
        ),
        assume_yes=getattr(args, "yes", False),
    )


def setup_directories(config: DetectorConfig) -> None:
    """db / log ディレクトリを作成します。"""
    os.makedirs(config.db_dir, exist_ok=True)
    os.makedirs(config.log_dir, exist_ok=True)


def setup_logging(config: DetectorConfig) -> None:
    """ファイル（DEBUG）＋コンソール（INFO）のログを初期化します。"""
    log_file = os.path.join(config.log_dir, f"detector{datetime.now():%Y%m%d}.log")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[file_handler, console_handler],
    )


def config_snapshot(config: DetectorConfig) -> dict:
    """実行時設定のスナップショット（ar_ingests.config_json 用 — 再現性のため）。"""
    return dataclasses.asdict(config)
