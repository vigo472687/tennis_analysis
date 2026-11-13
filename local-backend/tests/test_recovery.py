"""Crash/timeout recovery planning tests (BE-023, AC-SPEC-008/045).

Runs against real SQLite stores, the real fixture pipeline and the frozen
fixture library. Covers: resume from durable checkpoints with stage actions,
input change voiding reuse (fingerprint mismatch), consent withdrawal
voiding reuse, settled/cancelled/deleted refusals, and the guarantee that a
re-render plan never re-runs completed media/analysis stages.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.analysis.fixture import FixtureAnalysisAdapter, FixtureLibrary
from tennis_backend.application.cancel import CancelService
from tennis_backend.application.idempotency import OperationLedger
from tennis_backend.application.pipeline import AnalysisPipeline
from tennis_backend.application.recovery import plan_recovery
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
    return {
        "metadata": metadata,
        "service": service,
        "pipeline": pipeline,
        "adapter": adapter,
        "registry": registry,
        "cancel": CancelService(metadata),
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


def run_pipeline_to_analyzing(env: dict[str, Any], task_id: str) -> None:
    env["pipeline"].run(
        operation_id=f"op-pipe-{task_id}",
        task_id=task_id,
        version=1,
        fixture_id="iphone-flat",
        cancel=CancellationToken(),
        at=NOW,
    )


# ---------------------------------------------------------------------------
# Resume from durable checkpoints
# ---------------------------------------------------------------------------


def test_crash_after_gate_resumes_from_checkpoints(env: dict[str, Any]) -> None:
    metadata, adapter, service = env["metadata"], env["adapter"], env["service"]
    task_id = create_task(env, "resume")
    run_pipeline_to_analyzing(env, task_id)
    plan = plan_recovery(
        metadata=metadata,
        service=service,
        adapter=adapter,
        task_id=task_id,
        version=1,
        fixture_id="iphone-flat",
    )
    assert plan.status == "resume"
    assert plan.reused_checkpoints == ("checking_input",)
    assert plan.stage_actions["media"] == "reuse"  # re-render never re-runs media
    assert "analysis" in plan.redo_stages or plan.stage_actions["analysis"] in ("reuse", "redo")
    assert plan.stage_actions["render"] == "redo"  # only unfinished work is redone
    assert plan.refusals == ()
    # durable checkpoints were not touched by planning
    assert [c.name for c in metadata.checkpoints(task_id)] == ["checking_input"]


def test_no_checkpoints_restart_cleanly(env: dict[str, Any]) -> None:
    metadata, adapter, service = env["metadata"], env["adapter"], env["service"]
    task_id = create_task(env, "fresh")  # draft: never reached the gate
    plan = plan_recovery(
        metadata=metadata,
        service=service,
        adapter=adapter,
        task_id=task_id,
        version=1,
        fixture_id="iphone-flat",
    )
    assert plan.status == "restart"
    assert plan.reused_checkpoints == ()
    assert set(plan.stage_actions.values()) == {"redo"}
    assert "no durable checkpoints" in plan.notes[0]


# ---------------------------------------------------------------------------
# Input change voids reuse (输入改变不能越权复用)
# ---------------------------------------------------------------------------


def test_input_change_voids_reuse(env: dict[str, Any]) -> None:
    metadata, adapter, service = env["metadata"], env["adapter"], env["service"]
    task_id = create_task(env, "changed")
    run_pipeline_to_analyzing(env, task_id)
    # the task now claims a different fixture: the stored gate belonged to
    # different inputs, so nothing may be reused
    plan = plan_recovery(
        metadata=metadata,
        service=service,
        adapter=adapter,
        task_id=task_id,
        version=1,
        fixture_id="missing-ball",
    )
    assert plan.status == "restart"
    assert plan.reused_checkpoints == ()
    assert plan.refusals == ("RECOVERY_INPUT_CHANGED",)
    assert all(action == "redo" for action in plan.stage_actions.values())
    assert "nothing is reused" in plan.notes[0]


# ---------------------------------------------------------------------------
# Authorization change voids reuse (授权改变不能越权复用)
# ---------------------------------------------------------------------------


def test_withdrawn_consent_refuses_recovery(env: dict[str, Any]) -> None:
    metadata, adapter, service, registry = (
        env["metadata"],
        env["adapter"],
        env["service"],
        env["registry"],
    )
    task_id = create_task(env, "consent")
    run_pipeline_to_analyzing(env, task_id)
    # the user withdrew the analysis purpose after the crash
    from tennis_backend.domain.models import ConsentSnapshot

    withdrawn = ConsentSnapshot(
        consent_id="consent-1",
        task_id=task_id,
        version=1,
        purposes=["save"],  # analysis withdrawn
        text_version="consent-text-1",
        confirmed=True,
        guardian_required=False,
        guardian_simulated=False,
        confirmed_at=NOW,
        recipient_digest=None,
        materials_digest=None,
    )
    plan = plan_recovery(
        metadata=metadata,
        service=service,
        adapter=adapter,
        task_id=task_id,
        version=1,
        fixture_id="iphone-flat",
        consent=withdrawn,
        registry=registry,
    )
    assert plan.status == "refused"
    assert plan.refusals == ("RECOVERY_NOT_AUTHORIZED",)
    assert plan.reused_checkpoints == ()
    assert any("re-confirm authorization" in note for note in plan.notes)


def test_valid_consent_allows_resume(env: dict[str, Any]) -> None:
    metadata, adapter, service, registry = (
        env["metadata"],
        env["adapter"],
        env["service"],
        env["registry"],
    )
    task_id = create_task(env, "consent-ok")
    run_pipeline_to_analyzing(env, task_id)
    from tennis_backend.domain.models import ConsentSnapshot

    consent = ConsentSnapshot(
        consent_id="consent-1",
        task_id=task_id,
        version=1,
        purposes=["analysis", "save"],
        text_version="consent-text-1",
        confirmed=True,
        guardian_required=False,
        guardian_simulated=False,
        confirmed_at=NOW,
        recipient_digest=None,
        materials_digest=None,
    )
    plan = plan_recovery(
        metadata=metadata,
        service=service,
        adapter=adapter,
        task_id=task_id,
        version=1,
        fixture_id="iphone-flat",
        consent=consent,
        registry=registry,
    )
    assert plan.status == "resume"
    assert plan.reused_checkpoints == ("checking_input",)


# ---------------------------------------------------------------------------
# No recovery for settled / cancelled / deleted
# ---------------------------------------------------------------------------


def test_settled_task_refuses_recovery(env: dict[str, Any]) -> None:
    metadata, adapter, service = env["metadata"], env["adapter"], env["service"]
    task_id = create_task(env, "settled")
    env["pipeline"].run(
        operation_id=f"op-pipe-{task_id}",
        task_id=task_id,
        version=1,
        fixture_id="overlong",
        cancel=CancellationToken(),
        at=NOW,
    )
    plan = plan_recovery(
        metadata=metadata,
        service=service,
        adapter=adapter,
        task_id=task_id,
        version=1,
        fixture_id="overlong",
    )
    assert plan.status == "refused"
    assert plan.refusals == ("task_settled",)
    assert any("results stand" in note for note in plan.notes)


def test_cancelled_task_refuses_recovery(env: dict[str, Any]) -> None:
    metadata, adapter, service, cancel = (
        env["metadata"],
        env["adapter"],
        env["service"],
        env["cancel"],
    )
    task_id = create_task(env, "cancelled")
    cancel.cancel(task_id)
    plan = plan_recovery(
        metadata=metadata,
        service=service,
        adapter=adapter,
        task_id=task_id,
        version=1,
        fixture_id="iphone-flat",
    )
    assert plan.status == "refused"
    assert plan.refusals == ("task_cancelled",)
    assert any("stays cancelled" in note for note in plan.notes)


def test_deleted_task_refuses_recovery(env: dict[str, Any]) -> None:
    metadata, adapter, service, cancel = (
        env["metadata"],
        env["adapter"],
        env["service"],
        env["cancel"],
    )
    task_id = create_task(env, "deleted")
    cancel.cancel(task_id)
    env["metadata"].record_transition(
        task_id, current="cancelled", target="deleted", reason="deleted", at=NOW
    )
    plan = plan_recovery(
        metadata=metadata,
        service=service,
        adapter=adapter,
        task_id=task_id,
        version=1,
        fixture_id="iphone-flat",
    )
    assert plan.status == "refused"
    assert plan.refusals == ("task_deleted",)
    assert any("never revived" in note for note in plan.notes)


def test_quality_identical_fixture_switch_voids_reuse(env: dict[str, Any]) -> None:
    """BE-023 review blocking finding: iphone-flat and gopro-flat share one
    quality report, so a fixture-bound digest is required to catch the
    switch — the quality payload alone collides."""
    metadata, adapter, service = env["metadata"], env["adapter"], env["service"]
    task_id = create_task(env, "collision")
    run_pipeline_to_analyzing(env, task_id)
    plan = plan_recovery(
        metadata=metadata,
        service=service,
        adapter=adapter,
        task_id=task_id,
        version=1,
        fixture_id="gopro-flat",
    )
    assert plan.status == "restart"
    assert plan.reused_checkpoints == ()
    assert plan.refusals == ("RECOVERY_INPUT_CHANGED",)
    assert all(action == "redo" for action in plan.stage_actions.values())


def test_unknown_task_raises(env: dict[str, Any]) -> None:
    metadata, adapter, service = env["metadata"], env["adapter"], env["service"]
    with pytest.raises(DomainViolation) as error:
        plan_recovery(
            metadata=metadata,
            service=service,
            adapter=adapter,
            task_id="task-nope",
            version=1,
            fixture_id="iphone-flat",
        )
    assert error.value.code == "DOMAIN_UNKNOWN_TASK"
