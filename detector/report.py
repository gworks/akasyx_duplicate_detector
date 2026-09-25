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

    lines.append(f"Archive folder: {config.archive_root} (#{aid})")
    lines.append(f"Master DB     : {config.archive_db}")
    lines.append("")
    lines.append("■ Archive folder status")
    if not stats:
        lines.append("  (none registered)")
    for status in sorted(stats):
        bucket = stats[status]
        lines.append(
            f"  {status:<13} {bucket['count']:>6} files  {format_size(bucket['size'])}"
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
        lines.append("■ Disposition flags")
        for name in sorted(counts):
            lines.append(f"  {name:<13} {counts[name]:>6} files")

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
    lines.append(f"■ Unresolved duplicates (still in the source): {unresolved}")
    if unresolved:
        lines.append("  Use delete-duplicates to delete them with verification")

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
    lines = ["", f"■ Recent runs (up to {limit})"]
    if not rows:
        lines.append("  (no history)")
    for r in rows:
        dry = "[dry-run] " if r.dry_run else ""
        lines.append(
            f"  #{r.id:<4} {r.mode:<18} {dry}{r.status:<12} "
            f"moved {r.moved} / duplicates {r.duplicated} / skipped {r.skipped} / failed {r.failed}"
        )
    return lines


def _ingest_detail(session, ingest_id: int) -> list[str]:
    row = session.get(Ingest, ingest_id)
    if row is None:
        return ["", f"■ Run #{ingest_id} not found"]

    lines = ["", f"■ Run #{row.id} ({row.mode})"]
    lines.append(f"  Source      : {row.source_root or '-'}")
    lines.append(f"  Start / end : {row.started_at} / {row.finished_at}")
    lines.append(f"  Status      : {row.status}{' [dry-run]' if row.dry_run else ''}")
    lines.append(f"  Version     : {row.app_version or '-'}")
    if row.stats_json:
        lines.append("  Breakdown   :")
        for key, value in json.loads(row.stats_json).items():
            lines.append(f"    {key:<18} {value}")
    return lines
