# hashing.py - SHA-256 のストリーミング計算（設計書 §6.9）
#
# crawler と同じアルゴリズム（標準 hashlib の sha256）を使う。ここで独自の高速化を
# 入れて結果が変わると重複判定が壊れるため、hashlib 以外は使わない。
import hashlib

HASH_CHUNK_SIZE = 1024 * 1024  # 1MB（crawler の utl/fileinfo.py と揃える）
HASH_ALGO = "sha256"


def file_hash(path: str) -> str:
    """ファイルの SHA-256 を16進小文字で返します。読み取り失敗は OSError を送出します。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(HASH_CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()
