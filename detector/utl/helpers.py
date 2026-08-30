# helpers.py - 共通ヘルパー
import json
import os
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


def is_nested(outer: str, inner: str) -> bool:
    """inner が outer の配下（または同一）かを判定します（設計書 §12）。

    シンボリックリンク経由の入れ子を見逃さないよう realpath で正規化する。
    """
    outer = os.path.realpath(outer)
    inner = os.path.realpath(inner)
    try:
        return os.path.commonpath([outer, inner]) == outer
    except ValueError:
        # 異なるドライブ（Windows）は commonpath が例外を投げる = 入れ子ではない
        return False


def format_size(num: int) -> str:
    """バイト数を人間可読な文字列にします（レポート表示用）。"""
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} TiB"  # pragma: no cover - 上のループで必ず返る
