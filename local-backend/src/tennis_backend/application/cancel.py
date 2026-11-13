"""Cancellation with checkpoint retention (BE-022).

Cancels one non-terminal task while preserving exactly what the spec says
(AC-SPEC-009/039, FR-023):

- **Only active states cancel** — the BE-005 state machine decides; a task
  already in a settled state (complete, partial, reshoot_required,
  unsupported, safety_stop) is refused without changing it (原终态不变),
  and ``deleted`` is absolutely terminal.
- **Complete checkpoints are kept, incomplete stages are discarded** — the
  durable ``checking_input`` etc. checkpoints stay readable, and
  ``incomplete_stages`` lists exactly what was dropped; no half stage is
  ever shown as done (隐藏未完成).
- **Derived writes stop** — after a cancellation the task refuses further
  derived writes (:func:`assert_derivative_writes_allowed`), and any
  running worker tokens handed to :meth:`CancelService.cancel` are cancelled
  so in-flight work stops at its next check point (停派生写入).
- **Repeating the cancel is stable** — a second cancellation returns the
  same outcome without adding a transition event (重复一致).
- **Cancellation never deletes** (不自动删除) — media rows, resources and
  checkpoints all remain; deletion is the separate BE-025 flow.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tennis_backend.application.worker import CancellationToken
from tennis_backend.domain.models import DomainViolation
from tennis_backend.domain.states import (
    CANCELLABLE_TASK_STATES,
    SETTLED_TASK_STATES,
    TERMINAL_TASK_STATES,
    cancel_task,
)
from tennis_backend.storage.metadata import MetadataStore

_UNKNOWN_TASK = "UNKNOWN_TASK"
_ALREADY_SETTLED = "CANCEL_NOT_CANCELLABLE"
_TERMINAL = "CANCEL_TERMINAL"
_WRITES_BLOCKED = "CANCELLED_WRITES_BLOCKED"


@dataclass(frozen=True)
class CancelOutcome:
    """The stable result of one cancellation request."""

    task_id: str
    state: str
    already_cancelled: bool
    retained_checkpoints: tuple[str, ...]
    discarded_stages: tuple[str, ...]
    derivative_writes_blocked: bool
    data_removed: bool
    notes: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state,
            "already_cancelled": self.already_cancelled,
            "retained_checkpoints": list(self.retained_checkpoints),
            "discarded_stages": list(self.discarded_stages),
            "derivative_writes_blocked": self.derivative_writes_blocked,
            "data_removed": self.data_removed,
            "notes": list(self.notes),
        }


def assert_derivative_writes_allowed(metadata: MetadataStore, task_id: str) -> None:
    """Refuse derived writes once a task is cancelled or settled.

    Analysis, evidence and render stages call this before publishing; a
    cancelled task's checkpoint data stays readable, but nothing new may be
    derived from it. Deletion flows (BE-025) are a separate authorized path.
    """
    state = metadata.task_state(task_id)
    if state is None:
        raise DomainViolation(_UNKNOWN_TASK, f"task {task_id} is not stored")
    if state == "cancelled" or state in SETTLED_TASK_STATES:
        raise DomainViolation(
            _WRITES_BLOCKED,
            f"task {task_id} is {state}; derived writes are blocked",
        )


class CancelService:
    """Cancel active tasks with checkpoint retention and idempotent repeats."""

    def __init__(self, metadata: MetadataStore) -> None:
        self._metadata = metadata

    def cancel(
        self,
        task_id: str,
        *,
        reason: str = "user_requested",
        running_tokens: Iterable[CancellationToken] = (),
    ) -> CancelOutcome:
        """Cancel one task; repeating the call yields the same outcome."""
        state = self._metadata.task_state(task_id)
        if state is None:
            raise DomainViolation(_UNKNOWN_TASK, f"task {task_id} is not stored")
        if state in TERMINAL_TASK_STATES:
            raise DomainViolation(_TERMINAL, f"task {task_id} is deleted; nothing can be cancelled")
        already_cancelled = state == "cancelled"
        if not already_cancelled and state in SETTLED_TASK_STATES:
            # a settled task keeps its terminal state unchanged (原终态不变)
            raise DomainViolation(
                _ALREADY_SETTLED,
                f"task {task_id} is already {state}; use deletion instead",
            )

        already_cancelled = state == "cancelled"
        if not already_cancelled:
            if state not in CANCELLABLE_TASK_STATES:
                raise DomainViolation(_ALREADY_SETTLED, f"task {task_id} in {state} cannot cancel")
            target = cancel_task(state)
            self._metadata.record_transition(
                task_id,
                current=state,
                target=target,
                reason=reason,
                at=datetime.now(UTC),
            )
            state = target
        else:
            state = "cancelled"

        # stop in-flight derived work at its next check point
        for token in running_tokens:
            token.cancel()

        retained = tuple(c.name for c in self._metadata.checkpoints(task_id))
        discarded = self._metadata.incomplete_stages(task_id)
        notes = (
            "complete checkpoints retained; incomplete stages discarded, not published",
            "nothing was deleted; removal is a separate explicit flow",
        )
        return CancelOutcome(
            task_id=task_id,
            state=state,
            already_cancelled=already_cancelled,
            retained_checkpoints=retained,
            discarded_stages=discarded,
            derivative_writes_blocked=True,
            data_removed=False,
            notes=notes,
        )
