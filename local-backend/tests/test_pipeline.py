"""Analysis pipeline tests: quality-first, checkpoints, partial gating (BE-017).

Runs against the real frozen fixture library with real SQLite stores. Covers
the AC-SPEC-010/011/013/014/015/019/045/046 behaviors this batch owns: fail
first, pause instead of guessing, disable only what the missing objects
actually gate, list reliable and undecidable items separately, checkpoint
reuse on resume, and settled-operation replay.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.analysis.fixture import FixtureAnalysisAdapter, FixtureLibrary
from tennis_backend.application.idempotency import OperationLedger
from tennis_backend.application.pipeline import AnalysisPipeline
from tennis_backend.application.tasks import TaskService
from tennis_backend.application.worker import CancellationToken, JobCancelled
from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.domain.models import DomainViolation, Grounded, UserInput
from tennis_backend.storage.metadata import MetadataStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "v1"
FIXTURE_ROOT = REPO_ROOT / "fixtures"
NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)


class CountingAdapter(FixtureAnalysisAdapter):
    """Counts gate invocations so reuse can be asserted honestly."""

    def __init__(self, library: FixtureLibrary) -> None:
        super().__init__(library)
        self.gate_calls = 0
        self.stage_calls = 0

    def quality_report(self, request: Any) -> dict[str, Any]:
        self.gate_calls += 1
        return super().quality_report(request)

    def run(self, stage: str, request: Any, *, cancel: CancellationToken) -> dict[str, Any]:
        self.stage_calls += 1
        return super().run(stage, request, cancel=cancel)


@pytest.fixture
def env(tmp_path: Path) -> tuple[AnalysisPipeline, TaskService, MetadataStore, CountingAdapter]:
    counter = {"n": 0}

    def factory() -> str:
        counter["n"] += 1
        return f"task-{counter['n']:04d}"

    service = TaskService(
        MetadataStore(tmp_path / "meta.sqlite"),
        OperationLedger(tmp_path / "ledger.sqlite"),
        task_id_factory=factory,
    )
    metadata = service._metadata  # noqa: SLF001 - test wiring of the same store
    ledger = service._ledger  # noqa: SLF001
    adapter = CountingAdapter(FixtureLibrary.load(FIXTURE_ROOT))
    registry = ContractRegistry.load(CONTRACTS_DIR)
    pipeline = AnalysisPipeline(
        service=service, metadata=metadata, ledger=ledger, adapter=adapter, registry=registry
    )
    return pipeline, service, metadata, adapter


def create_task(service: TaskService, task_id_hint: str = "op") -> str:
    outcome = service.create_task(
        operation_id=f"op-create-{task_id_hint}",
        fixture_id=f"fixture-{task_id_hint}",
        user_input=UserInput(
            question=Grounded.observed("看看正手"),
            age_band=Grounded.observed("adult"),
            handedness=Grounded.observed("right"),
            target_stroke=Grounded.observed("forehand"),
            goal=Grounded.observed("稳定击球"),
            exercise_background=Grounded.observed("recreational"),
            safety=Grounded.observed("clear"),
        ),
        primary_route="technical_diagnosis",
        auxiliary_routes=["tactics"],
        at=NOW,
    )
    return outcome.task_id


def run_pipeline(
    pipeline: AnalysisPipeline,
    task_id: str,
    fixture_id: str,
    *,
    operation_id: str = "op-pipe-1",
    cancel: CancellationToken | None = None,
) -> Any:
    return pipeline.run(
        operation_id=operation_id,
        task_id=task_id,
        version=1,
        fixture_id=fixture_id,
        cancel=cancel or CancellationToken(),
        at=NOW,
    )


# ---------------------------------------------------------------------------
# Compatibility first
# ---------------------------------------------------------------------------


def test_supported_fixture_reaches_analysis_with_checkpoints(env: Any) -> None:
    pipeline, service, metadata, adapter = env
    task_id = create_task(service, "ok")
    outcome = run_pipeline(pipeline, task_id, "iphone-flat")
    assert outcome.status == "analyzed"
    assert outcome.task_state == "analyzing"
    assert "checking_input" in outcome.checkpoints
    assert set(outcome.stage_results) == {"strokes", "timing", "metrics"}
    assert outcome.stage_results["strokes"]["simulation"] is True
    assert outcome.disabled == ()
    assert "stroke_window" in outcome.reliable
    summary = service.task_summary(task_id)
    assert summary["state"] == "analyzing"
    checkpoints = [c.name for c in metadata.checkpoints(task_id)]
    assert checkpoints == ["checking_input"]


@pytest.mark.parametrize(
    "fixture_id,expected_state,note_code",
    [
        ("overlong", "unsupported", "duration_exceeded"),
        ("damaged", "unsupported", "unreadable_media"),
        ("native-panorama", "unsupported", "panorama_requires_flat_export"),
        ("unknown-action", "unsupported", "unsupported_stroke"),
        ("bad-time-mapping", "reshoot_required", "time_mapping_invalid"),
        ("small-person", "reshoot_required", "subject_too_small"),
        ("missing-body", "reshoot_required", "body_out_of_frame"),
    ],
)
def test_failed_quality_blocks_before_any_analysis(
    env: Any, fixture_id: str, expected_state: str, note_code: str
) -> None:
    pipeline, service, _, adapter = env
    task_id = create_task(service, fixture_id)
    outcome = run_pipeline(pipeline, task_id, fixture_id)
    assert outcome.status == "blocked"
    assert outcome.task_state == expected_state
    assert note_code in outcome.notes
    assert outcome.stage_results == {}
    assert adapter.stage_calls == 0  # nothing was analysed
    assert service.task_summary(task_id)["state"] == expected_state


# ---------------------------------------------------------------------------
# Pause instead of guessing
# ---------------------------------------------------------------------------


def test_multiple_subjects_pause_until_selection(env: Any) -> None:
    pipeline, service, _, _ = env
    task_id = create_task(service, "multi")
    outcome = run_pipeline(pipeline, task_id, "multiple-subjects")
    assert outcome.status == "paused"
    assert outcome.task_state == "checking_input"
    assert outcome.pending is not None
    assert outcome.pending["type"] == "subject_selection"
    assert sorted(outcome.pending["candidates"]) == ["person-1", "person-2"]
    assert outcome.stage_results == {}  # no individual conclusions yet


def test_mirror_conflict_pauses_until_direction_confirmed(env: Any) -> None:
    pipeline, service, _, _ = env
    task_id = create_task(service, "mirror")
    outcome = run_pipeline(pipeline, task_id, "mirror-conflict")
    assert outcome.status == "paused"
    assert outcome.pending is not None
    assert outcome.pending["type"] == "direction_confirmation"
    assert service.task_summary(task_id)["state"] == "checking_input"


# ---------------------------------------------------------------------------
# Partial analysis: object-specific gating (AC-SPEC-013/014/015)
# ---------------------------------------------------------------------------


def test_missing_feet_disables_footwork_and_support(env: Any) -> None:
    pipeline, service, _, _ = env
    task_id = create_task(service, "feet")
    outcome = run_pipeline(pipeline, task_id, "missing-feet")
    assert outcome.status == "analyzed"
    disabled_capabilities = {item["capability"] for item in outcome.disabled}
    assert "footwork_evaluation" in disabled_capabilities
    assert "support_and_leg_drive" in disabled_capabilities
    assert all(item["reason"] == "feet_not_visible" for item in outcome.disabled)
    # the adapter's timing truth flows through: recovery (requires feet) is undecidable
    assert "timing:recovery" in outcome.undecidable
    assert "timing:preparation" in outcome.reliable
    assert outcome.stage_results["timing"]["simulation"] is True


def test_missing_ball_disables_ball_judgments(env: Any) -> None:
    pipeline, service, _, _ = env
    task_id = create_task(service, "ball")
    outcome = run_pipeline(pipeline, task_id, "missing-ball")
    disabled_capabilities = {item["capability"] for item in outcome.disabled}
    assert {
        "bounce_and_landing",
        "incoming_ball_timing",
        "precise_contact_moment",
    } <= disabled_capabilities
    assert "stroke_window" in outcome.reliable  # body observation stays reliable


def test_missing_racket_disables_racket_head(env: Any) -> None:
    pipeline, service, _, _ = env
    task_id = create_task(service, "racket")
    outcome = run_pipeline(pipeline, task_id, "missing-racket")
    disabled_capabilities = {item["capability"] for item in outcome.disabled}
    assert {"racket_head_path", "racket_drop"} <= disabled_capabilities


def test_partial_analysis_lists_reliable_and_undecidable_separately(env: Any) -> None:
    pipeline, service, _, _ = env
    task_id = create_task(service, "partial")
    outcome = run_pipeline(pipeline, task_id, "missing-ball")
    assert outcome.reliable
    assert outcome.undecidable
    assert not (set(outcome.reliable) & set(outcome.undecidable))


# ---------------------------------------------------------------------------
# Replay and checkpoint reuse (AC-SPEC-045)
# ---------------------------------------------------------------------------


def test_settled_operation_replays_without_rework(env: Any) -> None:
    pipeline, service, _, adapter = env
    task_id = create_task(service, "replay")
    first = run_pipeline(pipeline, task_id, "iphone-flat", operation_id="op-pipe-r")
    calls_after_first = adapter.stage_calls
    second = run_pipeline(pipeline, task_id, "iphone-flat", operation_id="op-pipe-r")
    assert second.to_payload() == first.to_payload()
    assert adapter.stage_calls == calls_after_first  # zero re-execution


def test_resume_skips_re_running_the_quality_gate(env: Any) -> None:
    pipeline, service, metadata, adapter = env
    task_id = create_task(service, "resume")
    first = run_pipeline(pipeline, task_id, "iphone-flat", operation_id="op-pipe-1")
    checkpoints_after_first = [c.name for c in metadata.checkpoints(task_id)]
    # a new operation on the same task: the durable gate result is reused
    second = run_pipeline(pipeline, task_id, "iphone-flat", operation_id="op-pipe-2")
    assert second.status == "analyzed"
    assert [c.name for c in metadata.checkpoints(task_id)] == checkpoints_after_first
    assert second.checkpoints == first.checkpoints


# ---------------------------------------------------------------------------
# Refusals and cancellation
# ---------------------------------------------------------------------------


def test_pipeline_refuses_settled_task(env: Any) -> None:
    pipeline, service, _, _ = env
    task_id = create_task(service, "settled")
    run_pipeline(pipeline, task_id, "overlong")  # ends unsupported
    # same inputs under a new operation id replay (EX-005); genuinely new
    # inputs against a settled task are refused by the state check
    replay = run_pipeline(pipeline, task_id, "overlong", operation_id="op-pipe-x")
    assert replay.status == "blocked"  # honest replay of the blocked outcome
    with pytest.raises(DomainViolation) as error:
        run_pipeline(pipeline, task_id, "iphone-flat", operation_id="op-pipe-y")
    assert error.value.code == "DOMAIN_PIPELINE_STATE"


def test_cancelled_token_stops_between_stages(env: Any) -> None:
    pipeline, service, _, _ = env
    task_id = create_task(service, "cancel")
    token = CancellationToken()
    token.cancel()
    with pytest.raises(JobCancelled):
        run_pipeline(pipeline, task_id, "iphone-flat", cancel=token)
