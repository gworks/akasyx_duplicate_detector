# report.py - 保存フォルダの状態と実行履歴の表示（設計書 §11）
import json

from models import (
    RESULT_DUPLICATE,
    ArchiveFile,
    Ingest,
    IngestItem,
)
from utl.helpers import format_size
from verify import archive_stats


def run_report(session, config, ingest) -> tuple[str, dict]:
    """保存フォルダの状態を表示します（DB も実体も変更しない）。"""
    lines: list[str] = []
    aid = config.archive_id
    stats = archive_stats(session, aid)

    lines.append(f"保存用フォルダ: {config.archive_root}（#{aid}）")
    lines.append(f"正本 DB      : {config.archive_db}")
    lines.append("")
    lines.append("■ 保存フォルダの状態")
    if not stats:
        lines.append("  （登録なし）")
    for status in sorted(stats):
        bucket = stats[status]
        lines.append(
            f"  {status:<13} {bucket['count']:>6} 件  {format_size(bucket['size'])}"
        )

    dispositions = (
        session.query(ArchiveFile.disposition)
        .filter(ArchiveFile.archive_id == aid, ArchiveFile.disposition.isnot(None))
        .all()
    )
    if dispositions:
        counts: dict[str, int] = {}
        for (d,) in dispositions:
            counts[d] = counts.get(d, 0) + 1
        lines.append("")
        lines.append("■ 処置予定フラグ（disposition）")
        for name in sorted(counts):
            lines.append(f"  {name:<13} {counts[name]:>6} 件")

    unresolved = (
        session.query(IngestItem)
        .join(Ingest, IngestItem.ingest_id == Ingest.id)
        .filter(
            Ingest.archive_id == aid,
            IngestItem.result == RESULT_DUPLICATE,
            IngestItem.resolution.is_(None),
            Ingest.dry_run == 0,
        )
        .count()
    )
    lines.append("")
    lines.append(f"■ 未処置の重複（投入元に残っている）: {unresolved} 件")
    if unresolved:
        lines.append("  delete-duplicates で検証付きの削除ができます")

    if config.ingest_id is not None:
        lines.extend(_ingest_detail(session, config.ingest_id))
    else:
        lines.extend(_recent_ingests(session, aid, exclude_id=ingest.id))

    text = "\n".join(lines)
    print(text)
    return "completed", {"archive_statuses": len(stats), "unresolved": unresolved}


def _recent_ingests(
    session, archive_id: int, limit: int = 10, exclude_id: int | None = None
) -> list[str]:
    query = session.query(Ingest).filter(Ingest.archive_id == archive_id)
    if exclude_id is not None:
        query = query.filter(Ingest.id != exclude_id)  # 実行中の report 自身は出さない
    rows = query.order_by(Ingest.id.desc()).limit(limit).all()
    lines = ["", f"■ 直近の実行（最大 {limit} 件）"]
    if not rows:
        lines.append("  （履歴なし）")
    for r in rows:
        dry = "[dry-run] " if r.dry_run else ""
        lines.append(
            f"  #{r.id:<4} {r.mode:<18} {dry}{r.status:<12} "
            f"移動 {r.moved} / 重複 {r.duplicated} / スキップ {r.skipped} / 失敗 {r.failed}"
        )
    return lines


def _ingest_detail(session, ingest_id: int) -> list[str]:
    row = session.get(Ingest, ingest_id)
    if row is None:
        return ["", f"■ 実行 #{ingest_id} は見つかりません"]

    lines = ["", f"■ 実行 #{row.id}（{row.mode}）"]
    lines.append(f"  投入元      : {row.source_root or '-'}")
    lines.append(f"  開始 / 終了 : {row.started_at} / {row.finished_at}")
    lines.append(f"  ステータス  : {row.status}{' [dry-run]' if row.dry_run else ''}")
    lines.append(f"  バージョン  : {row.app_version or '-'}")
    if row.stats_json:
        lines.append("  内訳        :")
        for key, value in json.loads(row.stats_json).items():
            lines.append(f"    {key:<18} {value}")
    return lines
