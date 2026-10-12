# test_stop_watch.py - 停止ファイルでの中断（#32。Windows の UI からの中断）
import subprocess
import sys
import threading
import time

import crawler_client
import main
import pytest
import stop_watch


# 本物のプロセスグループにシグナルを送るテスト（ほかのテストは、偽の pid 4242 のグループに本当に送らない）
_REAL_SIGNAL_TESTS = {
    "test_a_crawler_ignoring_sigint_does_not_survive_the_interrupt",
    "test_detector_terminated_while_waiting_takes_the_crawler_down",
}


@pytest.fixture(autouse=True)
def _no_real_killpg(request, monkeypatch):
    if request.node.name in _REAL_SIGNAL_TESTS:
        return

    def gone(pgid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(crawler_client.os, "killpg", gone, raising=False)


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
    _record_killpg(monkeypatch)
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


# --- macOS・Linux: crawler をプロセスグループごと止める（#35） ---------------------------------------------


def _record_killpg(monkeypatch):
    sent = []
    monkeypatch.setattr(crawler_client, "_is_windows", lambda: False)
    monkeypatch.setattr(crawler_client.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)), raising=False)
    return sent


def test_crawler_runs_in_its_own_process_group_outside_windows(make_config, tmp_path, monkeypatch):
    # 自前のグループで起動する（uv が止まっても、その下の python をグループごと止められるように）
    seen = {}

    class _Done:
        pid = 4242

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs)
        return _Done()

    monkeypatch.setattr(crawler_client, "_is_windows", lambda: False)
    monkeypatch.setattr(crawler_client, "crawler_command", lambda config: (["crawler"], str(tmp_path)))
    monkeypatch.setattr(crawler_client.subprocess, "Popen", fake_popen)
    with pytest.raises(Exception):
        crawler_client.run_crawler(str(tmp_path), make_config())  # DB が無いので止まる（起動の仕方だけ見る）
    assert seen.get("start_new_session") is True


def test_crawler_is_not_given_a_new_session_on_windows(make_config, tmp_path, monkeypatch):
    seen = {}

    class _Done:
        pid = 4242

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs)
        return _Done()

    monkeypatch.setattr(crawler_client, "_is_windows", lambda: True)
    monkeypatch.setattr(crawler_client, "crawler_command", lambda config: (["crawler"], str(tmp_path)))
    monkeypatch.setattr(crawler_client.subprocess, "Popen", fake_popen)
    with pytest.raises(Exception):
        crawler_client.run_crawler(str(tmp_path), make_config())
    assert not seen.get("start_new_session")


def test_interrupt_is_forwarded_to_the_crawler_group(monkeypatch):
    # crawler は別のグループなので、UI・端末からの SIGINT は届かない。detector が受けた中断を crawler のグループに送る
    import signal

    sent = _record_killpg(monkeypatch)
    stopped = []
    monkeypatch.setattr(crawler_client, "stop_process_tree", lambda p: stopped.append(p.pid))
    proc = _FakeProc()
    with pytest.raises(KeyboardInterrupt):
        crawler_client.wait_crawler(proc)
    assert sent == [(4242, signal.SIGINT)]  # 猶予の前に 1 度だけ
    assert proc.timeouts[-1] == crawler_client.CRAWLER_STOP_GRACE
    assert stopped == [4242]


def test_interrupt_is_not_forwarded_by_signal_on_windows(monkeypatch):
    sent = []
    monkeypatch.setattr(crawler_client, "_is_windows", lambda: True)
    monkeypatch.setattr(crawler_client.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)), raising=False)
    monkeypatch.setattr(crawler_client, "stop_process_tree", lambda p: None)
    with pytest.raises(KeyboardInterrupt):
        crawler_client.wait_crawler(_FakeProc())
    assert sent == []


def test_stop_process_tree_kills_the_whole_group_even_after_uv_exited(monkeypatch):
    # uv が先に終わっていても、その下の python（crawler）が残りうる。グループごと止める指示は必ず出す
    sent = _record_killpg(monkeypatch)
    proc = _FakeProc()
    proc.killed = True  # uv はもう終わっている
    crawler_client.stop_process_tree(proc)
    assert (4242, crawler_client.SIGKILL) in sent


def test_stop_process_tree_tolerates_a_group_that_is_already_gone(monkeypatch):
    monkeypatch.setattr(crawler_client, "_is_windows", lambda: False)

    def gone(pgid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(crawler_client.os, "killpg", gone, raising=False)
    proc = _FakeProc()
    crawler_client.stop_process_tree(proc)  # 投げない
    assert proc.killed


@pytest.mark.skipif(sys.platform == "win32", reason="macOS・Linux のプロセスグループの実物の確かめ")
def test_a_crawler_ignoring_sigint_does_not_survive_the_interrupt(tmp_path):
    # uv の代わりの親と、SIGINT を無視する子（crawler の代わり）。中断の後、子も残らない
    import os
    import signal

    pid_file = tmp_path / "child.pid"
    child = (
        "import os, signal, sys, time; signal.signal(signal.SIGINT, signal.SIG_IGN); "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    # 親は uv と同じく SIGINT では子を止めない（subprocess.call は中断で子を kill するので、無視させる）。
    # 子が残らないのは、グループごとの SIGKILL だけによる
    parent = (
        "import signal, subprocess, sys; signal.signal(signal.SIGINT, signal.SIG_IGN); "
        f"subprocess.call([sys.executable, '-c', {child!r}])"
    )
    proc = subprocess.Popen([sys.executable, "-c", parent], start_new_session=True)
    try:
        assert _wait_until(lambda: pid_file.exists() and pid_file.read_text(), timeout=10)
        child_pid = int(pid_file.read_text())

        class _Interrupting:
            pid = proc.pid
            first = True

            def wait(self, timeout=None):
                if self.first:
                    self.first = False
                    raise KeyboardInterrupt
                return proc.wait(timeout=timeout)

            def poll(self):
                return proc.poll()

            def kill(self):
                proc.kill()

        with pytest.raises(KeyboardInterrupt):
            crawler_client.wait_crawler(_Interrupting())

        def gone():
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                return True
            return False

        assert _wait_until(gone, timeout=5)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=5)


def test_grace_continues_while_the_crawler_outlives_uv(monkeypatch):
    # uv が先に終わっても、その下の python が後片付けをしている間は猶予の終わりまで待つ（すぐ SIGKILL しない）
    import signal

    probes = []
    events = []

    def killpg(pgid, sig):
        if sig == 0:
            probes.append(1)
            if len(probes) >= 3:
                raise ProcessLookupError  # 3 度目の確認で居なくなった
            return
        events.append(("signal", sig))

    monkeypatch.setattr(crawler_client, "_is_windows", lambda: False)
    monkeypatch.setattr(crawler_client.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(crawler_client, "GROUP_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(crawler_client, "stop_process_tree", lambda p: events.append(("stop", None)))

    class _UvExitsAtOnce(_FakeProc):
        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            return 0  # uv は SIGINT ですぐ終わる

    with pytest.raises(KeyboardInterrupt):
        crawler_client.wait_crawler(_UvExitsAtOnce())
    assert len(probes) == 3  # グループが空になるまで待った
    assert events == [("signal", signal.SIGINT), ("stop", None)]


def test_grace_ends_even_if_the_group_never_empties(monkeypatch):
    monkeypatch.setattr(crawler_client, "_is_windows", lambda: False)
    monkeypatch.setattr(crawler_client.os, "killpg", lambda pgid, sig: None, raising=False)  # いつまでも居る
    monkeypatch.setattr(crawler_client, "GROUP_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(crawler_client, "CRAWLER_STOP_GRACE", 0.2)
    stopped = []
    monkeypatch.setattr(crawler_client, "stop_process_tree", lambda p: stopped.append(p.pid))

    class _UvExitsAtOnce(_FakeProc):
        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            return 0

    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        crawler_client.wait_crawler(_UvExitsAtOnce())
    assert time.monotonic() - started < 2
    assert stopped == [4242]


def test_termination_while_waiting_is_forwarded_to_the_crawler_group(monkeypatch):
    # crawler は別のグループ。端末を閉じた（SIGHUP）・kill された（SIGTERM）detector だけが終わり crawler が残らないよう、
    # 同じシグナルを crawler のグループにも送り、元の扱いに戻して detector 自身にも送り直す
    import signal

    sent = []
    resent = []
    monkeypatch.setattr(crawler_client, "_is_windows", lambda: False)
    monkeypatch.setattr(crawler_client.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)), raising=False)
    monkeypatch.setattr(crawler_client.os, "kill", lambda pid, sig: resent.append((pid, sig)))
    before = signal.getsignal(signal.SIGTERM)
    with crawler_client._forward_termination([_FakeProc()]):
        handler = signal.getsignal(signal.SIGTERM)
        assert handler is not before
        handler(signal.SIGTERM, None)
        assert signal.getsignal(signal.SIGTERM) is before  # 送り直す前に元の扱いに戻した
    assert sent == [(4242, signal.SIGTERM)]
    assert resent == [(crawler_client.os.getpid(), signal.SIGTERM)]
    assert signal.getsignal(signal.SIGTERM) is before


def test_termination_handlers_are_restored_after_the_wait(monkeypatch):
    import signal

    monkeypatch.setattr(crawler_client, "_is_windows", lambda: False)
    before = signal.getsignal(signal.SIGTERM)

    class _Done(_FakeProc):
        def wait(self, timeout=None):
            return 0

    assert crawler_client.wait_crawler(_Done()) == 0
    assert signal.getsignal(signal.SIGTERM) is before


@pytest.mark.skipif(sys.platform == "win32", reason="SIGHUP は macOS・Linux だけ")
def test_ignored_hangup_is_left_alone(monkeypatch):
    import signal

    monkeypatch.setattr(crawler_client, "_is_windows", lambda: False)
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)  # nohup で起動されたとき
    try:
        with crawler_client._forward_termination([_FakeProc()]):
            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)


@pytest.mark.skipif(sys.platform == "win32", reason="macOS・Linux のプロセスグループの実物の確かめ")
def test_detector_terminated_while_waiting_takes_the_crawler_down(tmp_path):
    # detector（crawler を待っている）が SIGTERM で終わっても、crawler（別のグループ・SIGINT を無視）が残らない
    import os
    import signal

    pid_file = tmp_path / "child.pid"
    child = (
        "import os, signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    detector_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    detector = (
        f"import subprocess, sys; sys.path.insert(0, {detector_dir!r}); import crawler_client; "
        f"p = subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True); "
        "crawler_client.wait_crawler(p)"
    )
    proc = subprocess.Popen([sys.executable, "-c", detector])
    child_pid = None
    try:
        assert _wait_until(lambda: pid_file.exists() and pid_file.read_text(), timeout=10)
        child_pid = int(pid_file.read_text())
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        assert proc.returncode == -signal.SIGTERM  # detector は今までどおり SIGTERM で終わる

        def gone():
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                return True
            return False

        assert _wait_until(gone, timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_termination_is_still_forwarded_during_the_grace(monkeypatch):
    # 中断の後の猶予の間に SIGTERM が来ても（止めるのを急かされた）、detector だけが終わって crawler が残らない
    import signal

    monkeypatch.setattr(crawler_client, "_is_windows", lambda: False)
    monkeypatch.setattr(crawler_client, "stop_process_tree", lambda p: None)
    before = signal.getsignal(signal.SIGTERM)
    during_grace = []

    class _Proc(_FakeProc):
        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            during_grace.append(signal.getsignal(signal.SIGTERM))
            return 0

    with pytest.raises(KeyboardInterrupt):
        crawler_client.wait_crawler(_Proc())
    assert during_grace and during_grace[0] is not before
    assert signal.getsignal(signal.SIGTERM) is before


def _spawned(monkeypatch, tmp_path, wait_crawler):
    stopped = []

    class _Started(_FakeProc):
        pass

    monkeypatch.setattr(crawler_client, "crawler_command", lambda config: (["crawler"], str(tmp_path)))
    monkeypatch.setattr(crawler_client.subprocess, "Popen", lambda cmd, **kw: _Started())
    monkeypatch.setattr(crawler_client, "wait_crawler", wait_crawler)
    monkeypatch.setattr(crawler_client, "stop_process_tree", lambda p: stopped.append(p.pid))
    return stopped


def test_interrupt_right_after_the_crawler_started_still_stops_it(make_config, tmp_path, monkeypatch):
    # 起動した直後・wait_crawler が後片付けを引き受ける前の中断（#35）
    def interrupted_before_waiting(proc):
        raise KeyboardInterrupt

    stopped = _spawned(monkeypatch, tmp_path, interrupted_before_waiting)
    with pytest.raises(KeyboardInterrupt):
        crawler_client.run_crawler(str(tmp_path), make_config())
    assert stopped == [4242]


def test_crawler_stopped_by_wait_is_not_stopped_twice(make_config, tmp_path, monkeypatch):
    def stopped_while_waiting(proc):
        proc.stopped_by_detector = True
        raise KeyboardInterrupt

    stopped = _spawned(monkeypatch, tmp_path, stopped_while_waiting)
    with pytest.raises(KeyboardInterrupt):
        crawler_client.run_crawler(str(tmp_path), make_config())
    assert stopped == []


def test_termination_is_forwarded_from_before_the_crawler_starts(make_config, tmp_path, monkeypatch):
    # 起動した直後（wait_crawler が始まる前）に届いた SIGTERM も crawler に送る
    import signal

    monkeypatch.setattr(crawler_client, "_is_windows", lambda: False)
    monkeypatch.setattr(crawler_client, "crawler_command", lambda config: (["crawler"], str(tmp_path)))
    monkeypatch.setattr(crawler_client, "stop_process_tree", lambda p: None)
    monkeypatch.setattr(crawler_client.subprocess, "Popen", lambda cmd, **kw: _FakeProc())
    seen = []

    def wait_after_a_signal(proc):
        seen.append(signal.getsignal(signal.SIGTERM))
        raise KeyboardInterrupt

    monkeypatch.setattr(crawler_client, "wait_crawler", wait_after_a_signal)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt):
        crawler_client.run_crawler(str(tmp_path), make_config())
    assert seen and getattr(seen[0], "forwards_to_crawler", False)  # wait_crawler の前から送り直しの用意がある
    assert signal.getsignal(signal.SIGTERM) is before


def test_forwarding_continues_after_a_python_handler_that_returns(monkeypatch):
    import signal

    sent = []
    calls = []
    monkeypatch.setattr(crawler_client, "_is_windows", lambda: False)
    monkeypatch.setattr(crawler_client.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)), raising=False)
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: calls.append(signum))
    try:
        with crawler_client._forward_termination([_FakeProc()]):
            handler = signal.getsignal(signal.SIGTERM)
            handler(signal.SIGTERM, None)
            assert signal.getsignal(signal.SIGTERM) is handler  # 送り直しを続ける
            handler(signal.SIGTERM, None)
        assert calls == [signal.SIGTERM, signal.SIGTERM]
        assert sent == [(4242, signal.SIGTERM), (4242, signal.SIGTERM)]
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_nested_forwarding_does_not_send_twice(monkeypatch):
    import signal

    sent = []
    monkeypatch.setattr(crawler_client, "_is_windows", lambda: False)
    monkeypatch.setattr(crawler_client.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)), raising=False)
    monkeypatch.setattr(crawler_client.os, "kill", lambda pid, sig: None)
    proc = _FakeProc()
    with crawler_client._forward_termination([proc]):
        outer = signal.getsignal(signal.SIGTERM)
        with crawler_client._forward_termination([proc]):
            assert signal.getsignal(signal.SIGTERM) is outer
            outer(signal.SIGTERM, None)
    assert sent == [(4242, signal.SIGTERM)]
