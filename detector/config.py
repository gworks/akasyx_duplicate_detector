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


def app_version() -> str:
    """バージョンの正本 <リポジトリルート>/version.txt を返します。

    detector/pyproject.toml の [project] version は配布メタデータで、CI
    （.github/scripts/bump_version.sh）が version.txt と同期する。
    読めない場合は起動を止めず "0.0.0" を返す（表示が 0.0.0 なら配置漏れを疑う）。
    """
    try:
        with open(os.path.join(repo_root(), "version.txt"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "0.0.0"


def _default_db_dir() -> str:
    return os.path.join(repo_root(), "dist", "db")


def _default_log_dir() -> str:
    return os.path.join(repo_root(), "dist", "log")


def _default_archive_db() -> str:
    """正本 DB の既定 <リポジトリルート>/dist/archive.db（v0.2.0）。

    保存フォルダの外・ローカルディスクに置く。将来 Electron を実行形式にしたときは
    実行ファイルと同階層に置く方針（設計書 §4 / §15）。
    """
    return os.path.join(repo_root(), "dist", "archive.db")


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
        help="正本 DB のパス（既定: <リポジトリルート>/dist/archive.db）",
    )
    parser.add_argument(
        "--crawler-repo", default=None, metavar="PATH",
        help="akasyx_crawler リポジトリのパス（既定: ../akasyx_crawler）",
    )
    parser.add_argument(
        "--db-dir", default=None,
        help="crawler の DB 出力先（既定: <リポジトリルート>/dist/db）",
    )
    parser.add_argument(
        "--log-dir", default=None,
        help="ログ・CSV 出力先（既定: <リポジトリルート>/dist/log）",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="akasyx_duplicate_detector",
        description="重複判定アーカイバ: 保存用フォルダに内容重複のないファイル集合を"
        "構築します（設計書 v0.1.0）",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {app_version()}"
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    # --- add -----------------------------------------------------------------
    p_add = sub.add_parser(MODE_ADD, help="投入元を探索して保存フォルダへ取り込む")
    p_add.add_argument("archive_root", help="保存用フォルダ")
    p_add.add_argument("source_path", help="投入元のファイルまたはフォルダ")
    p_add.add_argument(
        "--folder-limit", type=int, default=DEFAULT_FOLDER_LIMIT, metavar="N",
        help="1 フォルダに直接置くファイル数の上限。超えたら 001, 002, … の枝フォルダに入れる"
        f"（既定 {DEFAULT_FOLDER_LIMIT}）",
    )
    p_add.add_argument(
        "--dest-subdir", default=None, metavar="NAME",
        help="年月フォルダの下に置く階層名（既定: 投入元フォルダ名 / '' で付けない）。"
        "保存先は <YYYY-MM>/<NAME>/<投入元の相対パス>/ になる",
    )
    p_add.add_argument(
        "--dry-run", action="store_true",
        help="判定だけ行い、移動も DB 更新もしない",
    )
    p_add.add_argument(
        "--min-size", type=int, default=1, metavar="BYTES",
        help="このサイズ未満は移動しない（既定 1 = 0 バイトを除外）",
    )
    p_add.add_argument(
        "--with-meta", action="store_true",
        help="crawler に形式別メタデータも抽出させる（crawler 側の --extra meta が必要）",
    )
    p_add.add_argument(
        "--follow-symlinks", action="store_true", help="シンボリックリンクを追跡する"
    )
    p_add.add_argument(
        "--prune-empty-dirs", action="store_true",
        help="移動後に空になった投入元ディレクトリを削除する"
        "（.DS_Store 等の OS メタデータしか無いフォルダも空とみなす）",
    )
    _add_common_args(p_add)

    # --- verify --------------------------------------------------------------
    p_ver = sub.add_parser(MODE_VERIFY, help="保存フォルダと DB の整合性を検査する")
    p_ver.add_argument("archive_root", help="保存用フォルダ")
    p_ver.add_argument(
        "--flag-quarantine", nargs="+", default=[], choices=QUARANTINE_KINDS,
        metavar="KIND",
        help="検出結果に処置予定フラグ（disposition=quarantine）を立てる。"
        f"指定できる種別: {' / '.join(QUARANTINE_KINDS)}。"
        "このコマンドはファイルを一切動かさない",
    )
    p_ver.add_argument(
        "--follow-symlinks", action="store_true", help="シンボリックリンクを追跡する"
    )
    _add_common_args(p_ver)

    # --- delete-duplicates ---------------------------------------------------
    p_del = sub.add_parser(
        MODE_DELETE_DUPLICATES, help="add で投入元に据え置いた重複を検証付きで削除する"
    )
    p_del.add_argument("archive_root", help="保存用フォルダ")
    p_del.add_argument(
        "--ingest-id", type=int, default=None,
        help="対象を特定の取り込み実行に絞る（既定: 未処置のすべて）",
    )
    p_del.add_argument(
        "--trash-dir", default=None, metavar="DIR",
        help="削除せず DIR へ退避する（投入元の相対パス構造を再現）",
    )
    p_del.add_argument(
        "--prune-empty-dirs", action="store_true",
        help="削除後に空になった投入元ディレクトリを削除する",
    )
    p_del.add_argument(
        "--yes", action="store_true",
        help="実際に削除する（未指定なら検証結果の一覧表示のみ）",
    )
    _add_common_args(p_del)

    # --- report --------------------------------------------------------------
    p_rep = sub.add_parser(MODE_REPORT, help="保存フォルダの状態と実行履歴を表示する")
    p_rep.add_argument("archive_root", help="保存用フォルダ")
    p_rep.add_argument(
        "--ingest-id", type=int, default=None, help="特定の実行の内訳を表示する"
    )
    _add_common_args(p_rep)

    # --- archives ------------------------------------------------------------
    p_arc = sub.add_parser(
        MODE_ARCHIVES, help="正本 DB に登録されている保存フォルダの一覧を表示する"
    )
    _add_common_args(p_arc)

    return parser


def parse_arguments(argv: list[str] | None = None) -> DetectorConfig:
    """CLI 引数を解析して DetectorConfig を返します。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "folder_limit", DEFAULT_FOLDER_LIMIT) < 1:
        parser.error("--folder-limit は 1 以上を指定してください")

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
