# test_batch_files.py - 複数のファイルを 1 回の add で取り込む（#37）
#
# かんたん取り込みで多くのファイルを落とすと、1 件ずつ detector を起動していた（uv・Python の起動と正本 DB のロックの
# 取り直しが件数分起きる）。add に複数のファイルを渡せるようにし、1 回の実行で取り込む。実行の記録（ar_ingests）は
# ファイルごとに分け（履歴・delete-duplicates の掃除の範囲は今と同じ）、出力（CSV・件数・サマリ）は 1 回の実行の形にする。
import csv
import logging
import os
import re
from datetime import datetime

import crawler_client
import ingest
import main
import pytest
from conftest import archive_db_path, write_file
from database import get_session
from models import Ingest, IngestItem


@pytest.fixture(autouse=True)
def _info_logs(caplog):
    caplog.set_level(logging.INFO)  # CSV の場所は INFO のログに出る


def _ingests(tmp_path) -> list[Ingest]:
    session, engine = get_session(archive_db_path(tmp_path))
    try:
        rows = session.query(Ingest).order_by(Ingest.id).all()
        for r in rows:
            session.expunge(r)
        return rows
    finally:
        session.close()
        engine.dispose()


def _items(tmp_path) -> list[IngestItem]:
    session, engine = get_session(archive_db_path(tmp_path))
    try:
        rows = session.query(IngestItem).order_by(IngestItem.id).all()
        for r in rows:
            session.expunge(r)
        return rows
    finally:
        session.close()
        engine.dispose()


def _files(tmp_path, contents):
    folder = tmp_path / "drop"
    return [write_file(str(folder / f"f{i}.txt"), c) for i, c in enumerate(contents)]


def _csv_rows(caplog) -> list[list[str]]:
    paths = re.findall(r"CSV report: (.+)", caplog.text)
    assert len(paths) == 1, caplog.text  # CSV は実行全体で 1 つ
    with open(paths[0].strip(), encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))[1:]


def _month_dir(archive):
    return os.path.join(archive, f"{datetime.now():%Y-%m}")


def test_cli_accepts_several_files(archive, tmp_path):
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    code = main.main(
        ["add", archive, *paths,
         "--db-dir", str(tmp_path / "db"), "--log-dir", str(tmp_path / "log"),
         "--archive-db", archive_db_path(tmp_path)]
    )
    assert code == main.EXIT_OK
    for p in paths:
        assert not os.path.exists(p)
        assert os.path.exists(os.path.join(_month_dir(archive), os.path.basename(p)))


def test_each_file_gets_its_own_ingest_record(make_config, archive, tmp_path):
    paths = _files(tmp_path, [b"A", b"B", b"A"])
    config = make_config(source_path=paths[0], source_paths=paths)
    assert main.run(config) == main.EXIT_OK
    rows = _ingests(tmp_path)
    # 履歴・delete-duplicates の掃除の範囲（source_root）は、1 件ずつ取り込んだときと同じ
    assert [r.source_root for r in rows] == paths
    assert [r.status for r in rows] == ["completed"] * 3
    assert [(r.moved, r.duplicated) for r in rows] == [(1, 0), (1, 0), (0, 1)]
    # 判定の記録は、その実行の記録に付く
    items = _items(tmp_path)
    assert [(i.ingest_id, i.source_path_abs) for i in items] == [(r.id, r.source_root) for r in rows]
    assert os.path.exists(paths[2])  # 重複は投入元に残る


def test_output_is_one_run(make_config, archive, tmp_path, capsys, monkeypatch, caplog):
    monkeypatch.setenv(ingest.UI_PROGRESS_ENV, "1")
    paths = _files(tmp_path, [b"A", b"B", b"A"])
    capsys.readouterr()
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_OK
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert out.count("Total: 3 files") == 1
    assert out.count("Stage: ingest") == 1
    assert out.count("Ingest finished") == 1
    assert out.count("Stage: done") == 1
    assert out.count("[Summary]") == 1
    # 進み具合は通し番号（UI は「n/全体」で件数と残り時間を出す）
    assert re.findall(r"Progress: (\d+)/3 files", out) == ["1", "2", "3"]
    assert "[Summary] moved: 2, duplicates (left in place): 1" in out
    rows = _csv_rows(caplog)
    assert [(r[1], r[2]) for r in rows] == [(paths[0], "moved"), (paths[1], "moved"), (paths[2], "duplicate")]


def test_dry_run_shares_the_plan_across_files(make_config, archive, tmp_path, capsys):
    # 同じ内容の 2 件は、実際の実行と同じく 2 件目が重複になる（予定を実行全体で共有する）
    paths = _files(tmp_path, [b"A", b"A"])
    capsys.readouterr()
    assert main.run(make_config(source_path=paths[0], source_paths=paths, dry_run=True)) == main.EXIT_OK
    out = capsys.readouterr().out
    assert "[Summary] moved: 1, duplicates (left in place): 1" in out
    assert all(os.path.exists(p) for p in paths)


def test_dry_run_plans_distinct_destinations_for_same_names(make_config, archive, tmp_path, capsys, caplog):
    # 別のフォルダの同じ名前のファイル（内容は別）は、予定の保存先が重ならない
    a = write_file(str(tmp_path / "x" / "same.txt"), b"A")
    b = write_file(str(tmp_path / "y" / "same.txt"), b"B")
    assert main.run(make_config(source_path=a, source_paths=[a, b], dry_run=True)) == main.EXIT_OK
    rows = _csv_rows(caplog)
    planned = [r[4] for r in rows]
    assert [r[2] for r in rows] == ["moved", "moved"]
    assert len(set(p.casefold() for p in planned)) == 2, planned


def test_same_names_from_different_folders_both_move(make_config, archive, tmp_path):
    a = write_file(str(tmp_path / "x" / "same.txt"), b"A")
    b = write_file(str(tmp_path / "y" / "same.txt"), b"B")
    assert main.run(make_config(source_path=a, source_paths=[a, b])) == main.EXIT_OK
    assert not os.path.exists(a) and not os.path.exists(b)
    assert len(os.listdir(_month_dir(archive))) == 2


def test_folder_among_several_sources_is_one_failure(make_config, archive, tmp_path, source, caplog):
    # フォルダはまとめて取り込めない。そのファイルの失敗にして、ほかは取り込む（ファイルごとの事情。R6）
    paths = _files(tmp_path, [b"A"])
    assert main.run(make_config(source_path=source, source_paths=[source, paths[0]])) == main.EXIT_HAS_FAILURES
    assert not os.path.exists(paths[0])
    rows = _csv_rows(caplog)
    assert [(r[1], r[2]) for r in rows] == [(source, "failed"), (paths[0], "moved")]


def test_the_same_file_twice_is_one_failure(make_config, archive, tmp_path, caplog):
    paths = _files(tmp_path, [b"A"])
    assert main.run(make_config(source_path=paths[0], source_paths=[paths[0], paths[0]])) == main.EXIT_HAS_FAILURES
    rows = _csv_rows(caplog)
    assert [r[2] for r in rows] == ["moved", "failed"]
    assert "more than once" in rows[1][5]


def test_file_inside_an_archive_is_one_failure_and_the_rest_continue(make_config, archive, tmp_path, caplog):
    # 置き場所の断りもそのファイルの失敗（まとめた全部を断らない。確認は各ファイルの番に行う。R5）
    seed = write_file(str(tmp_path / "seed" / "s.txt"), b"S")
    assert main.run(make_config(source_path=seed)) == main.EXIT_OK  # 保存フォルダを登録しておく
    caplog.clear()
    paths = _files(tmp_path, [b"A"])
    inside = write_file(os.path.join(archive, "in.txt"), b"B")
    config = make_config(source_path=paths[0], source_paths=[inside, paths[0]])
    assert main.run(config) == main.EXIT_HAS_FAILURES
    assert os.path.exists(inside)  # 保存フォルダの中のものは動かさない
    assert not os.path.exists(paths[0])
    rows = _csv_rows(caplog)
    assert [(r[1], r[2]) for r in rows] == [(inside, "failed"), (paths[0], "moved")]
    assert "nested" in rows[0][5]


def test_file_missing_at_start_is_one_failure_not_a_rejection(make_config, archive, tmp_path, capsys, caplog):
    # まとめた項目が列で待つ間に 1 件消えても、残りは取り込む（1 件のために全部を断らない。R2）
    paths = _files(tmp_path, [b"A", b"B"])
    gone = str(tmp_path / "drop" / "gone.txt")
    capsys.readouterr()
    config = make_config(source_path=paths[0], source_paths=[paths[0], gone, paths[1]])
    assert main.run(config) == main.EXIT_HAS_FAILURES
    assert not os.path.exists(paths[0]) and not os.path.exists(paths[1])
    assert [(r[1], r[2]) for r in _csv_rows(caplog)] == [(paths[0], "moved"), (gone, "failed"), (paths[1], "moved")]


def test_file_gone_before_its_turn_is_a_failure_and_the_rest_continue(
    make_config, archive, tmp_path, capsys, monkeypatch, caplog):
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real_run_add = ingest.run_add

    def run_add_then_remove(session, config, record, *args, **kwargs):
        result = real_run_add(session, config, record, *args, **kwargs)
        if config.source_path == paths[0]:
            os.unlink(paths[1])  # 1 件目を取り込んでいる間に、2 件目が消えた
        return result

    monkeypatch.setattr(ingest, "run_add", run_add_then_remove)
    capsys.readouterr()
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_HAS_FAILURES
    out = capsys.readouterr().out
    assert "failed: 1" in out
    assert not os.path.exists(paths[2])  # 3 件目は取り込んだ
    rows = _csv_rows(caplog)
    assert [(r[1], r[2]) for r in rows] == [(paths[0], "moved"), (paths[1], "failed"), (paths[2], "moved")]


def test_interrupt_stops_the_batch_and_keeps_finished_files(
    make_config, archive, tmp_path, capsys, monkeypatch
):
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real_scan = crawler_client.scan_single_file

    def interrupted_on_second(path, st=None):
        if path == paths[1]:
            raise KeyboardInterrupt
        return real_scan(path, st)

    monkeypatch.setattr(crawler_client, "scan_single_file", interrupted_on_second)
    capsys.readouterr()
    main.run(make_config(source_path=paths[0], source_paths=paths))
    out = capsys.readouterr().out
    assert "run was interrupted" in out
    rows = _ingests(tmp_path)
    assert [r.status for r in rows] == ["completed", "interrupted"]  # 3 件目は始めない
    assert not os.path.exists(paths[0])
    assert os.path.exists(paths[1]) and os.path.exists(paths[2])
    assert out.count("[Summary]") == 1


def test_single_source_output_is_unchanged(make_config, archive, tmp_path, capsys):
    paths = _files(tmp_path, [b"A"])
    capsys.readouterr()
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_OK
    out = capsys.readouterr().out
    assert out.count("[Summary]") == 1
    assert "Total: 1 files" in out


# ---- R1 のレビュー

def test_dry_run_plans_folder_limit_branches_across_files(make_config, archive, tmp_path, caplog):
    # 保存先のフォルダの件数は実行全体で数える（実際の実行と同じく、上限を超えたら 001/ に入る）
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    config = make_config(source_path=paths[0], source_paths=paths, dry_run=True, folder_limit=2)
    assert main.run(config) == main.EXIT_OK
    planned = [r[4] for r in _csv_rows(caplog)]
    assert [p.count("/") for p in planned] == [1, 1, 2], planned  # 3 件目は <YYYY-MM>/001/


def test_interrupt_between_files_does_not_leave_a_running_record(
    make_config, archive, tmp_path, capsys, monkeypatch
):
    # 2 件目の記録を書き戻す途中（ループの外・実行の記録の後片付け）に中断が届いた
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real = main._apply_counters
    calls = []

    def interrupted(record, counters):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(record, counters)

    monkeypatch.setattr(main, "_apply_counters", interrupted)
    capsys.readouterr()
    main.run(make_config(source_path=paths[0], source_paths=paths))
    out = capsys.readouterr().out
    assert out.count("[Summary]") == 1
    assert "run was interrupted" in out
    assert "1 files were not started" in out  # 2 件目は始めていた
    # 2 件目の移動は DB に保存されているので、数え直してサマリに入れる（分からないとは言わない。R2）
    assert "moved: 2" in out
    assert "not available" not in out
    rows = _ingests(tmp_path)
    assert [r.status for r in rows] == ["completed", "interrupted"]
    assert rows[1].moved == 1


def test_file_gone_after_its_check_does_not_stop_the_batch(make_config, archive, tmp_path, capsys, monkeypatch):
    # 順番が来たときの確認の後、読む前に消えた。その 1 件だけの結果にし、残りは続ける（全体を致命的エラーにしない）
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real = main._run_record

    def gone_before_reading(session, config, archive_row, runner, *args, **kwargs):
        if config.source_path == paths[1]:
            os.unlink(paths[1])
        return real(session, config, archive_row, runner, *args, **kwargs)

    monkeypatch.setattr(main, "_run_record", gone_before_reading)
    capsys.readouterr()
    code = main.run(make_config(source_path=paths[0], source_paths=paths))
    out = capsys.readouterr().out
    assert code != main.EXIT_FATAL, out
    assert "fatal" not in out
    assert not os.path.exists(paths[2])  # 3 件目も取り込んだ


def test_each_record_snapshot_does_not_carry_the_whole_list(make_config, archive, tmp_path):
    import json

    paths = _files(tmp_path, [b"A", b"B", b"C"])
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_OK
    for row in _ingests(tmp_path):
        snapshot = json.loads(row.config_json)
        assert snapshot["source_path"] == row.source_root
        assert "source_paths" not in snapshot


def test_dry_run_interrupt_outside_the_loop_says_counts_are_incomplete(
    make_config, archive, tmp_path, capsys, monkeypatch
):
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real = ingest.commit_after_interrupt
    calls = []

    def interrupted(session):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt  # 2 件目の判定のループの後片付けの最中
        return real(session)

    monkeypatch.setattr(ingest, "commit_after_interrupt", interrupted)
    capsys.readouterr()
    main.run(make_config(source_path=paths[0], source_paths=paths, dry_run=True))
    out = capsys.readouterr().out
    assert "run was interrupted" in out
    assert "not available" in out  # 2 件目の件数は分からない（足し合わせた件数を全部と読ませない）


def test_interrupt_while_finishing_still_prints_the_summary(make_config, archive, tmp_path, capsys, monkeypatch):
    paths = _files(tmp_path, [b"A", b"B"])

    def interrupted(session, lookup):
        raise KeyboardInterrupt  # 最後の知らせ（つながっていない保存フォルダ）の最中

    monkeypatch.setattr(ingest, "_report_unavailable_owners", interrupted)
    capsys.readouterr()
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_OK
    out = capsys.readouterr().out
    assert out.count("[Summary]") == 1
    assert "run was interrupted" in out
    assert "moved: 2" in out


def test_refusal_while_importing_stops_the_batch_with_a_summary(make_config, archive, tmp_path, capsys, monkeypatch, caplog):
    # 取り込みの途中の断り（正本 DB・ほかの保存フォルダが読めないなど、環境の断り）はそこで止める（同じ失敗を残りの全部で
    # 繰り返さない）。取り込んだ分の件数と、始めていない件数は出す（R5）
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real = ingest.run_add

    def refuse_second(session, config, record, *args, **kwargs):
        if config.source_path == paths[1]:
            raise main.PreflightError("refused for the test")
        return real(session, config, record, *args, **kwargs)

    monkeypatch.setattr(ingest, "run_add", refuse_second)
    capsys.readouterr()
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_FATAL
    captured = capsys.readouterr()
    assert captured.out.count("[Summary]") == 1
    assert "moved: 1" in captured.out
    assert "1 files were not started" in captured.out
    assert "refused for the test" in captured.err
    assert not os.path.exists(paths[0]) and os.path.exists(paths[1]) and os.path.exists(paths[2])
    rows = _csv_rows(caplog)
    assert [(r[1], r[2]) for r in rows] == [(paths[0], "moved"), (paths[1], "failed")]
    assert [r.status for r in _ingests(tmp_path)] == ["completed"]  # 断ったファイルの記録は残さない（1 件のときと同じ）


def test_fatal_error_in_one_file_does_not_drop_the_rest(make_config, archive, tmp_path, capsys, monkeypatch, caplog):
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real = ingest.run_add

    def broken_second(session, config, record, *args, **kwargs):
        if config.source_path == paths[1]:
            raise RuntimeError("boom")
        return real(session, config, record, *args, **kwargs)

    monkeypatch.setattr(ingest, "run_add", broken_second)
    capsys.readouterr()
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_FATAL
    out = capsys.readouterr().out
    assert "fatal error" in out
    assert not os.path.exists(paths[2])  # 3 件目も取り込んだ
    assert [r.status for r in _ingests(tmp_path)] == ["completed", "failed", "completed"]
    assert [r[2] for r in _csv_rows(caplog)] == ["moved", "failed", "moved"]


def test_file_missing_at_start_is_checked_when_it_appears(make_config, archive, tmp_path, monkeypatch, caplog):
    # 事前の確認のときに無かったファイルが、順番までに保存フォルダの中に現れた。置き場所の確認を通さずに取り込まない
    paths = _files(tmp_path, [b"A"])
    later = os.path.join(archive, "later.txt")
    real = main._run_record

    def appears(session, config, archive_row, runner, *args, **kwargs):
        result = real(session, config, archive_row, runner, *args, **kwargs)
        write_file(later, b"B")  # 1 件目の後に現れる
        return result

    monkeypatch.setattr(main, "_run_record", appears)
    code = main.run(make_config(source_path=paths[0], source_paths=[paths[0], later]))
    assert code == main.EXIT_HAS_FAILURES
    assert os.path.exists(later)  # 保存フォルダの中のものは動かさない
    assert [r[2] for r in _csv_rows(caplog)] == ["moved", "failed"]


@pytest.mark.skipif(os.name == "nt", reason="シンボリックリンクの作成に権限が要る")
def test_dangling_symlink_is_one_failure_not_a_rejection(make_config, archive, tmp_path, caplog):
    # リンク先が消えたリンク（列で待つ間に消えた）も、消えたファイルと同じく 1 件の失敗（R3）
    paths = _files(tmp_path, [b"A"])
    link = str(tmp_path / "drop" / "link.txt")
    os.symlink(str(tmp_path / "gone.txt"), link)
    assert main.run(make_config(source_path=paths[0], source_paths=[link, paths[0]])) == main.EXIT_HAS_FAILURES
    assert not os.path.exists(paths[0])
    assert [(r[1], r[2]) for r in _csv_rows(caplog)] == [(link, "failed"), (paths[0], "moved")]


def test_fatal_error_before_the_result_row_still_leaves_a_row(make_config, archive, tmp_path, capsys, monkeypatch, caplog):
    # 判定の行を出す前（移動の後・判定の記録の書き込み）に止まっても、そのファイルの結果を残す（Codex R4）。移動は保存されて
    # いるので「移動した」と出す（失敗と出すと、もう一度落とされて「無い」になる。R7）
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real = ingest._record_item

    def broken(session, config, ingest_row, scanned, *args, **kwargs):
        if scanned.path_abs == paths[1]:
            raise RuntimeError("boom")
        return real(session, config, ingest_row, scanned, *args, **kwargs)

    monkeypatch.setattr(ingest, "_record_item", broken)
    capsys.readouterr()
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_FATAL
    rows = _csv_rows(caplog)
    assert [r[1] for r in rows] == paths
    assert rows[1][2] == "moved" and rows[1][4]  # 保存先も出す
    assert "fatal error" in rows[1][5]
    out = capsys.readouterr().out
    assert "moved: 3" in out
    assert [r.moved for r in _ingests(tmp_path)] == [1, 1, 1]  # 記録の件数もそろえる


def test_csv_write_error_outside_a_file_run_still_prints_the_summary(
    make_config, archive, tmp_path, capsys, monkeypatch
):
    # 消えていたファイルの行を書けない（CSV が開けない・ディスクがいっぱい）。落ちずに止まったことを出す（Claude R4）
    paths = _files(tmp_path, [b"A"])
    gone = str(tmp_path / "drop" / "gone.txt")
    real = ingest.csv_update

    def broken(path, row):
        if row[1] == gone:
            raise OSError(28, "No space left on device")
        return real(path, row)

    monkeypatch.setattr(ingest, "csv_update", broken)
    capsys.readouterr()
    assert main.run(make_config(source_path=gone, source_paths=[gone, paths[0]])) == main.EXIT_FATAL
    out = capsys.readouterr().out
    assert out.count("[Summary]") == 1
    assert "fatal error" in out


def test_interrupt_right_after_a_file_is_written_back_keeps_its_counts(
    make_config, archive, tmp_path, capsys, monkeypatch
):
    # 記録を書き戻した直後（_run_record が返る前）の中断。そのファイルの件数をサマリから落とさない（Claude R4）
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real = main._run_record

    def interrupted_after(session, config, archive_row, runner, *args, **kwargs):
        result = real(session, config, archive_row, runner, *args, **kwargs)
        if config.source_path == paths[1]:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(main, "_run_record", interrupted_after)
    capsys.readouterr()
    main.run(make_config(source_path=paths[0], source_paths=paths))
    out = capsys.readouterr().out
    assert "moved: 2" in out
    assert "not available" not in out
    assert [r.status for r in _ingests(tmp_path)] == ["completed", "completed"]


def test_missing_file_that_was_imported_earlier_says_so(make_config, archive, tmp_path, caplog):
    # 中断の直前に取り込み終えていたファイルが、列に戻されて番が来た（もう投入元に無い）。黙って外さず、取り込み済みと出す
    paths = _files(tmp_path, [b"A", b"B"])
    assert main.run(make_config(source_path=paths[0])) == main.EXIT_OK
    caplog.clear()
    main.run(make_config(source_path=paths[0], source_paths=paths))
    rows = _csv_rows(caplog)
    assert rows[0][1] == paths[0] and rows[0][2] == "failed"
    # 同じパスから前に取り込んだ別のファイルかもしれないので、言い切らない
    assert "was imported earlier" in rows[0][5]
    assert rows[1][2] == "moved"


def test_error_outside_a_file_run_does_not_leave_a_running_record(make_config, archive, tmp_path, capsys, monkeypatch):
    # 記録を書き戻す最中の例外（ファイルの処理の外）。止めるが、記録を running のまま残さない（R5）
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real = main._apply_counters
    calls = []

    def broken(record, counters):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("database is locked")
        return real(record, counters)

    monkeypatch.setattr(main, "_apply_counters", broken)
    capsys.readouterr()
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_FATAL
    out = capsys.readouterr().out
    assert "stopped due to a fatal error" in out
    assert "moved: 2" in out  # 2 件目の移動は保存されている（数え直す）
    assert [r.status for r in _ingests(tmp_path)] == ["completed", "failed"]


def test_error_while_finishing_still_prints_the_summary(make_config, archive, tmp_path, capsys, monkeypatch, caplog):
    paths = _files(tmp_path, [b"A", b"B"])

    def broken(session, lookup):
        raise RuntimeError("db gone")

    monkeypatch.setattr(ingest, "_report_unavailable_owners", broken)
    capsys.readouterr()
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_FATAL
    out = capsys.readouterr().out
    assert out.count("[Summary]") == 1
    assert "moved: 2" in out
    # ファイルごとの結果は CSV にしか無いので、後片付けが失敗しても CSV の場所は出す（UI のリンク。R21）
    assert [r[2] for r in _csv_rows(caplog)] == ["moved", "moved"]


def test_interrupt_after_a_refusal_does_not_touch_the_deleted_record(
    make_config, archive, tmp_path, capsys, monkeypatch
):
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real = ingest.run_add

    def refuse_second(session, config, record, *args, **kwargs):
        if config.source_path == paths[1]:
            raise main.PreflightError("refused for the test")
        return real(session, config, record, *args, **kwargs)

    def interrupted(batch, path, message):
        raise KeyboardInterrupt  # 断ったファイルの行を書いている最中

    monkeypatch.setattr(ingest, "run_add", refuse_second)
    monkeypatch.setattr(ingest, "record_failed", interrupted)
    capsys.readouterr()
    main.run(make_config(source_path=paths[0], source_paths=paths))
    out = capsys.readouterr().out
    assert out.count("[Summary]") == 1
    assert "moved: 1" in out


def test_error_after_the_result_row_keeps_the_saved_counts(make_config, archive, tmp_path, capsys, monkeypatch):
    # 結果の行を出した後（判定のループの後片付け）の例外。移動は保存されているので数え直す（R6）
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real = ingest.commit_after_interrupt
    calls = []

    def broken(session):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("disk error")
        return real(session)

    monkeypatch.setattr(ingest, "commit_after_interrupt", broken)
    capsys.readouterr()
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_FATAL
    out = capsys.readouterr().out
    assert "moved: 3" in out


@pytest.mark.parametrize("error", [KeyboardInterrupt])
def test_stop_during_a_files_checks_counts_it_as_not_started(make_config, archive, tmp_path, capsys, monkeypatch, error):
    # ファイルの番の確認（置き場所など）の最中に中断した。そのファイルは結果の行が無いので「始めていない」に入れる（R7）。
    # 確認の予期しない例外は、そのファイルの行にして続ける（R14。test_unexpected_error_while_checking_a_file_is_one_row）
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real = main._preflight_source

    def stopped(config, source, *args, **kwargs):
        if source == paths[1]:
            raise error
        return real(config, source, *args, **kwargs)

    monkeypatch.setattr(main, "_preflight_source", stopped)
    capsys.readouterr()
    main.run(make_config(source_path=paths[0], source_paths=paths))
    out = capsys.readouterr().out
    assert "2 files were not started" in out


def test_refusal_on_the_first_file_is_a_rejection(make_config, archive, tmp_path, monkeypatch):
    # 1 件も取り込まないうちの環境の断りは、1 件のときと同じく「断った」（R7）
    paths = _files(tmp_path, [b"A", b"B"])

    def refuse(session, config, record, *args, **kwargs):
        raise main.PreflightError("refused for the test")

    monkeypatch.setattr(ingest, "run_add", refuse)
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_REJECTED


def _case_sensitive(folder) -> bool:
    probe = os.path.join(folder, "CaseProbe")
    write_file(probe, b"x")
    try:
        return not os.path.exists(os.path.join(folder, "caseprobe"))
    finally:
        os.unlink(probe)


def test_names_differing_only_in_case_are_different_files_on_a_case_sensitive_volume(
    make_config, archive, tmp_path, monkeypatch
):
    # macOS の大文字小文字を区別するボリューム（APFS の区別あり）の別のファイルを、重複と断らない（R7）
    import utl.helpers

    folder = str(tmp_path / "drop")
    os.makedirs(folder)
    if not _case_sensitive(folder):
        pytest.skip("大文字小文字を区別しないボリューム")
    monkeypatch.setattr(utl.helpers, "_CASE_INSENSITIVE", True)  # macOS と同じ比較キー（大文字小文字をそろえる）
    a = write_file(os.path.join(folder, "Report.txt"), b"A")
    b = write_file(os.path.join(folder, "report.txt"), b"B")
    assert main.run(make_config(source_path=a, source_paths=[a, b])) == main.EXIT_OK
    assert not os.path.exists(a) and not os.path.exists(b)


def test_refusal_after_only_turned_away_files_is_a_rejection(make_config, archive, tmp_path, monkeypatch):
    # 取り込む前に断ったファイルしか無いうちの環境の断りも「断った」（1 件も取り込んでいない。R8）
    paths = _files(tmp_path, [b"A"])
    gone = str(tmp_path / "drop" / "gone.txt")

    def refuse(session, config, record, *args, **kwargs):
        raise main.PreflightError("refused for the test")

    monkeypatch.setattr(ingest, "run_add", refuse)
    assert main.run(make_config(source_path=gone, source_paths=[gone, paths[0]])) == main.EXIT_REJECTED


def test_dry_run_keeps_the_plan_after_a_fatal_error_in_one_file(make_config, archive, tmp_path, monkeypatch, caplog):
    # dry-run の予定は保存先の計画の中にしか無い。1 件の致命的エラーで作り直して、予定を忘れない（R8）
    paths = _files(tmp_path, [b"A", b"B", b"C", b"D"])
    real = ingest.run_add

    def broken_third(session, config, record, *args, **kwargs):
        if config.source_path == paths[2]:
            raise RuntimeError("boom")
        return real(session, config, record, *args, **kwargs)

    monkeypatch.setattr(ingest, "run_add", broken_third)
    main.run(make_config(source_path=paths[0], source_paths=paths, dry_run=True, folder_limit=2))
    planned = {r[1]: r[4] for r in _csv_rows(caplog) if r[2] == "moved"}
    assert planned[paths[3]].count("/") == 2, planned  # 4 件目は <YYYY-MM>/001/（a・b で上限）


def test_refusal_after_the_result_row_is_not_counted_as_not_started(make_config, archive, tmp_path, capsys, monkeypatch):
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real = ingest.run_add

    def refuse_after_row(session, config, record, *args, **kwargs):
        result = real(session, config, record, *args, **kwargs)
        if config.source_path == paths[1]:
            raise main.PreflightError("refused after the row")
        return result

    monkeypatch.setattr(ingest, "run_add", refuse_after_row)
    capsys.readouterr()
    main.run(make_config(source_path=paths[0], source_paths=paths))
    assert "1 files were not started" in capsys.readouterr().out


def test_failed_row_lines_come_in_the_same_order_as_in_the_loop(make_config, archive, tmp_path, capsys, monkeypatch):
    # 失敗の行（Failed）を出してから進み具合（Progress）。判定のループと同じ順（R8）
    monkeypatch.setenv(ingest.UI_PROGRESS_ENV, "1")
    paths = _files(tmp_path, [b"A"])
    gone = str(tmp_path / "drop" / "gone.txt")
    capsys.readouterr()
    main.run(make_config(source_path=gone, source_paths=[gone, paths[0]]))
    lines = capsys.readouterr().out.splitlines()
    failed_at = next(i for i, line in enumerate(lines) if line.startswith("Failed: "))
    assert lines[failed_at + 1].startswith("Progress: 1/2 files (failed)")


def test_error_outside_a_file_run_leaves_a_row_for_the_started_file(make_config, archive, tmp_path, capsys, monkeypatch, caplog):
    # 記録を作ってから処理の外の例外で止まったファイルにも結果の行を出す。始めていない件数は行の無いファイルの数で、UI の
    # 数え方（Progress の行）と同じ（R9）
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real = main._apply_counters
    calls = []

    def broken(record, counters):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("database is locked")
        return real(record, counters)

    monkeypatch.setattr(main, "_apply_counters", broken)
    capsys.readouterr()
    main.run(make_config(source_path=paths[0], source_paths=paths))
    out = capsys.readouterr().out
    rows = _csv_rows(caplog)
    assert [r[1] for r in rows] == paths[:2]  # 2 件目にも行がある（移動済みなので moved）
    assert rows[1][2] == "moved"
    progress = re.findall(r"Progress: (\d+)/3", out)
    assert f"{3 - len(progress)} files were not started" in out  # UI と同じ数え方


def test_same_file_given_twice_in_different_case_is_caught(make_config, archive, tmp_path, caplog):
    # 大文字小文字を区別しないボリューム（macOS の既定）で、同じファイルを大文字小文字違いで 2 度渡した（R9）
    paths = _files(tmp_path, [b"A"])
    other_case = os.path.join(os.path.dirname(paths[0]), os.path.basename(paths[0]).upper())
    if not os.path.exists(other_case):
        pytest.skip("大文字小文字を区別するボリューム")
    assert main.run(make_config(source_path=paths[0], source_paths=[paths[0], other_case])) == main.EXIT_HAS_FAILURES
    rows = _csv_rows(caplog)
    assert [r[2] for r in rows] == ["moved", "failed"]
    assert "more than once" in rows[1][5]


def test_file_failing_without_a_row_has_a_matching_record(make_config, archive, tmp_path, monkeypatch):
    # 判定の行を出す前に止まったファイル: 失敗の行と、記録の件数（failed 1）をそろえる（R9）
    paths = _files(tmp_path, [b"A", b"B"])
    real = ingest.run_add

    def broken_second(session, config, record, *args, **kwargs):
        if config.source_path == paths[1]:
            raise RuntimeError("boom")
        return real(session, config, record, *args, **kwargs)

    monkeypatch.setattr(ingest, "run_add", broken_second)
    main.run(make_config(source_path=paths[0], source_paths=paths))
    rows = _ingests(tmp_path)
    assert (rows[1].status, rows[1].failed, rows[1].total) == ("failed", 1, 1)


def test_hard_links_are_judged_as_duplicates_not_given_twice(make_config, archive, tmp_path, caplog):
    # 同じ実体の別のパス（ハードリンク）は、1 件ずつのときと同じく重複として判定する（R10）
    paths = _files(tmp_path, [b"A"])
    link = str(tmp_path / "other" / "x.txt")
    os.makedirs(os.path.dirname(link))
    os.link(paths[0], link)
    assert main.run(make_config(source_path=paths[0], source_paths=[paths[0], link])) == main.EXIT_OK
    assert [r[2] for r in _csv_rows(caplog)] == ["moved", "duplicate"]


def test_second_path_is_imported_when_the_first_was_turned_away(make_config, archive, tmp_path, monkeypatch, caplog):
    # 1 度目のパスが置き場所で断られたら、同じファイルの 2 度目のパスは取り込む（R10）
    paths = _files(tmp_path, [b"A"])
    link_dir = tmp_path / "via"
    os.symlink(os.path.dirname(paths[0]), str(link_dir))
    first = str(link_dir / os.path.basename(paths[0]))
    real = main._preflight_source

    def refuse_first(config, source, *args, **kwargs):
        if source == first:
            raise main.PreflightError("refused for the test")
        return real(config, source, *args, **kwargs)

    monkeypatch.setattr(main, "_preflight_source", refuse_first)
    main.run(make_config(source_path=first, source_paths=[first, paths[0]]))
    assert [r[2] for r in _csv_rows(caplog)] == ["failed", "moved"]


def test_turned_away_file_is_announced_as_the_current_file(make_config, archive, tmp_path, capsys, monkeypatch):
    # 取り込む前に断ったファイルも、処理中のファイルとして知らせてから結果を出す（UI が前のファイルを出し続けない。R10）
    monkeypatch.setenv(ingest.UI_PROGRESS_ENV, "1")
    paths = _files(tmp_path, [b"A"])
    gone = str(tmp_path / "drop" / "gone.txt")
    capsys.readouterr()
    main.run(make_config(source_path=paths[0], source_paths=[paths[0], gone]))
    lines = capsys.readouterr().out.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("Failed: "))
    assert lines[at - 1].startswith("Current file: ") and "gone.txt" in lines[at - 1]


def test_unreadable_file_is_not_called_imported_earlier(make_config, archive, tmp_path, monkeypatch, caplog):
    # 読めない（権限が無い）だけのファイルに「前に取り込んだ」とは言わない（無いときだけ。R11）
    paths = _files(tmp_path, [b"A", b"B"])
    assert main.run(make_config(source_path=paths[0])) == main.EXIT_OK
    write_file(paths[0], b"new file at the same path")
    caplog.clear()
    real_stat = os.stat

    def denied(path, *args, **kwargs):
        if path == paths[0]:
            raise PermissionError(13, "Permission denied", path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(main.os, "stat", denied)
    main.run(make_config(source_path=paths[0], source_paths=paths))
    row = _csv_rows(caplog)[0]
    assert row[2] == "failed" and "imported earlier" not in row[5] and "Permission denied" in row[5]


def test_dry_run_forgets_the_plan_of_a_file_that_failed(make_config, archive, tmp_path, monkeypatch, caplog):
    # dry-run で予期しない例外で終わったファイルの予定（内容）を残さない。後の同じ内容のファイルは移動の予定（R11）
    paths = _files(tmp_path, [b"A", b"A"])
    real = ingest._record_item

    def broken(session, config, ingest_row, scanned, *args, **kwargs):
        if scanned.path_abs == paths[0]:
            raise RuntimeError("boom")
        return real(session, config, ingest_row, scanned, *args, **kwargs)

    monkeypatch.setattr(ingest, "_record_item", broken)
    main.run(make_config(source_path=paths[0], source_paths=paths, dry_run=True))
    assert [r[2] for r in _csv_rows(caplog)] == ["failed", "moved"]


def test_interrupt_while_cleaning_up_after_an_error_still_prints_the_summary(
    make_config, archive, tmp_path, capsys, monkeypatch
):
    paths = _files(tmp_path, [b"A", b"B"])
    real = main._apply_counters
    calls = []

    def broken(record, counters):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("database is locked")
        return real(record, counters)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt  # 止まった後の片付けの最中に中断

    monkeypatch.setattr(main, "_apply_counters", broken)
    monkeypatch.setattr(main, "_close_cut_records", interrupted)
    capsys.readouterr()
    main.run(make_config(source_path=paths[0], source_paths=paths))
    assert capsys.readouterr().out.count("[Summary]") == 1


def test_error_while_closing_records_after_an_interrupt_still_prints_the_summary(
    make_config, archive, tmp_path, capsys, monkeypatch
):
    paths = _files(tmp_path, [b"A", b"B"])
    real = main._run_record

    def interrupted_after(session, config, archive_row, runner, *args, **kwargs):
        result = real(session, config, archive_row, runner, *args, **kwargs)
        raise KeyboardInterrupt

    def broken(*args, **kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(main, "_run_record", interrupted_after)
    monkeypatch.setattr(main, "_close_cut_records", broken)
    capsys.readouterr()
    main.run(make_config(source_path=paths[0], source_paths=paths))
    assert capsys.readouterr().out.count("[Summary]") == 1


def test_no_done_stage_when_a_file_ended_with_a_fatal_error(make_config, archive, tmp_path, capsys, monkeypatch):
    # 1 件のときと同じく、致命的エラーがあれば後片付け・完了の段階は出さない（R12）
    paths = _files(tmp_path, [b"A", b"B"])
    real = ingest.run_add

    def broken_second(session, config, record, *args, **kwargs):
        if config.source_path == paths[1]:
            raise RuntimeError("boom")
        return real(session, config, record, *args, **kwargs)

    monkeypatch.setattr(ingest, "run_add", broken_second)
    capsys.readouterr()
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_FATAL
    out = capsys.readouterr().out
    assert "Stage: done" not in out and "Stage: cleanup" not in out


def test_dry_run_keeps_the_plan_of_a_row_already_written(make_config, archive, tmp_path, monkeypatch, caplog):
    # 結果の行（予定の保存先）を書いた後に例外になったファイルの予定は戻さない（同じ保存先を 2 件に予定しない。R12）
    a = write_file(str(tmp_path / "x" / "same.txt"), b"A")
    b = write_file(str(tmp_path / "y" / "same.txt"), b"B")
    real = ingest.commit_after_interrupt
    calls = []

    def broken(session):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")  # 1 件目の判定のループの後片付け（行は書いた後）
        return real(session)

    monkeypatch.setattr(ingest, "commit_after_interrupt", broken)
    main.run(make_config(source_path=a, source_paths=[a, b], dry_run=True))
    planned = [r[4] for r in _csv_rows(caplog) if r[2] == "moved"]
    assert len(planned) == 2 and len({p.casefold() for p in planned}) == 2, planned


def test_file_replaced_by_a_folder_after_its_check_is_not_crawled(make_config, archive, tmp_path, monkeypatch, caplog):
    # 番の確認（ファイル）の後にフォルダに替わっても、まとめた実行の中でフォルダとして走査しない（R13）
    paths = _files(tmp_path, [b"A", b"B"])
    real = main._preflight_source

    def swap(config, source, *args, **kwargs):
        real(config, source, *args, **kwargs)
        if source == paths[0]:
            os.unlink(paths[0])
            os.makedirs(paths[0])
            write_file(os.path.join(paths[0], "inner.txt"), b"I")

    monkeypatch.setattr(main, "_preflight_source", swap)
    main.run(make_config(source_path=paths[0], source_paths=paths))
    assert os.path.exists(os.path.join(paths[0], "inner.txt"))  # 中のファイルは動かさない
    assert [r[1] for r in _csv_rows(caplog)] == paths


def test_second_path_is_imported_when_the_first_failed_with_an_error(make_config, archive, tmp_path, monkeypatch, caplog):
    # 1 度目が予期しない例外で取り込めなかったら、同じファイルの 2 度目のパスは取り込む（R13）
    paths = _files(tmp_path, [b"A"])
    link_dir = tmp_path / "via"
    os.symlink(os.path.dirname(paths[0]), str(link_dir))
    first = str(link_dir / os.path.basename(paths[0]))
    real = ingest.run_add
    calls = []

    def broken_first(session, config, record, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(session, config, record, *args, **kwargs)

    monkeypatch.setattr(ingest, "run_add", broken_first)
    main.run(make_config(source_path=first, source_paths=[first, paths[0]]))
    assert [r[2] for r in _csv_rows(caplog)] == ["failed", "moved"]


def test_error_while_finishing_after_an_interrupt_is_still_an_interrupt(make_config, archive, tmp_path, capsys, monkeypatch):
    # 中断の後の後片付けの例外で、終わり方を致命的エラーにしない（UI は中断として残りを列に戻す。R13）
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real_scan = crawler_client.scan_single_file

    def interrupted_on_second(path, st=None):
        if path == paths[1]:
            raise KeyboardInterrupt
        return real_scan(path, st)

    def broken(session, lookup):
        raise RuntimeError("session broken")

    monkeypatch.setattr(crawler_client, "scan_single_file", interrupted_on_second)
    monkeypatch.setattr(ingest, "_report_unavailable_owners", broken)
    capsys.readouterr()
    code = main.run(make_config(source_path=paths[0], source_paths=paths))
    assert code != main.EXIT_FATAL
    assert "run was interrupted" in capsys.readouterr().out


def test_interrupt_after_a_files_fatal_error_is_not_a_fatal_exit(make_config, archive, tmp_path, monkeypatch):
    # 前のファイルの致命的エラーがあっても、中断で終わったら終了コードは致命的エラーにしない（UI が残りを列に戻す。R14）
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real = ingest.run_add
    real_scan = crawler_client.scan_single_file

    def broken_first(session, config, record, *args, **kwargs):
        if config.source_path == paths[0]:
            raise RuntimeError("boom")
        return real(session, config, record, *args, **kwargs)

    def interrupted_on_second(path, st=None):
        if path == paths[1]:
            raise KeyboardInterrupt
        return real_scan(path, st)

    monkeypatch.setattr(ingest, "run_add", broken_first)
    monkeypatch.setattr(crawler_client, "scan_single_file", interrupted_on_second)
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_HAS_FAILURES


def test_all_files_missing_is_rejected_before_registering_the_archive(make_config, archive, tmp_path):
    # 取り込めるファイルが 1 つも無ければ、1 件のときと同じく事前に断る（保存フォルダに登録の跡を残さない。R14）
    a, b = str(tmp_path / "a.txt"), str(tmp_path / "b.txt")
    with pytest.raises(main.PreflightError, match="Source not found"):
        main.run(make_config(source_path=a, source_paths=[a, b]))
    assert not os.path.exists(os.path.join(archive, ".akasyx"))


def test_same_file_twice_is_judged_again_when_the_first_stayed(make_config, archive, tmp_path, caplog):
    # 1 度目が重複で元の場所に残ったなら、2 度目もふつうに判定する（失敗にしない。R14）
    seed = write_file(str(tmp_path / "seed" / "s.txt"), b"A")
    assert main.run(make_config(source_path=seed)) == main.EXIT_OK
    caplog.clear()
    paths = _files(tmp_path, [b"A"])
    link_dir = tmp_path / "via"
    os.symlink(os.path.dirname(paths[0]), str(link_dir))
    again = str(link_dir / os.path.basename(paths[0]))
    assert main.run(make_config(source_path=paths[0], source_paths=[paths[0], again])) == main.EXIT_OK
    assert [r[2] for r in _csv_rows(caplog)] == ["duplicate", "duplicate"]


def test_unexpected_error_while_checking_a_file_is_one_row(make_config, archive, tmp_path, monkeypatch, caplog):
    # 番の確認（置き場所）の予期しない例外も、そのファイルの行にして続ける（R14）
    paths = _files(tmp_path, [b"A", b"B"])
    real = main._preflight_source

    def broken(config, source, *args, **kwargs):
        if source == paths[0]:
            raise OSError(5, "Input/output error")
        return real(config, source, *args, **kwargs)

    monkeypatch.setattr(main, "_preflight_source", broken)
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_HAS_FAILURES
    rows = _csv_rows(caplog)
    assert [r[2] for r in rows] == ["failed", "moved"]


def test_interrupt_after_a_fatal_error_past_its_row_is_still_a_failure_exit(make_config, archive, tmp_path, monkeypatch):
    # 行を出した後に致命的エラーになったファイルがあり、その後に中断した。終了コードは 2（R15）
    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real_commit = ingest.commit_after_interrupt
    real_scan = crawler_client.scan_single_file
    calls = []

    def broken(session):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk error")  # 1 件目の判定のループの後片付け（行は書いた後）
        return real_commit(session)

    def interrupted_on_second(path, st=None):
        if path == paths[1]:
            raise KeyboardInterrupt
        return real_scan(path, st)

    monkeypatch.setattr(ingest, "commit_after_interrupt", broken)
    monkeypatch.setattr(crawler_client, "scan_single_file", interrupted_on_second)
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_HAS_FAILURES


def test_dry_run_predicts_the_same_result_for_a_file_given_twice(make_config, archive, tmp_path, caplog):
    # 同じファイルを 2 度渡したとき、dry-run も実際の実行と同じ結果を予定する（R15）
    paths = _files(tmp_path, [b"A"])
    link_dir = tmp_path / "via"
    os.symlink(os.path.dirname(paths[0]), str(link_dir))
    again = str(link_dir / os.path.basename(paths[0]))
    main.run(make_config(source_path=paths[0], source_paths=[paths[0], again], dry_run=True))
    planned = [r[2] for r in _csv_rows(caplog)]
    caplog.clear()
    main.run(make_config(source_path=paths[0], source_paths=[paths[0], again]))
    assert planned == [r[2] for r in _csv_rows(caplog)] == ["moved", "failed"]


def test_db_error_while_checking_a_file_does_not_stop_the_rest(make_config, archive, tmp_path, monkeypatch, caplog):
    # 番の確認の DB の例外でセッションが壊れても、そのファイルの行にして次のファイルを取り込む（R16）
    paths = _files(tmp_path, [b"A", b"B"])
    real = main._preflight_source

    def broken(config, source, *args, **kwargs):
        if source == paths[0]:
            session = main._current_session_for_test
            session.add(IngestItem())  # 必須の列が無い行 → flush が失敗し、セッションは rollback 待ちになる
            session.flush()
        return real(config, source, *args, **kwargs)

    real_run_files = main._run_files

    def capture(session, *args, **kwargs):
        main._current_session_for_test = session
        return real_run_files(session, *args, **kwargs)

    monkeypatch.setattr(main, "_run_files", capture)
    monkeypatch.setattr(main, "_preflight_source", broken)
    main.run(make_config(source_path=paths[0], source_paths=paths))
    assert [r[2] for r in _csv_rows(caplog)] == ["failed", "moved"]


def test_refusal_summary_does_not_say_fatal_error(make_config, archive, tmp_path, capsys, monkeypatch):
    paths = _files(tmp_path, [b"A", b"B"])

    def refuse(session, config, record, *args, **kwargs):
        raise main.PreflightError("refused for the test")

    monkeypatch.setattr(ingest, "run_add", refuse)
    capsys.readouterr()
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_REJECTED
    out = capsys.readouterr().out
    assert "fatal error" not in out and "refused" in out


def test_only_folders_given_is_rejected_before_registering_the_archive(make_config, archive, tmp_path, source):
    # 取り込めるファイル（保存フォルダの外のふつうのファイル）が 1 つも無ければ事前に断る（R17）
    other = str(tmp_path / "other")
    os.makedirs(other)
    with pytest.raises(main.PreflightError, match="none of the given"):
        main.run(make_config(source_path=source, source_paths=[source, other]))
    assert not os.path.exists(os.path.join(archive, ".akasyx"))


def test_files_all_inside_another_archive_are_rejected_before_registering(make_config, archive, tmp_path):
    # 全部がほかの保存フォルダの中なら、1 件のときと同じく事前に断る（R18）
    other = tmp_path / "other_archive"
    write_file(str(other / ".akasyx" / "archive.id"), b"other-uid")
    a = write_file(str(other / "a.txt"), b"A")
    b = write_file(str(other / "b.txt"), b"B")
    with pytest.raises(main.PreflightError, match="No file to import"):
        main.run(make_config(source_path=a, source_paths=[a, b]))
    assert not os.path.exists(os.path.join(archive, ".akasyx"))


def test_failed_move_does_not_count_toward_the_folder_limit(make_config, archive, tmp_path, monkeypatch):
    # 移動に失敗したファイルを保存先のフォルダの件数に数えない（1 件ずつのときと同じく、実際の件数で枝分かれする。R19）
    import mover

    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real = mover.safe_move

    def broken(src, dst, *args, **kwargs):
        if src == paths[1]:
            raise OSError(28, "No space left on device")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(mover, "safe_move", broken)
    main.run(make_config(source_path=paths[0], source_paths=paths, folder_limit=2))
    month = _month_dir(archive)
    assert sorted(n for n in os.listdir(month) if not n.startswith(".")) == ["f0.txt", "f2.txt"]  # 001/ に入らない


def test_error_outside_a_file_run_recovers_the_pending_move_first(make_config, archive, tmp_path, capsys, monkeypatch, caplog):
    # 移動の途中（pending）を残したファイルが処理の外の例外で止まっても、ファイルごとの例外のときと同じく片付けてから結果を
    # 決める（移動を終えていたなら「移動した」。例外の出た場所で結果を変えない。R20）
    import mover

    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real_move = mover.safe_move
    real_apply = main._apply_counters

    def moved_then_broken(src, dst, *args, **kwargs):
        real_move(src, dst, *args, **kwargs)
        if src == paths[1]:
            raise RuntimeError("boom after the move")  # 移動は済んだが確定の前（pending のまま）

    calls = []

    def broken(record, counters):
        if record.source_root == paths[1] and not calls:
            calls.append(1)
            raise RuntimeError("database is locked")  # 記録の書き戻し（処理の外）でも止まる
        return real_apply(record, counters)

    monkeypatch.setattr(mover, "safe_move", moved_then_broken)
    monkeypatch.setattr(main, "_apply_counters", broken)
    main.run(make_config(source_path=paths[0], source_paths=paths))
    rows = _csv_rows(caplog)
    assert [r[1] for r in rows] == paths[:2]
    assert rows[1][2] == "moved", rows[1]  # 片付けで保存済みになった（「確定していない」の失敗にしない）


def test_file_replaced_by_a_folder_during_its_check_is_not_checked_as_a_folder(make_config, archive, tmp_path, monkeypatch, caplog):
    # 番の確認の stat（ファイル）の後、置き場所の確認の最中にフォルダに替わっても、フォルダの検査（中の走査・crawler の
    # 確認）に入らない（種類は番の stat で決める。R22）
    import archives

    paths = _files(tmp_path, [b"A", b"B"])
    real = archives.enclosing_archive
    calls = []

    def swap(source, *args, **kwargs):
        if source == paths[0]:
            calls.append(1)
            if len(calls) == 2:  # 1 回目は事前の確認、2 回目が番の確認
                os.unlink(paths[0])
                os.makedirs(paths[0])
                write_file(os.path.join(paths[0], "inner.txt"), b"I")
        return real(source, *args, **kwargs)

    def crawler_checked(config):
        raise AssertionError("checked the crawler for a file")

    monkeypatch.setattr(archives, "enclosing_archive", swap)
    monkeypatch.setattr(crawler_client, "check_crawler", crawler_checked)
    main.run(make_config(source_path=paths[0], source_paths=paths))
    assert len(calls) == 2
    assert os.path.exists(os.path.join(paths[0], "inner.txt"))
    assert "checked the crawler" not in caplog.text


def test_refusal_while_importing_does_not_save_the_files_partial_writes(make_config, archive, tmp_path, monkeypatch):
    # 取り込みの途中で断られたら、そのファイルの途中の書き込みを捨ててから記録を消す（記録の削除と一緒に確定させない・
    # DB の例外で断りを置き換えない。R22）
    from models import IngestItem as Item

    paths = _files(tmp_path, [b"A", b"B"])

    def refuse(session, config, record, *args, **kwargs):
        session.add(Item(ingest_id=record.id, source_path_abs=config.source_path, result="moved"))
        session.flush()
        raise main.PreflightError("refused for the test")

    monkeypatch.setattr(ingest, "run_add", refuse)
    assert main.run(make_config(source_path=paths[0], source_paths=paths)) == main.EXIT_REJECTED
    assert _items(tmp_path) == []
    assert _ingests(tmp_path) == []


def test_file_left_unregistered_by_the_recovery_says_where_it_is(make_config, archive, tmp_path, monkeypatch, caplog):
    # 例外で止まったファイルの移動の途中を片付けたら、同じ内容がほかの保存フォルダにあって未登録になった。元の場所からは
    # 消えているので、行に保存フォルダの中の場所を書く（黙って「失敗」だけにしない。R23）
    import archives
    import mover
    from models import STATUS_PENDING

    paths = _files(tmp_path, [b"A", b"B", b"C"])
    real_move = mover.safe_move
    real_promote = archives.promote_many

    def moved_then_broken(src, dst, *args, **kwargs):
        real_move(src, dst, *args, **kwargs)
        if src == paths[1]:
            raise RuntimeError("boom after the move")

    def refused_in_recovery(session, rows):
        if any(r.status == STATUS_PENDING for r in rows):
            return []  # ほかの保存フォルダに同じ内容が保存された
        return real_promote(session, rows)

    monkeypatch.setattr(mover, "safe_move", moved_then_broken)
    monkeypatch.setattr(archives, "promote_many", refused_in_recovery)
    main.run(make_config(source_path=paths[0], source_paths=paths))
    rows = _csv_rows(caplog)
    assert [r[2] for r in rows] == ["moved", "failed", "moved"]
    assert not os.path.exists(paths[1])
    assert "unregistered" in rows[1][5] and "f1.txt" in rows[1][5], rows[1]
