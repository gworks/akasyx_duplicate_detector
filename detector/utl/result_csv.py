# result_csv.py - CSV レポート（設計書 §7.5 — crawler の utl/result_csv.py と同形式）
import csv
import logging
import os

logger = logging.getLogger(__name__)

RESULT_CSV_HEADERS = ["ファイル名", "元パス", "判定", "サイズ", "保存先", "備考"]


def create_csv(log_dir: str, csv_name: str, ts_start: str) -> str:
    """CSV レポートファイルを作成してパスを返します。

    ファイル名形式: {csv_name}_{YYYYMMDD_HHMMSS}.csv
    """
    csv_file = os.path.join(log_dir, f"{csv_name}_{ts_start}.csv")
    with open(csv_file, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow(RESULT_CSV_HEADERS)
    return csv_file


def csv_update(csv_file: str, row: list) -> None:
    """CSV に1行追記します（追記モード・O(1)）。

    書き込み失敗は処理を止めず WARNING に局所化する。
    """
    try:
        with open(csv_file, "a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(row)
    except (OSError, csv.Error, UnicodeError) as e:
        logger.warning(f"CSV 書き込みに失敗: {csv_file}: {e}")
