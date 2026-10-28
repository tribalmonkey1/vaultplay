"""
thread_guard.py — make sure no QThread is still running when VaultPlay exits.

Destroying a running QThread makes Qt call qFatal("QThread: Destroyed while
thread is still running") -> SIGABRT -> coredump. VaultPlay has many ad-hoc
worker threads (scan, metadata, ProtonDB, version/update checks, playtime
watchers, and small workers defined inside settings_view), so instead of
hand-tracking each one, install() wraps QThread.start() to remember every
thread that gets started, and shutdown_all() stops them cleanly:

    1. watchers exposing request_abort() are told to stop (cooperative)
    2. requestInterruption() + quit() for everything
    3. wait() up to `grace_ms` in total
    4. anything still alive (e.g. blocked in a network call) is terminate()d
       and waited on — an abrupt stop is better than an abort/coredump

Call install() once before any thread is started (main.py) and
shutdown_all() from MainWindow.closeEvent and QApplication.aboutToQuit.
"""
import logging
import time
import weakref

from PyQt6.QtCore import QThread

log = logging.getLogger(__name__)

_threads: "weakref.WeakSet" = weakref.WeakSet()
_installed = False


def install():
    global _installed
    if _installed:
        return
    _orig_start = QThread.start

    def _start(self, *args, **kwargs):
        try:
            _threads.add(self)
        except TypeError:
            pass
        return _orig_start(self, *args, **kwargs)

    QThread.start = _start
    _installed = True


def _alive(t) -> bool:
    try:
        return t.isRunning()
    except RuntimeError:       # underlying C++ object already deleted
        return False


def shutdown_all(grace_ms: int = 3000):
    running = [t for t in list(_threads) if _alive(t)]
    if not running:
        return
    log.info("[SHUTDOWN] Stopping %d running thread(s)", len(running))
    for t in running:
        try:
            if hasattr(t, "request_abort"):
                t.request_abort()
            t.requestInterruption()
            t.quit()
        except RuntimeError:
            pass

    deadline = time.monotonic() + grace_ms / 1000.0
    for t in running:
        try:
            left = max(0, int((deadline - time.monotonic()) * 1000))
            t.wait(left)
        except RuntimeError:
            pass

    for t in running:
        if _alive(t):
            log.warning("[SHUTDOWN] Thread %s did not stop in time — terminating",
                        type(t).__name__)
            try:
                t.terminate()
                t.wait(1000)
            except RuntimeError:
                pass
