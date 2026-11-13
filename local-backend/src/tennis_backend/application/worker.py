"""Non-blocking task execution isolated from the protocol read loop (BE-012).

The NDJSON transport (BE-010) keeps the read loop responsive; this module is
the application-side executor behind it: work units run on dedicated worker
threads while callers poll and cancel at any time.

Guarantees
----------

- **Isolation** — ``submit`` returns immediately; the protocol thread never
  runs job code. ``status`` reads a snapshot under a lock, so a slow job
  cannot delay queries.
- **Cooperative cancellation** — jobs receive a :class:`CancellationToken`
  and must call :meth:`CancellationToken.check_point` between stages.
  Normal Python threads cannot be killed, so cancellation takes effect at
  the next check point; a job that never checks simply runs to completion
  and is reported ``cancelled_after_finish``… it is reported by its actual
  terminal state, never claimed cancelled while still running.
- **Exit stops subtasks** — ``shutdown`` refuses new submissions, cancels
  every queued and running job (at their next check point) and waits for
  the workers; threads are never claimed to be killed.
- **No infinite retry** — a job runs once per accepted submission; there is
  no internal retry loop. Failed jobs stay failed until explicitly
  resubmitted, and resubmission is bounded by ``max_attempts`` for the
  job id; once the budget is spent the id is refused (``RETRY_EXHAUSTED``).

Threading note (AG-B02): SQLite connections have thread affinity. Job code
must open its own stores on the worker thread; the executor only supplies
the token and bookkeeping.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from tennis_backend.domain.models import DomainViolation

#: Terminal job states.
State = str  # "queued" | "running" | "succeeded" | "failed" | "cancelled"

_CANCELLED_CODE = "CANCELLED"
_RETRY_EXHAUSTED = "RETRY_EXHAUSTED"


class JobCancelled(Exception):
    """Raised at a check point after cancellation was requested."""


class CancellationToken:
    """Cooperative cancellation flag handed to every job run."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check_point(self) -> None:
        """Call between stages; raises :class:`JobCancelled` once cancelled."""
        if self._event.is_set():
            raise JobCancelled


@dataclass
class _Job:
    job_id: str
    run: Callable[[CancellationToken], Any]
    max_attempts: int
    state: State = "queued"
    attempts: int = 0  # cumulative: every accepted submission counts, never reset
    result: Any = None
    error_code: str | None = None
    token: CancellationToken = field(default_factory=CancellationToken)


@dataclass(frozen=True)
class JobStatus:
    """An immutable snapshot of one job's bookkeeping."""

    job_id: str
    state: State
    attempts: int
    max_attempts: int
    result: Any = None
    error_code: str | None = None


class NonBlockingWorker:
    """Bounded executor for cancellable application work units."""

    def __init__(
        self,
        *,
        max_workers: int = 2,
        max_queued: int = 16,
    ) -> None:
        self._slots = threading.BoundedSemaphore(max_workers + max_queued)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job")
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        self._shutting_down = False

    # ------------------------------------------------------------------
    # Submission and queries
    # ------------------------------------------------------------------

    def submit(
        self,
        job_id: str,
        run: Callable[[CancellationToken], Any],
        *,
        max_attempts: int = 1,
    ) -> JobStatus:
        """Queue one work unit; returns immediately.

        Submitting an id that is queued or running is refused. A succeeded or
        cancelled job replays its terminal snapshot (idempotent, EX-005). A
        failed job may be resubmitted until ``max_attempts`` is spent.
        """
        with self._lock:
            if self._shutting_down:
                raise DomainViolation(
                    "WORKER_STOPPED", f"worker is shutting down; {job_id} refused"
                )
            existing = self._jobs.get(job_id)
            if existing is not None:
                if existing.state in ("queued", "running"):
                    raise DomainViolation(
                        "ALREADY_EXISTS", f"job {job_id} is already {existing.state}"
                    )
                if existing.state in ("succeeded", "cancelled"):
                    return self._snapshot(existing)
                if existing.attempts >= existing.max_attempts:
                    raise DomainViolation(
                        _RETRY_EXHAUSTED,
                        f"job {job_id} spent {existing.attempts}/{existing.max_attempts} attempts",
                    )
            if not self._slots.acquire(blocking=False):
                # Check the budget before mutating anything, so a refused
                # submission never leaves a half-reset failed job behind.
                raise DomainViolation("WORKER_BUSY", f"worker queue is full; {job_id} refused")
            if existing is not None:
                # bounded explicit retry: the cumulative attempt count is
                # never reset, so the budget can actually be exhausted
                existing.state = "queued"
                existing.error_code = None
                existing.result = None
                existing.token = CancellationToken()
                job = existing
            else:
                job = _Job(job_id=job_id, run=run, max_attempts=max_attempts)
            job.attempts += 1
            self._jobs[job_id] = job
        try:
            self._pool.submit(self._run_job, job)
        except RuntimeError:
            # the pool stopped between the flag check and the submission:
            # undo the bookkeeping instead of leaking the queue slot
            with self._lock:
                job.state = "cancelled"
                job.error_code = _CANCELLED_CODE
                self._slots.release()
            raise
        return self._snapshot(job)

    def status(self, job_id: str) -> JobStatus:
        """Snapshot of one job; unknown ids are refused with UNKNOWN_JOB."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise DomainViolation("UNKNOWN_JOB", f"job {job_id} is not tracked")
            return self._snapshot(job)

    def cancel(self, job_id: str) -> JobStatus:
        """Request cancellation; takes effect at the job's next check point.

        Queued jobs become ``cancelled`` at once. Running jobs keep the
        ``running`` state until their run honours the token; cancelling a
        terminal job changes nothing (idempotent).
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise DomainViolation("UNKNOWN_JOB", f"job {job_id} is not tracked")
            if job.state == "queued":
                job.state = "cancelled"
                job.error_code = _CANCELLED_CODE
            elif job.state == "running":
                job.token.cancel()
            return self._snapshot(job)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self, *, wait_timeout: float = 30.0) -> tuple[str, ...]:
        """Stop accepting work and cancel queued/running jobs at their next
        check point; wait up to ``wait_timeout`` for them to finish.

        Returns the ids of jobs still unfinished when the timeout expired —
        threads are never force-killed, so a job that never calls
        :meth:`CancellationToken.check_point` runs to completion on its own
        after this call returns; the process exit joins it. An empty tuple
        means every job reached a terminal state.
        """
        with self._lock:
            self._shutting_down = True
            for job in self._jobs.values():
                if job.state == "queued":
                    job.state = "cancelled"
                    job.error_code = _CANCELLED_CODE
                elif job.state == "running":
                    job.token.cancel()
        deadline = time.monotonic() + wait_timeout
        unfinished: list[str] = []
        while True:
            # evaluate at least once, so wait_timeout <= 0 still reports the
            # jobs that were cancelled but have not finished (never lie)
            with self._lock:
                unfinished = [
                    job.job_id for job in self._jobs.values() if job.state in ("queued", "running")
                ]
            if not unfinished or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        # Workers not yet done keep running on their own threads; they are
        # never killed. The pool refuses further submissions via the flag.
        self._pool.shutdown(wait=False)
        return tuple(unfinished)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_job(self, job: _Job) -> None:
        cancelled_before_start = job.token.cancelled
        with self._lock:
            if job.state == "cancelled":  # cancelled while queued
                self._slots.release()
                return
            job.state = "running"
        if cancelled_before_start:
            with self._lock:
                job.state = "cancelled"
                job.error_code = _CANCELLED_CODE
                self._slots.release()
            return
        try:
            if job.token.cancelled:
                raise JobCancelled
            result = job.run(job.token)
        except JobCancelled:
            with self._lock:
                job.state = "cancelled"
                job.error_code = _CANCELLED_CODE
        except DomainViolation as violation:
            with self._lock:
                job.state = "failed"
                job.error_code = violation.code
        except Exception:
            with self._lock:
                job.state = "failed"
                job.error_code = "JOB_FAILED"
        else:
            with self._lock:
                job.state = "succeeded"
                job.result = result
        finally:
            self._slots.release()

    def _snapshot(self, job: _Job) -> JobStatus:
        return JobStatus(
            job_id=job.job_id,
            state=job.state,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            result=job.result,
            error_code=job.error_code,
        )
