"""Regression tests for worker-thread lifetime.

These guard the crash

    QThread: Destroyed while thread '' is still running

which aborts the process rather than raising, so a regression here fails the
whole test run loudly instead of one assertion. That is the intent.
"""

import time

import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal

from app import worker as worker_module
from app.worker import run_in_thread, wait_for_threads


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


class _Worker(QObject):
    """Minimal stand-in for ExportWorker: emits ``finished`` and returns."""

    finished = Signal(int, int, int)

    def run(self) -> None:
        self.finished.emit(1, 0, 0)


class _Receiver(QObject):
    """A main-thread QObject receiver, so the slot is queued exactly as
    MainWindow's handlers are — that ordering is what triggered the crash."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []
        self.thread_ref = None

    def on_finished(self, success: int, failure: int, skipped: int) -> None:
        # Precisely what MainWindow._on_export_finished did: drop the caller's
        # only reference to the QThread from inside the finished handler.
        self.thread_ref = None
        self.calls.append((success, failure, skipped))


def _pump(app, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_clearing_the_caller_reference_mid_finish_is_safe(qapp):
    receiver = _Receiver()
    work = _Worker()
    work.finished.connect(receiver.on_finished)
    receiver.thread_ref = run_in_thread(work)

    assert _pump(qapp, lambda: bool(receiver.calls)), "worker never finished"
    assert receiver.calls == [(1, 0, 0)]
    # If the thread had been destroyed while running, the process would be
    # gone by now rather than reaching this line.
    assert wait_for_threads(3000)


def test_registry_is_emptied_once_threads_finish(qapp):
    work = _Worker()
    done: list[bool] = []
    work.finished.connect(lambda *_: done.append(True))
    run_in_thread(work)

    assert _pump(qapp, lambda: bool(done))
    assert wait_for_threads(3000)
    # deleteLater/finished handling needs one more spin of the loop.
    _pump(qapp, lambda: not worker_module._running, timeout=2.0)
    assert worker_module.running_thread_count() == 0


def test_wait_for_threads_is_a_noop_with_nothing_running(qapp):
    assert wait_for_threads(100) is True


def test_several_threads_can_run_and_all_are_waited_for(qapp):
    finished: list[int] = []
    for _ in range(4):
        work = _Worker()
        work.finished.connect(lambda *_: finished.append(1))
        run_in_thread(work)

    assert _pump(qapp, lambda: len(finished) == 4), f"only {len(finished)} of 4"
    assert wait_for_threads(3000)
