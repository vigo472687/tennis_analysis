"""State-domain tests for transitions, cancellation and terminal guard (BE-005).

The cases cover the four separated state domains (runtime, task, report,
feedback): legal and illegal moves, rejection paths, cancellation including the
repeated-cancellation rule, terminal protection, checkpoint bounds, stage
consistency against the BE-004 task entity, report visibility and the boundary
that keeps feedback, routing, report content and teaching prose outside the
backend. Samples are state names only.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tennis_backend.domain.models import DomainViolation, Task, TaskVersion
from tennis_backend.domain.states import (
    BACKEND_OWNED_DOMAINS,
    CANCELLABLE_TASK_STATES,
    FEEDBACK_TRANSITIONS,
    FOREIGN_DOMAINS,
    MAX_CHECKPOINTS,
    PIPELINE_STATES,
    PROTOTYPE_UNREACHABLE_FEEDBACK_STATES,
    RUNTIME_TRANSITIONS,
    SETTLED_TASK_STATES,
    TASK_STAGES,
    TASK_TRANSITIONS,
    TERMINAL_TASK_STATES,
    IllegalTransition,
    assert_backend_owned,
    assert_checkpoints_allowed,
    assert_runtime_transition,
    assert_state_stage_consistent,
    assert_task_transition,
    cancel_task,
    report_visibility,
)

STATES_MODULE = (
    Path(__file__).resolve().parents[1] / "src" / "tennis_backend" / "domain" / "states.py"
)

ALL_TASK_STATES = frozenset(TASK_TRANSITIONS)


def move(current: str, target: str) -> None:
    assert_task_transition(current, target)  # type: ignore[arg-type]


def runtime_move(current: str, target: str) -> None:
    assert_runtime_transition(current, target)  # type: ignore[arg-type]


def task_version() -> TaskVersion:
    return TaskVersion(
        task_id="task-1",
        version=1,
        primary_route="technical_diagnosis",
        auxiliary_routes=["question_answer"],
        runtime_requirement="required",
        contract_version="2.0.0",
        config_version="fixture-config-1",
        media_id="media-1",
        consent_id="consent-1",
        simulation=True,
    )


def task(state: str, stage: str) -> Task:
    return Task(
        task_id="task-1",
        versions=[task_version()],
        state=state,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        simulation=True,
    )


# ---------------------------------------------------------------------------
# Task pipeline: legal progression and skipped-stage counterexamples
# ---------------------------------------------------------------------------


def test_full_pipeline_progresses_stage_by_stage() -> None:
    path = ["draft", "checking_input", "analyzing", "researching", "rendering", "complete"]
    for current, target in zip(path, path[1:], strict=False):
        move(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("draft", "analyzing"),
        ("draft", "complete"),
        ("checking_input", "rendering"),
        ("checking_input", "complete"),
        ("analyzing", "complete"),
        ("researching", "complete"),
        ("waiting_for_capability", "analyzing"),
        ("rendering", "analyzing"),
    ],
)
def test_stage_skipping_is_rejected(current: str, target: str) -> None:
    with pytest.raises(IllegalTransition):
        move(current, target)


def test_partial_report_is_reached_from_rendering_only() -> None:
    move("rendering", "partial")
    with pytest.raises(IllegalTransition):
        move("analyzing", "partial")


def test_quality_and_support_verdicts_come_from_media_or_analysis() -> None:
    for source in ("checking_input", "analyzing"):
        for verdict in ("reshoot_required", "unsupported"):
            move(source, verdict)
    for blocked in ("researching", "rendering", "complete"):
        with pytest.raises(IllegalTransition):
            move(blocked, "reshoot_required")
        with pytest.raises(IllegalTransition):
            move(blocked, "unsupported")


def test_safety_stop_is_reachable_while_working_but_not_when_settled() -> None:
    for source in ("checking_input", "analyzing"):
        move(source, "safety_stop")
    for blocked in ("complete", "partial", "cancelled", "draft", "rendering"):
        with pytest.raises(IllegalTransition):
            move(blocked, "safety_stop")


def test_failed_task_retries_a_stage_but_never_jumps_to_success() -> None:
    for stage in PIPELINE_STATES:
        move("failed", stage)
    for blocked in ("complete", "partial", "cancelled", "safety_stop"):
        with pytest.raises(IllegalTransition):
            move("failed", blocked)


# ---------------------------------------------------------------------------
# Cancellation: where it is allowed, idempotence, and no data loss
# ---------------------------------------------------------------------------


def test_cancellation_is_allowed_exactly_while_work_is_active() -> None:
    assert CANCELLABLE_TASK_STATES == {
        "draft",
        "waiting_for_capability",
        "checking_input",
        "analyzing",
        "researching",
        "rendering",
    }
    for state in sorted(CANCELLABLE_TASK_STATES):
        assert cancel_task(state) == "cancelled"
        move(state, "cancelled")


def test_repeated_cancellation_returns_the_same_result() -> None:
    assert cancel_task("analyzing") == "cancelled"
    assert cancel_task("cancelled") == "cancelled"


def test_settled_tasks_cannot_be_cancelled_except_by_repetition() -> None:
    for state in sorted(SETTLED_TASK_STATES - {"cancelled"}):
        with pytest.raises(IllegalTransition):
            cancel_task(state)
    assert cancel_task("cancelled") == "cancelled"


def test_cancellation_keeps_recorded_checkpoints_instead_of_deleting_them() -> None:
    recorded = ["checking_input", "analyzing"]
    assert_checkpoints_allowed("cancelled", recorded)
    move("cancelled", "deleted")
    with pytest.raises(DomainViolation):
        assert_checkpoints_allowed("deleted", recorded)


# ---------------------------------------------------------------------------
# Terminal protection
# ---------------------------------------------------------------------------


def test_settled_states_allow_only_local_deletion() -> None:
    assert SETTLED_TASK_STATES == {
        "complete",
        "partial",
        "reshoot_required",
        "unsupported",
        "safety_stop",
        "cancelled",
    }
    for state in sorted(SETTLED_TASK_STATES):
        assert TASK_TRANSITIONS[state] == {"deleted"}
        for target in sorted(ALL_TASK_STATES - {"deleted"}):
            with pytest.raises(IllegalTransition):
                move(state, target)


def test_deleted_is_absolutely_terminal() -> None:
    assert TERMINAL_TASK_STATES == {"deleted"}
    assert TASK_TRANSITIONS["deleted"] == frozenset()
    for target in sorted(ALL_TASK_STATES):
        with pytest.raises(IllegalTransition):
            move("deleted", target)
    with pytest.raises(DomainViolation):
        cancel_task("deleted")


def test_unknown_states_are_rejected_for_both_domains() -> None:
    with pytest.raises(DomainViolation):
        move("archived", "deleted")
    with pytest.raises(DomainViolation):
        runtime_move("hibernating", "ready")


# ---------------------------------------------------------------------------
# Checkpoints and stage consistency
# ---------------------------------------------------------------------------


def test_checkpoint_bounds_grow_with_completed_stages() -> None:
    assert MAX_CHECKPOINTS["checking_input"] == frozenset()
    assert MAX_CHECKPOINTS["analyzing"] == {"checking_input"}
    assert MAX_CHECKPOINTS["researching"] == {"checking_input", "analyzing"}
    assert MAX_CHECKPOINTS["rendering"] == {"checking_input", "analyzing", "researching"}
    assert MAX_CHECKPOINTS["complete"] == set(PIPELINE_STATES)
    assert MAX_CHECKPOINTS["deleted"] == frozenset()


def test_current_stage_has_no_checkpoint_yet() -> None:
    with pytest.raises(DomainViolation):
        assert_checkpoints_allowed("analyzing", ["checking_input", "analyzing"])
    assert_checkpoints_allowed("analyzing", ["checking_input"])


def test_stage_follows_state_for_every_task_state() -> None:
    assert TASK_STAGES["checking_input"] == "media"
    assert TASK_STAGES["deleted"] == "delete"
    for state, stage in TASK_STAGES.items():
        assert_state_stage_consistent(state, stage)


def test_a_task_entity_carries_the_stage_its_state_implies() -> None:
    complete = task("complete", "none")
    assert_state_stage_consistent(complete.state, complete.stage)
    analyzing = task("analyzing", "analysis")
    assert_state_stage_consistent(analyzing.state, analyzing.stage)


def test_state_and_stage_disagreement_is_rejected() -> None:
    with pytest.raises(DomainViolation):
        assert_state_stage_consistent("complete", "analyzing")
    with pytest.raises(DomainViolation):
        assert_state_stage_consistent("deleted", "none")


# ---------------------------------------------------------------------------
# Runtime domain: preparation, rejection, repair and uninstall
# ---------------------------------------------------------------------------


def test_capability_prepare_verify_ready_path() -> None:
    path = ["not_initialized", "awaiting_consent", "preparing", "verifying", "ready"]
    for current, target in zip(path, path[1:], strict=False):
        runtime_move(current, target)


def test_refusing_preparation_returns_to_the_previous_state() -> None:
    runtime_move("awaiting_consent", "not_initialized")
    runtime_move("awaiting_consent", "installed_stopped")


def test_cancelled_preparation_and_starting_fall_back_without_executing() -> None:
    runtime_move("preparing", "not_initialized")
    runtime_move("verifying", "installed_stopped")
    runtime_move("starting", "installed_stopped")


def test_failures_repair_through_verification_again() -> None:
    for broken in ("preparing", "verifying", "starting", "ready", "upgrading"):
        runtime_move(broken, "repair_required")
    runtime_move("repair_required", "verifying")
    runtime_move("uninstalling", "repair_required")
    with pytest.raises(IllegalTransition):
        runtime_move("repair_required", "ready")


def test_partially_failed_uninstall_lands_in_repair_not_ready() -> None:
    runtime_move("ready", "uninstalling")
    with pytest.raises(IllegalTransition):
        runtime_move("uninstalling", "ready")
    runtime_move("uninstalling", "not_initialized")


def test_capability_cannot_be_used_without_confirmation() -> None:
    with pytest.raises(IllegalTransition):
        runtime_move("not_initialized", "ready")
    with pytest.raises(IllegalTransition):
        runtime_move("not_initialized", "preparing")


def test_stopped_capability_starts_and_stops_again() -> None:
    runtime_move("installed_stopped", "starting")
    runtime_move("installed_stopped", "uninstalling")
    with pytest.raises(IllegalTransition):
        runtime_move("installed_stopped", "verifying")


# ---------------------------------------------------------------------------
# Report visibility and the not-backend-owned boundary
# ---------------------------------------------------------------------------


def test_full_report_requires_a_complete_task() -> None:
    assert report_visibility("complete") == "full"
    assert report_visibility("partial") == "limited"
    full = {state for state in ALL_TASK_STATES if report_visibility(state) == "full"}
    limited = {state for state in ALL_TASK_STATES if report_visibility(state) == "limited"}
    assert full == {"complete"}
    assert limited == {"partial"}


def test_limited_and_unavailable_states_never_pass_as_full() -> None:
    for state in sorted(ALL_TASK_STATES - {"complete"}):
        assert report_visibility(state) != "full"


def test_backend_owns_only_runtime_and_task() -> None:
    assert BACKEND_OWNED_DOMAINS == {"runtime", "task"}
    assert BACKEND_OWNED_DOMAINS & FOREIGN_DOMAINS == frozenset()
    for domain in sorted(BACKEND_OWNED_DOMAINS):
        assert_backend_owned(domain)
    for domain in sorted(FOREIGN_DOMAINS):
        with pytest.raises(DomainViolation):
            assert_backend_owned(domain)


def test_feedback_sending_is_described_but_never_driven_by_the_backend() -> None:
    assert FEEDBACK_TRANSITIONS["ready_to_send"] == {"sending", "draft", "withdrawn"}
    assert FEEDBACK_TRANSITIONS["sending"] == {
        "send_failed",
        "unknown_result",
        "simulated_sent",
        "withdrawn",
    }
    assert PROTOTYPE_UNREACHABLE_FEEDBACK_STATES == {"sent"}
    with pytest.raises(DomainViolation):
        assert_backend_owned("feedback")
    with pytest.raises(IllegalTransition):
        move("analyzing", "sending")


# ---------------------------------------------------------------------------
# Domain separation and module decoupling
# ---------------------------------------------------------------------------


def test_state_vocabularies_stay_in_separate_maps_per_domain() -> None:
    runtime = set(RUNTIME_TRANSITIONS)
    task = set(TASK_TRANSITIONS)
    feedback = set(FEEDBACK_TRANSITIONS)
    assert runtime.isdisjoint(task)
    assert runtime.isdisjoint(feedback)
    # spec §8.2 and §8.3 both name a "draft" state; they live in different
    # maps with different meanings and are never mixed into one value.
    assert task & feedback == {"draft"}


def test_states_module_imports_only_the_domain_models_and_stdlib() -> None:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(STATES_MODULE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)
    assert imported <= {"__future__", "collections.abc", "tennis_backend.domain.models", "typing"}
