# test_stop_watch.py - 停止ファイルでの中断（#32。Windows の UI からの中断）
import subprocess
import sys
import threading
import time

import crawler_client
import main
import pytest
import stop_watch


def _wait_until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_watch_interrupts_once_and_removes_the_stop_file(tmp_path):
    stop = tmp_path / "stop"
    calls = []
    watch = stop_watch.start(str(stop), interval=0.01, interrupt=lambda: calls.append(1))
    assert watch is not None and watch.thread.daemon
    time.sleep(0.05)
    assert calls == []  # ファイルが無い間は何もしない
    stop.write_text("")
    assert _wait_until(lambda: calls == [1])
    time.sleep(0.05)
    assert calls == [1]  # 1 度だけ（2 度目の中断は後片付けを途中で止めうる）
    assert _wait_until(lambda: not watch.thread.is_alive())
    assert not stop.exists()  # UI が先に終わっても停止ファイルを残さない


def test_stopped_watch_does_not_interrupt(tmp_path):
    stop = tmp_path / "stop"
    calls = []
    watch = stop_watch.start(str(stop), interval=0.01, interrupt=lambda: calls.append(1))
    watch.stop()
    assert _wait_until(lambda: not watch.thread.is_alive())
    stop.write_text("")
    time.sleep(0.05)
    assert calls == []  # 実行が終わった後に中断を起こさない


def test_watch_is_not_started_without_a_stop_file_path():
    assert stop_watch.start(None, interrupt=lambda: None) is None
    assert stop_watch.start("", interrupt=lambda: None) is None


def test_start_from_env_reads_the_path(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setenv(stop_watch.STOP_FILE_ENV, str(tmp_path / "s"))
    monkeypatch.setattr(stop_watch, "start", lambda path, **kw: seen.append(path))
    stop_watch.start_from_env()
    assert seen == [str(tmp_path / "s")]


class _Watch:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_main_watches_only_while_running_and_handles_the_interrupt(monkeypatch, tmp_path):
    watch = _Watch()
    events = []
    monkeypatch.setattr(stop_watch, "start_from_env", lambda: events.append("start") or watch)

    def interrupted_run(config):
        events.append("run")
        raise KeyboardInterrupt  # 見張りが起こした中断

    monkeypatch.setattr(main, "run", interrupted_run)
    assert main.main(["report", str(tmp_path / "archive")]) == main.EXIT_FATAL  # 中断として後片付けして終わる
    assert events == ["start", "run"]  # 見張りは run の直前に立てる（その外で中断を起こさない）
    assert watch.stopped  # 終わったら見張りをやめる


class _FakeProc:
    """wait が KeyboardInterrupt で抜けられるかを見るための、終わらない子プロセス。"""

    pid = 4242

    def __init__(self, interrupt_at=3):
        self.waits = 0
        self.killed = False
        self.interrupt_at = interrupt_at
        self.timeouts = []

    def wait(self, timeout=None):
        self.waits += 1
        self.timeouts.append(timeout)
        if self.killed:
            return -9
        if self.waits == self.interrupt_at:
            raise KeyboardInterrupt  # 見張りのスレッドが中断を起こした
        raise subprocess.TimeoutExpired("crawler", timeout)

    def poll(self):
        return -9 if self.killed else None

    def kill(self):
        self.killed = True


def test_crawler_wait_is_interruptible_and_stops_the_whole_tree(monkeypatch):
    proc = _FakeProc()
    stopped = []
    monkeypatch.setattr(crawler_client, "stop_process_tree", lambda p: stopped.append(p.pid))
    with pytest.raises(KeyboardInterrupt):
        crawler_client.wait_crawler(proc)
    assert proc.waits >= 3  # 短い間隔で待つ（Windows では長い wait が中断で抜けない）
    assert stopped == [4242]  # crawler（uv の下の python まで）を止めてから中断を伝える


def test_crawler_gets_a_grace_period_before_it_is_stopped(monkeypatch):
    # crawler も中断を受けていれば（SIGINT・コンソールの Ctrl+C）自分で止まる。すぐ kill せず、少し待ってから止める
    proc = _FakeProc()
    stopped = []
    monkeypatch.setattr(crawler_client, "stop_process_tree", lambda p: stopped.append(p.pid))
    with pytest.raises(KeyboardInterrupt):
        crawler_client.wait_crawler(proc)
    assert proc.timeouts[-1] == crawler_client.CRAWLER_STOP_GRACE
    assert stopped == [4242]


def test_stop_process_tree_uses_taskkill_on_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(crawler_client, "_is_windows", lambda: True)
    monkeypatch.setattr(crawler_client.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    proc = _FakeProc()
    crawler_client.stop_process_tree(proc)
    assert calls and calls[0][:4] == ["taskkill", "/pid", "4242", "/T"]


def test_stop_process_tree_kills_the_child_elsewhere(monkeypatch):
    monkeypatch.setattr(crawler_client, "_is_windows", lambda: False)
    proc = _FakeProc()
    crawler_client.stop_process_tree(proc)
    assert proc.killed


@pytest.mark.skipif(sys.platform != "win32", reason="Windows の中断の実物の確かめ")
def test_interrupt_main_reaches_a_waiting_main_thread(tmp_path):
    # 本物の子プロセスを待っている間に停止ファイルを作ると、KeyboardInterrupt で抜ける
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    stop = tmp_path / "stop"
    watch = stop_watch.start(str(stop), interval=0.05)
    timer = threading.Timer(0.3, lambda: stop.write_text(""))
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            crawler_client.wait_crawler(proc)
        assert time.monotonic() - started < 10
        assert proc.poll() is not None
    finally:
        timer.cancel()
        watch.stop()
        if proc.poll() is None:
            proc.kill()


def test_a_second_interrupt_during_the_grace_still_stops_the_crawler(monkeypatch):
    class _Twice(_FakeProc):
        def wait(self, timeout=None):
            self.waits += 1
            if self.waits in (1, 2):
                raise KeyboardInterrupt  # 1 度目は待つ間、2 度目は猶予の間
            return -9

    proc = _Twice()
    stopped = []
    monkeypatch.setattr(crawler_client, "stop_process_tree", lambda p: stopped.append(p.pid))
    with pytest.raises(KeyboardInterrupt):
        crawler_client.wait_crawler(proc)
    assert stopped == [4242]


def test_commit_after_interrupt_recovers_a_session_broken_mid_flush(session):
    # 中断（KeyboardInterrupt）が flush の途中に割り込むと、セッションは rollback 待ちになり、後片付けの commit が
    # PendingRollbackError で失敗していた（2026-10-12、Windows の停止ファイルでの中断で再現）
    import database
    from models import Archive

    session.add(Archive(id=1, uid="dup", root_abs="/x"))  # 既にある id で flush を失敗させる
    with pytest.raises(Exception):
        session.flush()
    assert not session.is_active
    assert database.commit_after_interrupt(session) is True  # 投げない。捨てたことを返す（呼び出し元が知らせる）
    session.add(Archive(id=99, uid="after", root_abs="/y"))
    session.commit()  # その後も使える
    assert session.get(Archive, 99) is not None


def test_commit_after_interrupt_recovers_a_connection_broken_mid_statement(session):
    # 割り込みが接続の実行中に入ると、セッションは is_active のまま接続のトランザクションだけが無効になる
    # （2026-10-12 の Windows の E2E で再現。is_active だけでは見分けられない）
    import database
    from models import Archive

    session.add(Archive(id=5, uid="u", root_abs="/r"))
    session.flush()
    session.connection().invalidate()
    assert session.is_active
    assert database.commit_after_interrupt(session) is True  # 投げない。捨てたことを返す（呼び出し元が知らせる）
    session.add(Archive(id=99, uid="after", root_abs="/y"))
    session.commit()
    assert session.get(Archive, 99) is not None


def test_watch_retries_removing_a_locked_stop_file(monkeypatch, tmp_path):
    # ウイルス対策ソフトなどが一瞬つかんでいて消せないと、UI は残ったファイルを「受け取っていない」と読む（中断が記録されない）
    stop = tmp_path / "stop"
    stop.write_text("")
    real_remove = stop_watch.os.remove
    fails = []

    def flaky_remove(path):
        if len(fails) < 2:
            fails.append(path)
            raise PermissionError("locked")
        real_remove(path)

    monkeypatch.setattr(stop_watch.os, "remove", flaky_remove)
    calls = []
    watch = stop_watch.start(str(stop), interval=0.01, interrupt=lambda: calls.append(1))
    assert _wait_until(lambda: calls == [1])
    assert _wait_until(lambda: not stop.exists())  # 中断を起こした後に消し直す
    assert len(fails) == 2
    watch.stop()


def test_watch_leaves_the_stop_file_when_stopped_while_finding_it(monkeypatch, tmp_path):
    # ファイルを見つけた直後に見張りが止められた（実行が終わった）なら、消さずに残す（UI は残ったファイルで中断が
    # 届かなかったと分かる）。中断も起こさない
    stop = tmp_path / "stop"
    stop.write_text("")
    calls = []
    watch = stop_watch.StopWatch(str(stop), 0.01, lambda: calls.append(1))
    real_exists = stop_watch.os.path.exists

    def exists_then_stopped(path):
        found = real_exists(path)
        watch.stop()
        return found

    monkeypatch.setattr(stop_watch.os.path, "exists", exists_then_stopped)
    watch._watch()
    assert stop.exists() and calls == []


def test_commit_after_interrupt_reports_nothing_discarded_on_a_healthy_session(session):
    import database

    assert database.commit_after_interrupt(session) is False


def test_no_interrupt_after_stop_returns_even_while_removing(monkeypatch, tmp_path):
    # 停止ファイルを消し直している間に見張りが止められても、stop() から戻った後に中断を起こさない
    # （正常に終わった実行に中断が届く。PR #33 の bot の指摘）
    stop = tmp_path / "stop"
    stop.write_text("")
    removing = threading.Event()

    def locked_remove(path):
        removing.set()
        raise PermissionError("locked")

    monkeypatch.setattr(stop_watch.os, "remove", locked_remove)
    calls = []
    watch = stop_watch.start(str(stop), interval=0.01, interrupt=lambda: calls.append(1))
    assert removing.wait(2)
    before = list(calls)
    watch.stop()
    time.sleep(0.6)  # 消し直しの待ち（最大 REMOVE_TRIES × REMOVE_RETRY_WAIT）より長く
    assert calls == before


def test_stop_waits_for_the_stop_file_to_be_removed(monkeypatch, tmp_path):
    # 中断を起こした後に detector が先に終わると、残った停止ファイルを UI が「受け取っていない」と読む
    stop = tmp_path / "stop"
    stop.write_text("")
    real_remove = stop_watch.os.remove
    fails = []

    def flaky_remove(path):
        if len(fails) < 3:
            fails.append(path)
            raise PermissionError("locked")
        real_remove(path)

    monkeypatch.setattr(stop_watch.os, "remove", flaky_remove)
    calls = []
    watch = stop_watch.start(str(stop), interval=0.01, interrupt=lambda: calls.append(1))
    assert _wait_until(lambda: calls == [1])
    watch.stop()
    assert not stop.exists()
