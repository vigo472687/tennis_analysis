"""Cancellation with checkpoint retention tests (BE-022).

Runs against real SQLite stores and the real fixture pipeline. Covers:
cancel of an active task with checkpoints retained and incomplete stages
discarded, idempotent repeats (no duplicate events), settled/terminal
refusals leaving the original state untouched, derived-write blocking,
worker-token stopping, and the no-auto-delete guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.analysis.fixture import FixtureAnalysisAdapter, FixtureLibrary
from tennis_backend.application.cancel import (
    CancelService,
    assert_derivative_writes_allowed,
)
from tennis_backend.application.idempotency import OperationLedger
from tennis_backend.application.pipeline import AnalysisPipeline
from tennis_backend.application.tasks import TaskService
from tennis_backend.application.worker import CancellationToken
from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.domain.models import DomainViolation, Grounded, UserInput
from tennis_backend.storage.metadata import MetadataStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "v1"
FIXTURE_ROOT = REPO_ROOT / "fixtures"
NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    counter = {"n": 0}

    def factory() -> str:
        counter["n"] += 1
        return f"task-{counter['n']:04d}"

    metadata = MetadataStore(tmp_path / "meta.sqlite")
    ledger = OperationLedger(tmp_path / "ledger.sqlite")
    service = TaskService(metadata, ledger, task_id_factory=factory)
    registry = ContractRegistry.load(CONTRACTS_DIR)
    adapter = FixtureAnalysisAdapter(FixtureLibrary.load(FIXTURE_ROOT))
    pipeline = AnalysisPipeline(
        service=service, metadata=metadata, ledger=ledger, adapter=adapter, registry=registry
    )
    cancel = CancelService(metadata)
    return {
        "metadata": metadata,
        "service": service,
        "pipeline": pipeline,
        "cancel": cancel,
        "counter": counter,
    }


def create_task(env: dict[str, Any], hint: str) -> str:
    outcome = env["service"].create_task(
        operation_id=f"op-create-{hint}",
        fixture_id=f"fixture-{hint}",
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


# ---------------------------------------------------------------------------
# Active cancel: checkpoints retained, incomplete stages discarded
# ---------------------------------------------------------------------------


def test_cancel_active_task_keeps_checkpoints_and_discards_incomplete(env: dict[str, Any]) -> None:
    pipeline, metadata, cancel = env["pipeline"], env["metadata"], env["cancel"]
    task_id = create_task(env, "active")
    pipeline.run(
        operation_id="op-pipe-1",
        task_id=task_id,
        version=1,
        fixture_id="iphone-flat",
        cancel=CancellationToken(),
        at=NOW,
    )
    outcome = cancel.cancel(task_id)
    assert outcome.state == "cancelled"
    assert outcome.already_cancelled is False
    assert outcome.retained_checkpoints == ("checking_input",)  # complete stage kept
    assert set(outcome.discarded_stages) == {"analyzing", "researching", "rendering"}
    assert outcome.derivative_writes_blocked is True
    assert outcome.data_removed is False
    stored = [c.name for c in metadata.checkpoints(task_id)]
    assert stored == ["checking_input"]  # durable, still readable
    assert metadata.task_state(task_id) == "cancelled"


def test_repeated_cancel_is_stable_without_duplicate_events(env: dict[str, Any]) -> None:
    cancel, metadata = env["cancel"], env["metadata"]
    task_id = create_task(env, "repeat")
    first = cancel.cancel(task_id)
    events_after_first = len(metadata.state_events(task_id))
    second = cancel.cancel(task_id)
    assert second.already_cancelled is True
    assert second.state == first.state
    assert second.retained_checkpoints == first.retained_checkpoints
    assert second.discarded_stages == first.discarded_stages
    assert len(metadata.state_events(task_id)) == events_after_first  # no new transition
    assert second.as_payload()["state"] == "cancelled"


def test_cancel_from_draft_works(env: dict[str, Any]) -> None:
    cancel = env["cancel"]
    task_id = create_task(env, "draft")
    outcome = cancel.cancel(task_id)
    assert outcome.state == "cancelled"
    assert outcome.retained_checkpoints == ()


# ---------------------------------------------------------------------------
# Settled and terminal refusals: original state untouched
# ---------------------------------------------------------------------------


def test_settled_task_refuses_cancel_and_keeps_state(env: dict[str, Any]) -> None:
    pipeline, cancel, metadata = env["pipeline"], env["cancel"], env["metadata"]
    task_id = create_task(env, "settled")
    pipeline.run(
        operation_id="op-pipe-1",
        task_id=task_id,
        version=1,
        fixture_id="overlong",
        cancel=CancellationToken(),
        at=NOW,
    )
    assert env["metadata"].task_state(task_id) == "unsupported"
    with pytest.raises(DomainViolation) as error:
        cancel.cancel(task_id)
    assert error.value.code == "DOMAIN_CANCEL_NOT_CANCELLABLE"
    assert metadata.task_state(task_id) == "unsupported"  # 原终态不变


def test_terminal_deleted_task_refuses_cancel(env: dict[str, Any]) -> None:
    cancel, metadata = env["cancel"], env["metadata"]
    task_id = create_task(env, "deleted")
    cancel.cancel(task_id)
    # move to the absolute terminal state the way the deletion flow will
    metadata.record_transition(
        task_id, current="cancelled", target="deleted", reason="deleted", at=NOW
    )
    with pytest.raises(DomainViolation) as error:
        cancel.cancel(task_id)
    assert error.value.code == "DOMAIN_CANCEL_TERMINAL"
    assert metadata.task_state(task_id) == "deleted"


def test_unknown_task_refused(env: dict[str, Any]) -> None:
    with pytest.raises(DomainViolation) as error:
        env["cancel"].cancel("task-nope")
    assert error.value.code == "DOMAIN_UNKNOWN_TASK"


# ---------------------------------------------------------------------------
# Derived writes stop; nothing is auto-deleted
# ---------------------------------------------------------------------------


def test_derivative_writes_blocked_after_cancel(env: dict[str, Any]) -> None:
    cancel, metadata = env["cancel"], env["metadata"]
    task_id = create_task(env, "writes")
    assert_derivative_writes_allowed(metadata, task_id)  # active: allowed
    cancel.cancel(task_id)
    with pytest.raises(DomainViolation) as error:
        assert_derivative_writes_allowed(metadata, task_id)
    assert error.value.code == "DOMAIN_CANCELLED_WRITES_BLOCKED"


def test_running_tokens_are_cancelled(env: dict[str, Any]) -> None:
    token = CancellationToken()
    env["cancel"].cancel(create_task(env, "tokens"), running_tokens=[token])
    assert token.cancelled is True


def test_cancel_does_not_delete_media(env: dict[str, Any]) -> None:
    from tennis_backend.domain.models import MediaAsset, MediaProbe, TimePoint

    metadata, cancel = env["metadata"], env["cancel"]
    task_id = create_task(env, "media")
    asset = MediaAsset(
        media_id="media-keep-1",
        role="original",
        original_ref="projects/x/v1/media/source.mp4",
        probe=MediaProbe(
            container="mp4",
            codec="h264",
            duration_s=1.0,
            fps=25.0,
            width=64,
            height=64,
            rotation="0",
            slow_motion="unknown",
            hdr="no",
            device_source="unknown",
            panorama="flat",
            observation_kind="measured",
        ),
        time_mapping=[TimePoint(normalized_s=0.0, original_s=0.0)],
        time_mapping_valid=False,
        simulation=True,
    )
    metadata.record_media_asset(asset)
    cancel.cancel(task_id)
    # cancel never deletes: the media row is still readable (不自动删除)
    assert metadata.media_asset("media-keep-1").media_id == "media-keep-1"
    outcome_payload = cancel.cancel(task_id).as_payload()
    assert outcome_payload["data_removed"] is False
