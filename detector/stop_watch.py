# stop_watch.py - 停止ファイルによる中断（#32）
#
# UI の「中断」は、macOS・Linux ではプロセスグループへの SIGINT で届く（KeyboardInterrupt として後片付けする）。
# Windows にはプロセスグループへのシグナルが無く、UI から送れるのは uv の強制終了だけで、その下の python（detector）には
# 中断が届かない。そこで UI は起動するときに停止ファイルの場所を環境変数で渡し、中断のときにそのファイルを作る。
# detector はファイルを見張るスレッドを立て、できたら主スレッドに KeyboardInterrupt を起こす（SIGINT と同じ後片付けになる）。
import _thread
import logging
import os
import threading
import time

STOP_FILE_ENV = "AKASYX_STOP_FILE"
POLL_INTERVAL = 0.3  # 秒
REMOVE_TRIES = 10
REMOVE_RETRY_WAIT = 0.05  # 秒

logger = logging.getLogger(__name__)


class StopWatch:
    """停止ファイルの見張り。stop() で見張りをやめる（実行が終わった後に中断を起こさない）。"""

    def __init__(self, path: str, interval: float, interrupt):
        self._path = path
        self._interval = interval
        self._interrupt = interrupt
        self._stopped = threading.Event()
        self.thread = threading.Thread(target=self._watch, name="stop-watch", daemon=True)

    def _watch(self):
        while not self._stopped.wait(self._interval):
            if os.path.exists(self._path):
                # 見張りをやめた後（実行が終わった）なら、中断は起こさずファイルも残す。UI は残ったファイルで
                # detector が受け取らなかった（中断ではなく自分で終わった）と分かる
                if self._stopped.is_set():
                    return
                # 中断は 1 度だけ（2 度目は後片付けを途中で止めうる）。受け取った印として停止ファイルを消す
                # （UI が先に終わっても残さない）
                _remove(self._path)
                self._interrupt()
                return

    def stop(self):
        self._stopped.set()


def _remove(path: str) -> None:
    """停止ファイルを消します。ウイルス対策ソフトなどが一瞬つかんでいて消せなければ、少し待って消し直す。"""
    for _ in range(REMOVE_TRIES - 1):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(REMOVE_RETRY_WAIT)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        # 中断はそのまま起こす（後片付けはする）。UI は残ったファイルを「受け取っていない」と読み、中断として記録しない
        logger.warning(f"Could not remove the stop file: {path}: {e}")


def start(path: str | None, interval: float = POLL_INTERVAL, interrupt=_thread.interrupt_main) -> StopWatch | None:
    """path の停止ファイルができたら interrupt を 1 度だけ呼ぶ見張りを立てます（path が無ければ立てない）。

    見張りのスレッドは daemon（終わるときに待たない）。
    """
    if not path:
        return None
    watch = StopWatch(path, interval, interrupt)
    watch.thread.start()
    return watch


def start_from_env() -> StopWatch | None:
    """環境変数 AKASYX_STOP_FILE があれば見張りを立てます（UI から起動されたとき）。"""
    return start(os.environ.get(STOP_FILE_ENV))
