# helpers.py - 共通ヘルパー
import json
import os
import sys
from datetime import datetime


def datetime_serializer(o):
    """datetime オブジェクトを ISO 8601 形式に変換します（json.dumps の default 用）。"""
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


def json_dumps(obj) -> str:
    """datetime を ISO 8601 化しつつ JSON 文字列に変換します。"""
    return json.dumps(obj, ensure_ascii=False, default=datetime_serializer)


def to_posix(path: str) -> str:
    """OS 依存のパス区切りを POSIX 区切りに正規化します（DB へは常にこの形で入れる）。"""
    return path.replace(os.sep, "/").replace("\\", "/").strip("/")


def from_posix(archive_root: str, rel: str) -> str:
    """保存フォルダ相対の POSIX パスを、この OS の絶対パスに戻します。"""
    return os.path.join(archive_root, *[p for p in rel.split("/") if p])


# 既定のファイルシステムが大文字小文字を区別しない OS（macOS の APFS / Windows の NTFS）
_CASE_INSENSITIVE = sys.platform in ("darwin", "win32")


def path_key(path: str, real: bool = True) -> str:
    """パスを突き合わせるための比較キー。

    real=True はシンボリックリンクを解いた実体の位置（realpath）。macOS / Windows では
    大文字小文字違いも同じパスとみなす（realpath はまだ無いパスや macOS では表記を実体に
    合わせないため）。区別するボリュームでは取りこぼしより入れ子の見逃しが危険なので安全側。
    """
    p = os.path.realpath(path) if real else os.path.abspath(path)
    p = os.path.normcase(p)
    return p.casefold() if _CASE_INSENSITIVE else p


def key_within(outer_key: str, inner_key: str) -> bool:
    """比較キー同士で、inner が outer の配下（または同一）か。"""
    return inner_key == outer_key or inner_key.startswith(outer_key.rstrip(os.sep) + os.sep)


def is_nested(outer: str, inner: str) -> bool:
    """inner が outer の配下（または同一）かを判定します（設計書 §12）。

    シンボリックリンク経由の入れ子を見逃さないよう realpath で正規化する。
    """
    return key_within(path_key(outer), path_key(inner))


def format_size(num: int) -> str:
    """バイト数を人間可読な文字列にします（レポート表示用）。"""
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} TiB"  # pragma: no cover - 上のループで必ず返る
