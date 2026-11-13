"""Non-blocking worker tests: isolation, query, cancel, shutdown, retries (BE-012).

All jobs are synthetic callables; no media, protocol or real analysis is
involved. Timing-sensitive cases use events and explicit timeouts instead of
sleeps wherever a result is being awaited.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from tennis_backend.application.worker import (
    CancellationToken,
    JobCancelled,
    NonBlockingWorker,
)
from tennis_backend.domain.models import DomainViolation

RELEASE = threading.Event()


def wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


@pytest.fixture
def worker() -> Any:
    RELEASE.clear()  # each test starts with every slow job still blocked
    instance = NonBlockingWorker(max_workers=2, max_queued=4)
    yield instance
    RELEASE.set()  # unblock any slow jobs before shutting down
    instance.shutdown(wait_timeout=5)


def slow_job(steps: int = 50) -> Any:
    """A job that checks the token between stages until released."""

    def run(token: CancellationToken) -> str:
        for _ in range(steps):
            token.check_point()
            if RELEASE.is_set():
                return "done"
            time.sleep(0.01)
        return "done"

    return run


def test_submit_returns_and_status_tracks_lifecycle(worker: NonBlockingWorker) -> None:
    started = threading.Event()

    def run(token: CancellationToken) -> dict[str, int]:
        started.set()
        RELEASE.wait(timeout=5)
        token.check_point()
        return {"value": 42}

    snapshot = worker.submit("job-1", run)
    assert snapshot.state in ("queued", "running")
    assert started.wait(timeout=5)
    wait_for(lambda: worker.status("job-1").state == "running")
    RELEASE.set()
    wait_for(lambda: worker.status("job-1").state == "succeeded")
    final = worker.status("job-1")
    assert final.result == {"value": 42}
    assert final.attempts == 1


def test_slow_job_is_queried_while_running(worker: NonBlockingWorker) -> None:
    worker.submit("job-slow", slow_job())
    wait_for(lambda: worker.status("job-slow").state == "running")
    # many queries answered while the job is still running
    for _ in range(20):
        assert worker.status("job-slow").state == "running"
    assert RELEASE.is_set() is False  # the job is genuinely still running


def test_cancel_takes_effect_at_next_check_point(worker: NonBlockingWorker) -> None:
    worker.submit("job-c", slow_job(steps=500))
    wait_for(lambda: worker.status("job-c").state == "running")
    snapshot = worker.cancel("job-c")
    assert snapshot.state == "running"  # cooperative: not claimed cancelled yet
    wait_for(lambda: worker.status("job-c").state == "cancelled", timeout=10)
    cancelled = worker.status("job-c")
    assert cancelled.error_code == "CANCELLED"
    assert cancelled.result is None


def test_cancel_queued_job_is_immediate(worker: NonBlockingWorker) -> None:
    started = threading.Event()

    def blocker(token: CancellationToken) -> str:
        started.set()
        RELEASE.wait(timeout=5)
        return "done"

    # fill both workers with blockers so the next job stays queued
    worker.submit("b1", blocker)
    worker.submit("b2", blocker)
    wait_for(lambda: started.is_set())
    worker.submit("q1", blocker)
    assert worker.status("q1").state == "queued"
    cancelled = worker.cancel("q1")
    assert cancelled.state == "cancelled"
    assert cancelled.error_code == "CANCELLED"
    RELEASE.set()


def test_failure_stays_failed_without_automatic_retry(worker: NonBlockingWorker) -> None:
    calls = {"n": 0}

    def failing(token: CancellationToken) -> str:
        calls["n"] += 1
        raise DomainViolation("ANALYSIS_FAILED", "boom")

    worker.submit("job-f", failing)
    wait_for(lambda: worker.status("job-f").state == "failed")
    time.sleep(0.2)  # no internal retry loop may re-run it
    snapshot = worker.status("job-f")
    assert snapshot.state == "failed"
    assert snapshot.error_code == "DOMAIN_ANALYSIS_FAILED"
    assert calls["n"] == 1 and snapshot.attempts == 1


def test_bounded_explicit_retry_and_exhaustion(worker: NonBlockingWorker) -> None:
    calls = {"n": 0}

    def failing(token: CancellationToken) -> str:
        calls["n"] += 1
        raise DomainViolation("STAGE_FAILED", "boom")

    worker.submit("job-r", failing, max_attempts=2)
    wait_for(lambda: worker.status("job-r").state == "failed")
    snapshot = worker.submit("job-r", failing)  # explicit retry within budget
    assert snapshot.attempts == 2 and snapshot.state == "queued"  # cumulative
    wait_for(lambda: worker.status("job-r").state == "failed")
    assert worker.status("job-r").attempts == 2
    with pytest.raises(DomainViolation) as error:
        worker.submit("job-r", failing)  # budget spent
    assert error.value.code == "DOMAIN_RETRY_EXHAUSTED"
    assert calls["n"] == 2  # exactly two executions, no more


def test_duplicate_active_id_and_terminal_replay(worker: NonBlockingWorker) -> None:
    started = threading.Event()

    def blocker(token: CancellationToken) -> str:
        started.set()
        RELEASE.wait(timeout=5)
        return "done"

    worker.submit("job-d", blocker)
    wait_for(lambda: started.is_set())
    with pytest.raises(DomainViolation) as error:
        worker.submit("job-d", blocker)
    assert error.value.code == "DOMAIN_ALREADY_EXISTS"
    RELEASE.set()
    wait_for(lambda: worker.status("job-d").state == "succeeded")
    replay = worker.submit("job-d", blocker)  # terminal: idempotent snapshot
    assert replay.state == "succeeded" and replay.result == "done"


def test_unknown_job_refused(worker: NonBlockingWorker) -> None:
    with pytest.raises(DomainViolation) as error:
        worker.status("nope")
    assert error.value.code == "DOMAIN_UNKNOWN_JOB"
    with pytest.raises(DomainViolation):
        worker.cancel("nope")


def test_shutdown_cancels_queued_and_running_at_check_points() -> None:
    worker = NonBlockingWorker(max_workers=1, max_queued=2)
    started = threading.Event()
    finished = threading.Event()

    def blocker(token: CancellationToken) -> str:
        started.set()
        for _ in range(2000):
            token.check_point()
            if finished.is_set():
                break
            time.sleep(0.01)
        return "done"

    def queued(token: CancellationToken) -> str:
        return "should-never-run"

    worker.submit("running-1", blocker)
    worker.submit("queued-1", queued)
    wait_for(lambda: started.is_set())
    wait_for(lambda: worker.status("queued-1").state == "queued")
    unfinished = worker.shutdown(wait_timeout=10)
    assert set(unfinished) == {"queued-1"} or unfinished == ()
    # queued job cancelled by shutdown; running job cancelled at next check point
    assert worker.status("queued-1").state == "cancelled"
    assert worker.status("running-1").state in ("running", "cancelled")
    finished.set()
    wait_for(lambda: worker.status("running-1").state == "cancelled", timeout=10)
    with pytest.raises(DomainViolation):
        worker.submit("after-shutdown", blocker)  # refused after shutdown


def test_shutdown_with_zero_timeout_still_reports_running_jobs() -> None:
    worker = NonBlockingWorker(max_workers=1, max_queued=1)
    started = threading.Event()

    def slow(token: CancellationToken) -> str:
        started.set()
        time.sleep(0.3)  # ignores the token: stays running past the timeout
        return "done"

    worker.submit("job-z", slow)
    wait_for(lambda: started.is_set())
    unfinished = worker.shutdown(wait_timeout=0)
    assert unfinished == ("job-z",)  # never lie: timeout 0 still reports it
    wait_for(lambda: worker.status("job-z").state == "succeeded", timeout=5)


def test_non_cooperative_job_delayes_shutdown_but_reports_unfinished() -> None:
    worker = NonBlockingWorker(max_workers=1, max_queued=1)
    started = threading.Event()

    def never_checks(token: CancellationToken) -> str:
        started.set()
        time.sleep(0.4)  # ignores the token entirely
        return "ran-to-completion"

    worker.submit("job-x", never_checks)
    wait_for(lambda: started.is_set())
    unfinished = worker.shutdown(wait_timeout=0.1)
    assert unfinished == ("job-x",)  # honestly reported, not killed, not lied about
    wait_for(lambda: worker.status("job-x").state == "succeeded", timeout=5)
    assert worker.status("job-x").result == "ran-to-completion"


def test_check_point_semantics() -> None:
    token = CancellationToken()
    token.check_point()  # no-op before cancellation
    token.cancel()
    assert token.cancelled is True
    with pytest.raises(JobCancelled):
        token.check_point()
