# test_progress_output.py - 取り込みの進み具合を UI に伝える出力（#18）
#
# UI（electron-ui/detector-output.js）は stdout の決まった形の行を読んで、段階・全体の件数・処理中のファイル・
# 失敗の一覧を出す。ここで守るのは
# - 段階（scan → ingest → cleanup → done）と全体の件数が、処理を始める前に出る。段階は、前の段階が最後まで
#   済んだときだけ進む（UI は「次の段階が出たら前の段階は完了」とだけ読む）
# - Progress 行の全体の件数が、実際に処理する件数と一致する（100% を超えない・届かないまま終わらない）
# - 全体の件数を数えられないときは 0 件や 100% と出さず、旧形式（件数なし）にする
# - failed になったファイルは、どの経路でも Failed: 行（パスとメッセージ）で出る
import json
import logging
import os
import re
import sqlite3
import sys

import crawler_client
import main
import mover
import pytest
from conftest import write_file

# electron-ui/detector-output.js と同じ
PROGRESS_LINE = re.compile(r"^Progress: (\d+)(?:/(\d+))? files \((.+)\)$")
STAGE_LINE = re.compile(r"^Stage: (scan|ingest|cleanup|done)$")
TOTAL_LINE = re.compile(r"^Total: (\d+) files$")
CURRENT_LINE = re.compile(r"^Current file: (\{.*\})$")
FAILED_LINE = re.compile(r"^Failed: (\{.*\})$")


@pytest.fixture(autouse=True)
def _ui_progress(monkeypatch):
    """UI から起動したときと同じにする（処理中のファイル・失敗の JSON の行は UI のときだけ出る）。"""
    monkeypatch.setenv("AKASYX_UI_PROGRESS", "1")


def _lines(capsys):
    return capsys.readouterr().out.splitlines()


def _matches(lines, pattern):
    return [m for m in (pattern.match(line) for line in lines) if m]


def _add(make_config, archive, source, tmp_path, fake_crawler, **kwargs):
    fake_crawler(source, str(tmp_path / "crawler.db"))
    return main.run(make_config(archive_root=archive, source_path=source, **kwargs))


def test_stages_and_total_come_before_progress(
    make_config, archive, source, tmp_path, fake_crawler, capsys
):
    for name in ("a.txt", "b.txt", "c.txt"):
        write_file(os.path.join(source, name), name.encode())
    capsys.readouterr()
    assert _add(make_config, archive, source, tmp_path, fake_crawler) == main.EXIT_OK
    lines = _lines(capsys)

    stages = [m.group(1) for m in _matches(lines, STAGE_LINE)]
    assert stages == ["scan", "ingest", "cleanup", "done"]
    assert lines.index("Ingest finished") < lines.index("Stage: cleanup")
    assert [m.group(1) for m in _matches(lines, TOTAL_LINE)] == ["3"]
    # 全体の件数は最初のファイルを処理する前に出る（1 件目に時間がかかっても割合が出せる）
    assert lines.index("Total: 3 files") < next(
        i for i, line in enumerate(lines) if PROGRESS_LINE.match(line)
    )

    progress = _matches(lines, PROGRESS_LINE)
    assert [(m.group(1), m.group(2)) for m in progress] == [("1", "3"), ("2", "3"), ("3", "3")]
    assert all(m.group(3) == "moved" for m in progress)

    # 処理中のファイルは、処理する前に 1 件ずつ出る
    current = [json.loads(m.group(1))["path"] for m in _matches(lines, CURRENT_LINE)]
    assert sorted(os.path.basename(p) for p in current) == ["a.txt", "b.txt", "c.txt"]
    first_current = next(i for i, line in enumerate(lines) if CURRENT_LINE.match(line))
    first_progress = next(i for i, line in enumerate(lines) if PROGRESS_LINE.match(line))
    assert first_current < first_progress
    assert not _matches(lines, FAILED_LINE)


def test_single_file_has_no_scan_stage(make_config, archive, tmp_path, capsys):
    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")
    capsys.readouterr()
    assert main.run(make_config(archive_root=archive, source_path=path)) == main.EXIT_OK
    lines = _lines(capsys)
    assert [m.group(1) for m in _matches(lines, STAGE_LINE)] == ["ingest", "cleanup", "done"]
    assert "Total: 1 files" in lines
    assert "Progress: 1/1 files (moved)" in lines


def test_dry_run_reports_total_and_progress(
    make_config, archive, source, tmp_path, fake_crawler, capsys
):
    write_file(os.path.join(source, "a.txt"), b"AAA")
    write_file(os.path.join(source, "b.txt"), b"BBB")
    capsys.readouterr()
    assert _add(make_config, archive, source, tmp_path, fake_crawler, dry_run=True) == main.EXIT_OK
    lines = _lines(capsys)
    assert "Total: 2 files" in lines
    assert "Progress: 2/2 files (moved)" in lines


def test_total_unknown_falls_back_to_old_format(
    make_config, archive, source, tmp_path, fake_crawler, capsys, monkeypatch, caplog
):
    """数えられないときは 0 件・100% にしない。件数なしの旧形式にして、ログに理由を残す。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")

    def broken(db_path, scan_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(crawler_client, "count_files", broken)
    capsys.readouterr()
    with caplog.at_level(logging.WARNING):
        assert _add(make_config, archive, source, tmp_path, fake_crawler) == main.EXIT_OK
    lines = _lines(capsys)
    assert not _matches(lines, TOTAL_LINE)
    assert "Progress: 1 files (moved)" in lines
    assert "Could not count the files to process" in caplog.text


def test_move_failure_is_reported_with_path(
    make_config, archive, source, tmp_path, fake_crawler, capsys, monkeypatch, caplog
):
    src = write_file(os.path.join(source, "sub", "a.txt"), b"AAA")
    monkeypatch.setattr(
        mover, "safe_move", lambda *a, **k: (_ for _ in ()).throw(OSError(22, "Invalid argument"))
    )
    capsys.readouterr()
    with caplog.at_level(logging.WARNING):
        assert _add(make_config, archive, source, tmp_path, fake_crawler) == main.EXIT_HAS_FAILURES
    lines = _lines(capsys)
    failed = [json.loads(m.group(1)) for m in _matches(lines, FAILED_LINE)]
    assert len(failed) == 1
    assert os.path.realpath(failed[0]["path"]) == os.path.realpath(src)
    assert "Invalid argument" in failed[0]["message"]
    assert "Progress: 1/1 files (failed)" in lines
    # ログの警告にも、どのファイルが失敗したかが出る（CSV を開かなくても分かる）
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    # UI から起動したときも、失敗はパス入りの WARNING でログファイルに残る（1 件 1 行）。詳細ログ（コンソール）には
    # 流さない印が付く（失敗が多いと詳細ログを埋めて、ほかの警告を押し出すため。失敗の一覧と CSV にある）
    records = [r for r in caplog.records if r.levelno >= logging.WARNING and "a.txt" in r.getMessage()]
    assert [r.getMessage() for r in records] == [f"Failed: {src}: [Errno 22] Invalid argument"]
    assert all(getattr(r, "file_only", False) for r in records)


def test_cli_reports_each_failure_once_with_path(
    make_config, archive, source, tmp_path, fake_crawler, capsys, monkeypatch, caplog
):
    """CLI では、失敗は 1 件につき WARNING 1 行（Failed: パス: メッセージ）。経路ごとの行と二重にならない。"""
    monkeypatch.delenv("AKASYX_UI_PROGRESS")
    src = write_file(os.path.join(source, "sub", "a.txt"), b"AAA")
    monkeypatch.setattr(
        mover, "safe_move", lambda *a, **k: (_ for _ in ()).throw(OSError(22, "Invalid argument"))
    )
    with caplog.at_level(logging.WARNING):
        assert _add(make_config, archive, source, tmp_path, fake_crawler) == main.EXIT_HAS_FAILURES
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert [m for m in warnings if "a.txt" in m] == [f"Failed: {src}: [Errno 22] Invalid argument"]


def test_classify_error_is_reported_as_failed_line(
    make_config, archive, source, tmp_path, fake_crawler, capsys, monkeypatch
):
    """移動以外の経路（判定中の例外）で failed になっても Failed: 行が出る。"""
    import ingest

    write_file(os.path.join(source, "a.txt"), b"AAA")

    def boom(*a, **k):
        raise RuntimeError("classify boom")

    monkeypatch.setattr(ingest, "judge", boom)
    capsys.readouterr()
    assert _add(make_config, archive, source, tmp_path, fake_crawler) == main.EXIT_HAS_FAILURES
    failed = [json.loads(m.group(1)) for m in _matches(_lines(capsys), FAILED_LINE)]
    assert len(failed) == 1
    assert failed[0]["message"] == "classify boom"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows のファイル名に改行は使えない")
def test_odd_file_names_stay_on_one_line(
    make_config, archive, source, tmp_path, fake_crawler, capsys, monkeypatch
):
    """改行や `: ` を含むファイル名でも 1 行で出て、UI が読み違えない。"""
    odd = write_file(os.path.join(source, "a: b\nc 写真.txt"), b"AAA")
    monkeypatch.setattr(
        mover, "safe_move", lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    )
    capsys.readouterr()
    _add(make_config, archive, source, tmp_path, fake_crawler)
    lines = _lines(capsys)
    current = [json.loads(m.group(1))["path"] for m in _matches(lines, CURRENT_LINE)]
    failed = [json.loads(m.group(1))["path"] for m in _matches(lines, FAILED_LINE)]
    assert [os.path.basename(p) for p in current] == [os.path.basename(odd)]
    assert [os.path.basename(p) for p in failed] == [os.path.basename(odd)]
    # 行は ASCII だけで出る（端末や UI の文字コードに左右されない）
    for line in lines:
        if CURRENT_LINE.match(line) or FAILED_LINE.match(line):
            assert line.isascii()


def test_count_files_matches_read_files(tmp_path, source):
    """件数は read_files と同じ条件で数える（別の条件だと割合が 100% にならない）。"""
    from conftest import build_crawler_db

    write_file(os.path.join(source, "a.txt"), b"A")
    write_file(os.path.join(source, "b.txt"), b"B")
    db = str(tmp_path / "c.db")
    scan_id = build_crawler_db(db, source)
    with sqlite3.connect(db) as conn:
        # 前回のスキャンにしか無い行・active でない行は数えない
        conn.execute(
            "INSERT INTO fs_files (scan_id, last_seen_scan_id, name, path_abs, path_rel, size,"
            " filehash, hash_algo, status) VALUES (?, ?, 'old', '/x/old', 'old', 1, 'h', 'sha256', 'active')",
            (scan_id, scan_id - 1),
        )
        conn.execute(
            "INSERT INTO fs_files (scan_id, last_seen_scan_id, name, path_abs, path_rel, size,"
            " filehash, hash_algo, status) VALUES (?, ?, 'gone', '/x/gone', 'gone', 1, 'h', 'sha256', 'missing')",
            (scan_id, scan_id),
        )
    assert crawler_client.count_files(db, scan_id) == len(list(crawler_client.read_files(db, scan_id))) == 2


def test_single_file_stage_comes_before_hashing(make_config, archive, tmp_path, capsys, monkeypatch):
    """単一ファイルは、ハッシュを取る前に段階を出す（大きいファイルで何も出ないまま待たせない）。"""
    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")
    seen = []
    real = crawler_client.scan_single_file

    def spy(p, *args, **kwargs):
        seen.append(capsys.readouterr().out)
        return real(p, *args, **kwargs)

    monkeypatch.setattr(crawler_client, "scan_single_file", spy)
    capsys.readouterr()
    assert main.run(make_config(archive_root=archive, source_path=path)) == main.EXIT_OK
    assert "Stage: ingest" in seen[0].splitlines()
    lines = seen[0].splitlines() + _lines(capsys)
    # 処理中のファイルもハッシュの前に出る。1 件につき 1 回だけ
    assert [json.loads(m.group(1))["path"] for m in _matches(seen[0].splitlines(), CURRENT_LINE)] == [path]
    assert len(_matches(lines, CURRENT_LINE)) == 1
    assert [m.group(1) for m in _matches(lines, STAGE_LINE)] == ["ingest", "cleanup", "done"]


def test_incomplete_scan_does_not_enter_ingest_stage(
    make_config, archive, source, tmp_path, fake_crawler, capsys
):
    """crawler が完走していなければ、判定と移動の段階には入らない（止まったのは一覧作成）。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "crawler.db"), scan_status="interrupted")
    capsys.readouterr()
    from errors import PreflightError

    with pytest.raises(PreflightError, match="did not complete"):
        main.run(make_config(archive_root=archive, source_path=source))
    lines = _lines(capsys)
    assert [m.group(1) for m in _matches(lines, STAGE_LINE)] == ["scan"]


def test_cli_does_not_print_json_lines(
    make_config, archive, source, tmp_path, fake_crawler, capsys, monkeypatch, caplog
):
    """CLI（UI から起動していない）では、1 件ごとの JSON の行を出さない。失敗はパス入りの WARNING で分かる。"""
    monkeypatch.delenv("AKASYX_UI_PROGRESS")
    write_file(os.path.join(source, "写真.txt"), b"AAA")
    monkeypatch.setattr(
        mover, "safe_move", lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    )
    capsys.readouterr()
    with caplog.at_level(logging.WARNING):
        _add(make_config, archive, source, tmp_path, fake_crawler)
    lines = _lines(capsys)
    assert not _matches(lines, CURRENT_LINE)
    assert not _matches(lines, FAILED_LINE)
    # UI 向けの区切りの行も出さない
    assert "Crawler finished" not in lines
    assert "Ingest finished" not in lines
    # 段階・件数・進み具合は短いので CLI でも出す
    assert "Total: 1 files" in lines
    assert "Progress: 1/1 files (failed)" in lines
    assert "写真.txt" in caplog.text


def test_interrupted_run_stops_at_ingest_stage(
    make_config, archive, source, tmp_path, fake_crawler, capsys, monkeypatch
):
    """中断したら判定と移動の段階のまま終わる（後片付けは走るが、判定が済んだとは出さない）。"""
    import ingest

    write_file(os.path.join(source, "a.txt"), b"AAA")
    write_file(os.path.join(source, "b.txt"), b"BBB")

    def stop(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(ingest, "judge", stop)
    capsys.readouterr()
    _add(make_config, archive, source, tmp_path, fake_crawler)
    lines = _lines(capsys)
    assert [m.group(1) for m in _matches(lines, STAGE_LINE)] == ["scan", "ingest"]
    # 判定のループを抜けたことは出る（UI が処理中のファイルと残り時間を止める）
    assert "Ingest finished" in lines


def test_total_line_is_flushed(make_config, archive, source, tmp_path, fake_crawler, monkeypatch):
    """全体の件数の行は、1 件目の処理を待たずに届く（PYTHONUNBUFFERED が効かない起動でも）。"""
    import builtins

    write_file(os.path.join(source, "a.txt"), b"AAA")
    flushed = []
    real_print = builtins.print

    def spy(*args, **kwargs):
        if args and str(args[0]).startswith("Total:"):
            flushed.append(kwargs.get("flush", False))
        return real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", spy)
    _add(make_config, archive, source, tmp_path, fake_crawler)
    assert flushed == [True]


def test_crawler_finished_line_even_when_crawler_fails(
    make_config, archive, source, tmp_path, monkeypatch, capsys
):
    """crawler が例外で終わっても `Crawler finished` を出す（後の detector 自身の行を crawler の出力と取り違えない）。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")
    monkeypatch.setattr(crawler_client, "resolve_crawler_repo", lambda path: path)

    def broken(*a, **k):
        raise OSError("crawler binary not found")

    monkeypatch.setattr(crawler_client, "run_crawler", broken)
    capsys.readouterr()
    assert main.run(make_config(archive_root=archive, source_path=source)) == main.EXIT_FATAL
    lines = _lines(capsys)
    # detector 自身のサマリ（致命的エラー）は Crawler finished より後に出る
    summary = next(i for i, line in enumerate(lines) if line.startswith("[Summary]"))
    assert lines.index("Stage: scan") < lines.index("Crawler finished") < summary


def test_single_file_hash_can_be_interrupted(make_config, archive, tmp_path, capsys, monkeypatch):
    """単一ファイルのハッシュ中に中断しても、ディレクトリと同じく中断として扱い、判定の終わりを出す。"""
    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")

    def stop(p, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(crawler_client, "scan_single_file", stop)
    capsys.readouterr()
    main.run(make_config(archive_root=archive, source_path=path))
    lines = _lines(capsys)
    assert "Ingest finished" in lines
    assert [m.group(1) for m in _matches(lines, STAGE_LINE)] == ["ingest"]
    assert os.path.exists(path)


def test_crawler_db_path_with_uri_characters(tmp_path, source):
    """crawler の DB の場所に URI で意味を持つ文字（# ? %）があっても読める（数えられずに件数が消えない）。"""
    from conftest import build_crawler_db

    write_file(os.path.join(source, "a.txt"), b"A")
    db = str(tmp_path / "data#1 ?x %41" / "c.db")
    scan_id = build_crawler_db(db, source)
    assert crawler_client.count_files(db, scan_id) == 1
    assert len(list(crawler_client.read_files(db, scan_id))) == 1


@pytest.mark.parametrize(
    "path, windows, expected",
    [
        ("/Users/a/data#1/c.db", False, "file:///Users/a/data%231/c.db?mode=ro"),
        ("/Users/a/b\\c/x.db", False, "file:///Users/a/b%5Cc/x.db?mode=ro"),  # macOS では \\ はファイル名の文字
        ("C:\\Users\\a\\c.db", True, "file:///C:/Users/a/c.db?mode=ro"),
        # ネットワーク共有（UNC）。file://nas/... だと SQLite がホスト名として断る
        ("\\\\nas\\share\\akasyx\\c.db", True, "file:////nas/share/akasyx/c.db?mode=ro"),
        # Windows の拡張長パス（\\?\ と \\?\UNC\）は接頭辞を外す（? がエスケープされて別のパスになる）
        ("\\\\?\\C:\\long\\c.db", True, "file:///C:/long/c.db?mode=ro"),
        ("\\\\?\\UNC\\nas\\share\\c.db", True, "file:////nas/share/c.db?mode=ro"),
        # UTF-8 でない名前（surrogateescape）は元のバイトのままエスケープする（落ちて取り込みを止めない）
        ("/data/\udcff/c.db", False, "file:///data/%FF/c.db?mode=ro"),
    ],
)
def test_readonly_uri_forms(path, windows, expected):
    from utl.helpers import sqlite_uri_for

    assert sqlite_uri_for(path, windows=windows) == expected


def test_missing_single_file_fails_before_csv(make_config, archive, session, ingest_row, tmp_path, capsys):
    """単一ファイルが事前チェックの後に消えていたら、CSV を作る前・判定の段階に入る前に止まる（ハッシュだけを遅らせる）。"""
    import ingest

    config = make_config(archive_root=archive, source_path=str(tmp_path / "gone.txt"))
    with pytest.raises(FileNotFoundError):
        ingest.run_add(session, config, ingest_row)
    assert not [f for f in os.listdir(config.log_dir) if f.startswith("add_result")]
    assert "Stage: ingest" not in _lines(capsys)


def test_ingest_finished_even_if_interrupted_before_loop(
    make_config, archive, source, tmp_path, fake_crawler, capsys, monkeypatch
):
    """判定の段階に入った後なら、ループの前の準備（件数の数え上げなど）で止まっても判定の終わりを出す。"""
    import ingest

    write_file(os.path.join(source, "a.txt"), b"AAA")

    def stop(scan):
        raise KeyboardInterrupt

    monkeypatch.setattr(ingest, "_count_total", stop)
    capsys.readouterr()
    # 件数を数えている間の中断も、ループ中と同じく「中断」として終わる（致命的エラーにしない）
    _add(make_config, archive, source, tmp_path, fake_crawler)
    lines = _lines(capsys)
    assert lines.index("Stage: ingest") < lines.index("Ingest finished")
    assert any("run was interrupted" in line for line in lines)
    assert os.path.exists(os.path.join(source, "a.txt"))


def test_single_file_vanishing_after_precheck_is_one_result(make_config, archive, tmp_path, monkeypatch, capsys):
    """単一ファイルが事前の stat の後（ハッシュの前）に消えても、実行全体を落とさず 1 件の結果として残す。

    事前の stat の結果を使い、ループの中で stat し直さない（読めなければハッシュなしとして扱われる）。
    """
    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")
    real = crawler_client.scan_single_file

    def vanish_then_scan(p, *args, **kwargs):
        os.unlink(p)
        return real(p, *args, **kwargs)

    monkeypatch.setattr(crawler_client, "scan_single_file", vanish_then_scan)
    capsys.readouterr()
    assert main.run(make_config(archive_root=archive, source_path=path)) != main.EXIT_FATAL
    lines = _lines(capsys)
    assert any(line.startswith("Progress: 1/1 files (") for line in lines)


def test_setup_error_after_ingest_stage_is_not_masked(
    make_config, archive, source, tmp_path, fake_crawler, capsys, monkeypatch
):
    """判定の段階に入った後の準備で落ちたら、その例外がそのまま伝わる（後の処理の UnboundLocalError に化けない）。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")

    def broken(*a, **k):
        raise RuntimeError("planner broken")

    monkeypatch.setattr(mover, "DestPlanner", broken)
    capsys.readouterr()
    assert _add(make_config, archive, source, tmp_path, fake_crawler) == main.EXIT_FATAL
    lines = _lines(capsys)
    assert lines.index("Stage: ingest") < lines.index("Ingest finished")


def test_file_only_records_skip_the_console(tmp_path, make_config):
    """file_only の印が付いた記録は、ログファイルには書くがコンソール（UI の詳細ログ）には出さない。"""
    import config as config_module

    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = []
    try:
        cfg = make_config(log_dir=str(tmp_path / "logs"))
        config_module.setup_logging(cfg)
        console = next(h for h in root.handlers if not isinstance(h, logging.FileHandler))
        quiet = logging.LogRecord("ingest", logging.WARNING, __file__, 1, "Failed: x", None, None)
        quiet.file_only = True
        loud = logging.LogRecord("ingest", logging.WARNING, __file__, 1, "Failed: y", None, None)
        assert not console.filter(quiet)
        assert console.filter(loud)
        file_handler = next(h for h in root.handlers if isinstance(h, logging.FileHandler))
        assert file_handler.filter(quiet)
    finally:
        for h in root.handlers:
            h.close()
        root.handlers = saved


def test_files_iterator_is_closed_when_interrupted(
    make_config, archive, source, tmp_path, fake_crawler, monkeypatch
):
    """中断したら、一覧のジェネレータ（crawler DB の接続）をループを抜けたところで閉じる（後片付けの間まで開いたままにしない）。"""
    import ingest

    write_file(os.path.join(source, "a.txt"), b"AAA")
    write_file(os.path.join(source, "b.txt"), b"BBB")
    closed = []
    real_read = crawler_client.read_files

    def tracking_read(db_path, scan_id):
        try:
            yield from real_read(db_path, scan_id)
        finally:
            closed.append(True)

    def stop(*a, **k):
        raise KeyboardInterrupt

    seen_at_cleanup = []
    real_report = ingest._report_unavailable_owners

    def report(*a, **k):
        seen_at_cleanup.append(bool(closed))
        return real_report(*a, **k)

    monkeypatch.setattr(crawler_client, "read_files", tracking_read)
    monkeypatch.setattr(ingest, "judge", stop)
    monkeypatch.setattr(ingest, "_report_unavailable_owners", report)
    _add(make_config, archive, source, tmp_path, fake_crawler)
    assert seen_at_cleanup == [True]


def test_single_file_changed_while_hashing_is_not_moved(make_config, archive, tmp_path, monkeypatch, caplog):
    """単一ファイルが stat の後・ハッシュの間に書き換わったら、大きさとハッシュが別の版にならないよう移動しない。"""
    from utl import hashing

    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")
    real_hash = hashing.file_hash

    def rewrite_then_hash(p, *a, **k):
        with open(p, "ab") as f:
            f.write(b"MORE")  # ハッシュを取っている間に書き足された
        return real_hash(p, *a, **k)

    monkeypatch.setattr(hashing, "file_hash", rewrite_then_hash)
    with caplog.at_level(logging.WARNING):
        main.run(make_config(archive_root=archive, source_path=path))
    assert os.path.exists(path)  # 移動していない
    assert "changed while reading" in caplog.text


def test_total_unknown_when_uri_cannot_be_encoded(
    make_config, archive, source, tmp_path, fake_crawler, capsys, monkeypatch
):
    """crawler DB の URI を作れない（Windows の対になっていないサロゲートなど）ときも、件数なしで続ける（落とさない）。"""
    write_file(os.path.join(source, "a.txt"), b"AAA")

    def broken(db_path, scan_id):
        raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")

    monkeypatch.setattr(crawler_client, "count_files", broken)
    capsys.readouterr()
    assert _add(make_config, archive, source, tmp_path, fake_crawler) == main.EXIT_OK
    assert "Progress: 1 files (moved)" in _lines(capsys)


def test_unstable_inode_is_not_taken_as_change(make_config, archive, tmp_path, monkeypatch):
    """stat のたびに inode が変わるマウントでも、中身が変わっていなければ取り込む（読んでいる間に変わったと取り違えない）。"""
    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")
    real_stat = os.stat
    counter = [0]

    class Shifted:
        def __init__(self, st):
            self._st = st
            counter[0] += 1
            self.st_ino = st.st_ino + counter[0]

        def __getattr__(self, name):
            return getattr(self._st, name)

    def shifting_stat(p, *a, **k):
        st = real_stat(p, *a, **k)
        return Shifted(st) if os.fspath(p) == path else st

    monkeypatch.setattr(crawler_client.os, "stat", shifting_stat)
    assert main.run(make_config(archive_root=archive, source_path=path)) == main.EXIT_OK
    assert not os.path.exists(path)  # 取り込まれて移動した
