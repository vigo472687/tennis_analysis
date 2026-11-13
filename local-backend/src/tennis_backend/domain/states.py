"""Separated state domains, transitions and terminal protection (BE-005).

Four state domains stay apart (plan §10.2): ``runtime``, ``task``, ``report``
and ``feedback``. Each domain has its own vocabulary and its own transition
map, so no function mixes two domains into one value.

The backend owns exactly two of them: the local capability state and the task
state, including legal rejection, cancellation and terminal protection. Report
content, feedback delivery, routing and final teaching prose are decided on the
agent/Skill side, so this module carries their vocabularies read-only and
:func:`assert_backend_owned` rejects any attempt to drive them from here.

Transition sources: spec §8.1 (runtime), §8.2 (task), §8.3 (feedback), plus
FR-054 (report visibility follows the task state), FR-072 (repeated
cancellation returns the same result and never deletes data) and plan §10.2
(checkpoints are written only after a stage finished and validated).

Illegal moves raise :class:`IllegalTransition`; unknown states raise
:class:`~tennis_backend.domain.models.DomainViolation`. Messages contain state
names and domain names only, never raw media or credential material, and
nothing in this module logs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from tennis_backend.domain.models import DomainViolation, TaskStage, TaskState

#: The four state domains; each is validated and stored separately (plan §10.2).
StateDomain = Literal["runtime", "task", "report", "feedback"]

#: Domains this backend drives. Everything else stays read-only here.
BACKEND_OWNED_DOMAINS: frozenset[str] = frozenset({"runtime", "task"})

#: Vocabularies carried for reference but decided outside the backend.
FOREIGN_DOMAINS: frozenset[str] = frozenset(
    {"report_content", "feedback", "routing", "teaching", "external_delivery"}
)

# ---------------------------------------------------------------------------
# Runtime domain (spec §8.1)
# ---------------------------------------------------------------------------

RuntimeState = Literal[
    "not_initialized",
    "awaiting_consent",
    "preparing",
    "verifying",
    "installed_stopped",
    "starting",
    "ready",
    "repair_required",
    "upgrading",
    "uninstalling",
]

RUNTIME_TRANSITIONS: dict[str, frozenset[str]] = {
    "not_initialized": frozenset({"awaiting_consent"}),
    "awaiting_consent": frozenset(
        {"preparing", "upgrading", "uninstalling", "not_initialized", "installed_stopped"}
    ),
    "preparing": frozenset({"verifying", "repair_required", "not_initialized"}),
    "verifying": frozenset({"ready", "repair_required", "installed_stopped", "not_initialized"}),
    "installed_stopped": frozenset({"starting", "awaiting_consent", "uninstalling"}),
    "starting": frozenset({"ready", "repair_required", "installed_stopped"}),
    "ready": frozenset(
        {"installed_stopped", "repair_required", "awaiting_consent", "upgrading", "uninstalling"}
    ),
    "repair_required": frozenset({"verifying", "awaiting_consent", "not_initialized"}),
    "upgrading": frozenset({"ready", "installed_stopped", "repair_required", "not_initialized"}),
    "uninstalling": frozenset({"not_initialized", "repair_required"}),
}

# ---------------------------------------------------------------------------
# Task domain (spec §8.2) - the only place task transitions are defined
# ---------------------------------------------------------------------------

TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"draft", "waiting_for_capability", "checking_input", "cancelled"}),
    "waiting_for_capability": frozenset({"checking_input", "draft", "cancelled"}),
    "checking_input": frozenset(
        {
            "analyzing",
            "waiting_for_capability",
            "reshoot_required",
            "unsupported",
            "safety_stop",
            "failed",
            "cancelled",
        }
    ),
    "analyzing": frozenset(
        {"researching", "reshoot_required", "unsupported", "safety_stop", "failed", "cancelled"}
    ),
    "researching": frozenset({"rendering", "failed", "cancelled"}),
    "rendering": frozenset({"complete", "partial", "failed", "cancelled"}),
    "complete": frozenset({"deleted"}),
    "partial": frozenset({"deleted"}),
    "reshoot_required": frozenset({"deleted"}),
    "unsupported": frozenset({"deleted"}),
    "safety_stop": frozenset({"deleted"}),
    "failed": frozenset({"checking_input", "analyzing", "researching", "rendering", "deleted"}),
    "cancelled": frozenset({"deleted"}),
    "deleted": frozenset(),
}

#: Settled states: nothing left to run, only local deletion remains.
SETTLED_TASK_STATES: frozenset[TaskState] = frozenset(
    {"complete", "partial", "reshoot_required", "unsupported", "safety_stop", "cancelled"}
)

#: Absolutely terminal: deleted data is never revived (spec §8.2, EX-100).
TERMINAL_TASK_STATES: frozenset[TaskState] = frozenset({"deleted"})

#: States a still-running or resumable analysis can be cancelled from (FR-072).
CANCELLABLE_TASK_STATES: frozenset[TaskState] = frozenset(
    {"draft", "waiting_for_capability", "checking_input", "analyzing", "researching", "rendering"}
)

#: Pipeline order used for checkpoint and stage bookkeeping (plan §10.2).
PIPELINE_STATES: tuple[TaskState, ...] = (
    "checking_input",
    "analyzing",
    "researching",
    "rendering",
)

#: Checkpoints are named after the stage whose output they preserve.
CheckpointName = Literal["checking_input", "analyzing", "researching", "rendering"]

#: Upper bound of checkpoints a state can legitimately carry. The current
#: stage has no checkpoint yet (its output is not validated), and a deleted
#: task carries none because deleted data is not revived.
MAX_CHECKPOINTS: dict[str, frozenset[str]] = {
    "draft": frozenset(),
    "waiting_for_capability": frozenset(),
    "checking_input": frozenset(),
    "analyzing": frozenset({"checking_input"}),
    "researching": frozenset({"checking_input", "analyzing"}),
    "rendering": frozenset({"checking_input", "analyzing", "researching"}),
    "complete": frozenset(PIPELINE_STATES),
    "partial": frozenset(PIPELINE_STATES),
    "reshoot_required": frozenset(PIPELINE_STATES),
    "unsupported": frozenset(PIPELINE_STATES),
    "safety_stop": frozenset(PIPELINE_STATES),
    "failed": frozenset(PIPELINE_STATES),
    "cancelled": frozenset(PIPELINE_STATES),
    "deleted": frozenset(),
}

#: Stage shown while a task sits in a state; failures carry their stage in the
#: task record instead of guessing it here.
TASK_STAGES: dict[TaskState, TaskStage] = {
    "draft": "input",
    "waiting_for_capability": "input",
    "checking_input": "media",
    "analyzing": "analysis",
    "researching": "research",
    "rendering": "render",
    "complete": "none",
    "partial": "none",
    "reshoot_required": "none",
    "unsupported": "none",
    "safety_stop": "none",
    "failed": "none",
    "cancelled": "none",
    "deleted": "delete",
}

# ---------------------------------------------------------------------------
# Report and feedback vocabularies: carried read-only, decided elsewhere
# ---------------------------------------------------------------------------

# spec §8.2 and §8.3 both name a "draft" state: a task input draft and a
# feedback text draft. They live in separate maps, are never compared, and the
# four domains are stored separately (plan §10.2).

#: How much report a task state may show (FR-054): limited states never use
#: the full completion wording.
ReportVisibility = Literal["full", "limited", "unavailable"]

_FULL_REPORT_STATES: frozenset[TaskState] = frozenset({"complete"})
_LIMITED_REPORT_STATES: frozenset[TaskState] = frozenset({"partial"})

FeedbackState = Literal[
    "draft",
    "ready_to_send",
    "sending",
    "sent",
    "send_failed",
    "withdrawn",
    "unknown_result",
    "simulated_sent",
]

FEEDBACK_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"ready_to_send"}),
    "ready_to_send": frozenset({"sending", "draft", "withdrawn"}),
    "sending": frozenset({"send_failed", "unknown_result", "simulated_sent", "withdrawn"}),
    "sent": frozenset(),
    "send_failed": frozenset({"draft", "ready_to_send"}),
    "withdrawn": frozenset({"draft"}),
    "unknown_result": frozenset({"draft", "ready_to_send"}),
    "simulated_sent": frozenset({"draft"}),
}

#: ``sent`` needs a real receipt (spec §8.3); the prototype can never enter it,
#: and ``simulated_sent`` stays a distinct state instead of standing in for it.
PROTOTYPE_UNREACHABLE_FEEDBACK_STATES: frozenset[FeedbackState] = frozenset({"sent"})


# ---------------------------------------------------------------------------
# Violations and guards
# ---------------------------------------------------------------------------


class IllegalTransition(DomainViolation):
    """A move between two states of one domain that spec does not allow."""

    def __init__(self, domain: str, current: str, target: str) -> None:
        super().__init__(
            "ILLEGAL_TRANSITION",
            f"{domain} state {current} cannot move to {target}",
        )
        self.domain = domain
        self.current = current
        self.target = target


def _assert_move(
    domain: str, transitions: dict[str, frozenset[str]], current: str, target: str
) -> None:
    if current not in transitions:
        raise DomainViolation("UNKNOWN_STATE", f"unknown {domain} state {current}")
    if target not in transitions[current]:
        raise IllegalTransition(domain, current, target)


def assert_task_transition(current: TaskState, target: TaskState) -> None:
    """Reject any task move the task state table does not list."""
    _assert_move("task", TASK_TRANSITIONS, current, target)


def assert_runtime_transition(current: RuntimeState, target: RuntimeState) -> None:
    """Reject any local-capability move the runtime state table does not list."""
    _assert_move("runtime", RUNTIME_TRANSITIONS, current, target)


def cancel_task(current: TaskState) -> TaskState:
    """Return the state after a cancellation request (FR-072).

    Cancelling twice returns the same result, cancellation never deletes data
    (a cancelled task keeps its checkpoints and resources), and cancelling an
    already settled task is rejected instead of silently repeating it.
    """
    if current == "cancelled":
        return current
    if current == "deleted":
        raise DomainViolation("ALREADY_DELETED", "a deleted task cannot be cancelled again")
    if current not in CANCELLABLE_TASK_STATES:
        raise IllegalTransition("task", current, "cancelled")
    return "cancelled"


def assert_checkpoints_allowed(state: TaskState, recorded: Sequence[CheckpointName]) -> None:
    """Checkpoints must only cover stages the task actually passed (plan §10.2)."""
    if state not in MAX_CHECKPOINTS:
        raise DomainViolation("UNKNOWN_STATE", f"unknown task state {state}")
    allowed = MAX_CHECKPOINTS[state]
    excessive = sorted(set(recorded) - allowed)
    if excessive:
        raise DomainViolation(
            "CHECKPOINT_NOT_ALLOWED",
            f"state {state} cannot carry checkpoints {excessive}",
        )


def assert_state_stage_consistent(state: TaskState, stage: TaskStage) -> None:
    """The displayed stage must be the one the state implies."""
    if state not in TASK_STAGES:
        raise DomainViolation("UNKNOWN_STATE", f"unknown task state {state}")
    expected = TASK_STAGES[state]
    if stage != expected:
        raise DomainViolation(
            "STAGE_MISMATCH",
            f"task state {state} implies stage {expected}, not {stage}",
        )


def report_visibility(state: TaskState) -> ReportVisibility:
    """How much report the task state allows to be shown (FR-054)."""
    if state in _FULL_REPORT_STATES:
        return "full"
    if state in _LIMITED_REPORT_STATES:
        return "limited"
    return "unavailable"


def assert_backend_owned(domain: str) -> None:
    """Refuse to drive a domain that belongs to the agent/Skill side.

    Task and runtime transitions are the backend's job; report content,
    feedback delivery, routing and teaching prose are not, so a request to
    move them fails here instead of silently growing backend powers.
    """
    if domain not in BACKEND_OWNED_DOMAINS:
        raise DomainViolation(
            "DOMAIN_NOT_BACKEND_OWNED",
            f"domain {domain} is not owned by the backend",
        )


__all__ = [
    "BACKEND_OWNED_DOMAINS",
    "CANCELLABLE_TASK_STATES",
    "CheckpointName",
    "FEEDBACK_TRANSITIONS",
    "FOREIGN_DOMAINS",
    "FeedbackState",
    "IllegalTransition",
    "MAX_CHECKPOINTS",
    "PIPELINE_STATES",
    "PROTOTYPE_UNREACHABLE_FEEDBACK_STATES",
    "RUNTIME_TRANSITIONS",
    "ReportVisibility",
    "RuntimeState",
    "SETTLED_TASK_STATES",
    "StateDomain",
    "TASK_STAGES",
    "TASK_TRANSITIONS",
    "TERMINAL_TASK_STATES",
    "TaskStage",
    "TaskState",
    "assert_backend_owned",
    "assert_checkpoints_allowed",
    "assert_runtime_transition",
    "assert_state_stage_consistent",
    "assert_task_transition",
    "cancel_task",
    "report_visibility",
]
