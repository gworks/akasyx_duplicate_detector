# test_interrupt_outside_loop.py - 処理のループの外での中断も interrupted として記録する（#34）
#
# 中断は取り込み・重複の処理のループの中で受け止めていた。一覧作成（crawler）の段階や、ループの後の後片付けの
# commit の最中に届くと、KeyboardInterrupt のまま抜けて Ingest が failed（終了コード 1）になり、中断として扱う UI と
# 記録が食い違っていた。
import os

import crawler_client
import ingest
import main
from conftest import archive_db_path, write_file
from database import get_session
from models import MODE_VERIFY, Ingest


def _last_ingest(tmp_path) -> Ingest:
    session, engine = get_session(archive_db_path(tmp_path))
    try:
        row = session.query(Ingest).order_by(Ingest.id.desc()).first()
        session.expunge(row)
        return row
    finally:
        session.close()
        engine.dispose()


def test_interrupt_while_listing_the_source_is_recorded_as_interrupted(
    make_config, archive, source, tmp_path, monkeypatch, capsys
):
    path = write_file(os.path.join(source, "a.txt"), b"AAA")
    monkeypatch.setattr(crawler_client, "resolve_crawler_repo", lambda p: p)

    def interrupted_crawler(target, config, extra_excludes=()):
        raise KeyboardInterrupt  # crawler を待っている間の中断

    monkeypatch.setattr(crawler_client, "run_crawler", interrupted_crawler)
    capsys.readouterr()
    assert main.run(make_config(archive_root=archive, source_path=source)) == main.EXIT_OK
    row = _last_ingest(tmp_path)
    assert row.status == "interrupted"
    assert row.finished_at is not None
    assert row.moved == 0
    assert os.path.exists(path)  # 何も動いていない
    out = capsys.readouterr().out
    assert "run was interrupted" in out
    assert "no files were moved" in out  # 移動がまだ始まっていないことがサマリで分かる


def test_interrupt_during_the_last_commit_keeps_the_saved_counts(
    make_config, archive, source, tmp_path, fake_crawler, monkeypatch, capsys
):
    write_file(os.path.join(source, "a.txt"), b"AAA")
    write_file(os.path.join(source, "b.txt"), b"BBB")
    fake_crawler(source, str(tmp_path / "crawler.db"))
    real = ingest.commit_after_interrupt
    calls = []

    def interrupted_commit(session):
        calls.append(1)
        real(session)  # 移動の記録は確定している
        raise KeyboardInterrupt  # ループの後の後片付けの commit の最中の中断

    monkeypatch.setattr(ingest, "commit_after_interrupt", interrupted_commit)
    capsys.readouterr()
    assert main.run(make_config(archive_root=archive, source_path=source)) == main.EXIT_OK
    assert calls == [1]
    row = _last_ingest(tmp_path)
    assert row.status == "interrupted"
    assert row.moved == 2  # 件数は DB に保存された記録から数え直す（0 件と記録しない）
    out = capsys.readouterr().out
    assert "run was interrupted" in out
    assert "no files were moved" not in out


def test_interrupt_after_the_loop_is_recorded_as_interrupted(
    make_config, archive, source, tmp_path, fake_crawler, monkeypatch
):
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "crawler.db"))

    def interrupted_report(session, lookup):
        raise KeyboardInterrupt

    monkeypatch.setattr(ingest, "_report_unavailable_owners", interrupted_report)
    assert main.run(make_config(archive_root=archive, source_path=source)) == main.EXIT_OK
    row = _last_ingest(tmp_path)
    assert row.status == "interrupted"
    assert row.moved == 1


def test_interrupt_does_not_commit_unfinished_work_outside_the_loop(
    make_config, archive, source, tmp_path, fake_crawler, monkeypatch
):
    # ループの外の中断は、確定していない書き込みを捨てる（失敗と同じ。途中までの状態を確定させない）
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "crawler.db"))

    def half_done(session, lookup):
        from models import Archive

        session.get(Archive, 1).root_abs = "/half/done"  # 確定していない変更
        raise KeyboardInterrupt

    monkeypatch.setattr(ingest, "_report_unavailable_owners", half_done)
    main.run(make_config(archive_root=archive, source_path=source))
    session, engine = get_session(archive_db_path(tmp_path))
    try:
        from models import Archive

        assert all(a.root_abs != "/half/done" for a in session.query(Archive).all())
    finally:
        session.close()
        engine.dispose()


def test_verify_interrupted_while_scanning_is_recorded_as_interrupted(
    make_config, archive, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(crawler_client, "resolve_crawler_repo", lambda p: p)

    def interrupted_crawler(target, config, extra_excludes=()):
        raise KeyboardInterrupt

    monkeypatch.setattr(crawler_client, "run_crawler", interrupted_crawler)
    capsys.readouterr()
    assert main.run(make_config(mode=MODE_VERIFY, archive_root=archive)) == main.EXIT_OK
    assert _last_ingest(tmp_path).status == "interrupted"
    assert "counts are not available" in capsys.readouterr().out  # verify は ok の件数を DB に残さない


def test_main_still_exits_with_interrupted_status_not_fatal(
    make_config, archive, source, tmp_path, monkeypatch
):
    # main() まで KeyboardInterrupt が上がらない（致命的エラーとして終わらない）
    write_file(os.path.join(source, "a.txt"), b"AAA")
    monkeypatch.setattr(crawler_client, "resolve_crawler_repo", lambda p: p)
    monkeypatch.setattr(
        crawler_client, "run_crawler",
        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    cfg = make_config(archive_root=archive, source_path=source)
    monkeypatch.setattr(main.config_module, "parse_arguments", lambda argv: cfg)
    assert main.main([]) == main.EXIT_OK


def test_dry_run_interrupted_after_the_loop_does_not_claim_zero_counts(
    make_config, archive, source, tmp_path, fake_crawler, monkeypatch, capsys
):
    # dry-run は判定を DB に書かないので、数え直せない。0 件と読める形で出さない
    write_file(os.path.join(source, "a.txt"), b"AAA")
    fake_crawler(source, str(tmp_path / "crawler.db"))

    def interrupted_report(session, lookup):
        raise KeyboardInterrupt

    monkeypatch.setattr(ingest, "_report_unavailable_owners", interrupted_report)
    capsys.readouterr()
    assert main.run(make_config(archive_root=archive, source_path=source, dry_run=True)) == main.EXIT_OK
    assert _last_ingest(tmp_path).status == "interrupted"
    out = capsys.readouterr().out
    assert "counts are not available" in out
    assert "moved: 0" not in out


def test_delete_duplicates_interrupted_after_the_loop_does_not_claim_zero_counts(
    make_config, archive, tmp_path, monkeypatch, capsys
):
    # delete-duplicates は前の取り込みの行を書き換えるので、この実行の記録からは数え直せない
    import dedupe
    from models import MODE_DELETE_DUPLICATES

    def interrupted_items(session, config):
        raise KeyboardInterrupt

    monkeypatch.setattr(dedupe, "_pending_items", interrupted_items)
    capsys.readouterr()
    assert main.run(make_config(mode=MODE_DELETE_DUPLICATES, archive_root=archive)) == main.EXIT_OK
    assert _last_ingest(tmp_path).status == "interrupted"
    out = capsys.readouterr().out
    assert "counts are not available" in out
    assert "nothing to process" not in out


def test_interrupt_inside_the_last_commit_still_counts_the_moved_files(
    make_config, archive, source, tmp_path, fake_crawler, monkeypatch, caplog
):
    # 最後の commit の最中の中断では、判定の記録（ar_ingest_items）が確定しないまま捨てられる。移動したファイルの保存の記録は
    # 1 件ごとに確定しているので、移動の件数はそこから数える。捨てた分があることを黙らない
    write_file(os.path.join(source, "a.txt"), b"AAA")
    write_file(os.path.join(source, "b.txt"), b"BBB")
    fake_crawler(source, str(tmp_path / "crawler.db"))

    def interrupted_inside_commit(session):
        raise KeyboardInterrupt  # commit が確定する前の中断

    monkeypatch.setattr(ingest, "commit_after_interrupt", interrupted_inside_commit)
    assert main.run(make_config(archive_root=archive, source_path=source)) == main.EXIT_OK
    row = _last_ingest(tmp_path)
    assert row.status == "interrupted"
    assert row.moved == 2
    assert "were discarded" in caplog.text


def test_interrupted_report_says_the_output_may_be_incomplete(make_config, archive, monkeypatch, capsys):
    import report
    from models import MODE_REPORT

    def interrupted_report(session, config, ingest):
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "RUNNERS", {**main.RUNNERS, MODE_REPORT: interrupted_report})
    capsys.readouterr()
    assert main.run(make_config(mode=MODE_REPORT, archive_root=archive)) == main.EXIT_OK
    assert "report was interrupted" in capsys.readouterr().out


def test_no_discard_warning_while_listing(make_config, archive, source, tmp_path, monkeypatch, caplog):
    # 一覧作成の段階では判定はまだ無い。捨てた・CSV を見よとは言わない
    write_file(os.path.join(source, "a.txt"), b"AAA")
    monkeypatch.setattr(crawler_client, "resolve_crawler_repo", lambda p: p)

    def interrupted_crawler(target, config, extra_excludes=()):
        raise KeyboardInterrupt

    monkeypatch.setattr(crawler_client, "run_crawler", interrupted_crawler)
    main.run(make_config(archive_root=archive, source_path=source))
    assert "discarded" not in caplog.text


def test_report_does_not_show_zero_counts_for_unavailable_counts(
    make_config, archive, tmp_path, monkeypatch, capsys
):
    # 件数が分からない実行は、記録にも目印を残し、report の履歴でも 0 件と出さない
    import dedupe
    import json
    from models import COUNTS_UNAVAILABLE, MODE_DELETE_DUPLICATES, MODE_REPORT

    def interrupted_items(session, config):
        raise KeyboardInterrupt

    monkeypatch.setattr(dedupe, "_pending_items", interrupted_items)
    main.run(make_config(mode=MODE_DELETE_DUPLICATES, archive_root=archive))
    row = _last_ingest(tmp_path)
    assert json.loads(row.stats_json) == {COUNTS_UNAVAILABLE: True}
    monkeypatch.undo()
    capsys.readouterr()
    main.run(make_config(mode=MODE_REPORT, archive_root=archive))  # 最近の実行の一覧
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.strip().startswith(f"#{row.id} "))
    assert "counts not available" in line
    main.run(make_config(mode=MODE_REPORT, archive_root=archive, ingest_id=row.id))  # 実行の詳細
    assert "not available (interrupted)" in capsys.readouterr().out
