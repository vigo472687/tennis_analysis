"""Stop-then-delete with per-item results (BE-025, AC-SPEC-039/040/042).

Real stores: resources are published into a real workspace (including an
input copy of a user original outside the root), a real pipeline run leaves
the task analyzing, and deletion must stop writes, settle the task to the
absolute terminal state, report every item truthfully, keep user originals
and foreign workspaces untouched, replay idempotently, and support retry of
failed items.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.analysis.fixture import FixtureAnalysisAdapter, FixtureLibrary
from tennis_backend.application.cancel import CancelService
from tennis_backend.application.delete import DeleteService
from tennis_backend.application.idempotency import OperationLedger
from tennis_backend.application.pipeline import AnalysisPipeline
from tennis_backend.application.tasks import TaskService
from tennis_backend.application.worker import CancellationToken
from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.domain.models import DomainViolation, Grounded, UserInput
from tennis_backend.storage.metadata import MetadataStore
from tennis_backend.storage.resources import ResourceStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "v1"
FIXTURE_ROOT = REPO_ROOT / "fixtures"
NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def registry() -> ContractRegistry:
    return ContractRegistry.load(CONTRACTS_DIR)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    counter = {"n": 0}

    def factory() -> str:
        counter["n"] += 1
        return f"task-{counter['n']:04d}"

    metadata = MetadataStore(tmp_path / "meta.sqlite")
    ledger = OperationLedger(tmp_path / "ledger.sqlite")
    store = ResourceStore(tmp_path / "public", tmp_path / "internal")
    service = TaskService(metadata, ledger, task_id_factory=factory)
    registry = ContractRegistry.load(CONTRACTS_DIR)
    adapter = FixtureAnalysisAdapter(FixtureLibrary.load(FIXTURE_ROOT))
    pipeline = AnalysisPipeline(
        service=service, metadata=metadata, ledger=ledger, adapter=adapter, registry=registry
    )
    cancel = CancelService(metadata)
    delete = DeleteService(metadata=metadata, ledger=ledger, resources=store, cancel=cancel)
    outside = tmp_path / "outside"
    outside.mkdir()
    return {
        "metadata": metadata,
        "service": service,
        "pipeline": pipeline,
        "store": store,
        "delete": delete,
        "outside": outside,
        "tmp": tmp_path,
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


def publish_resources(env: dict[str, Any], task_id: str, original: Path) -> None:
    store = env["store"]
    store.publish_bytes(
        task_id,
        1,
        f"{store.workspace_ref(task_id, 1)}/evidence/ev-1.png",
        b"png-bytes-1",
        kind="evidence_frame",
        mime="image/png",
    )
    store.publish_bytes(
        task_id,
        1,
        f"{store.workspace_ref(task_id, 1)}/evidence/ev-2.png",
        b"png-bytes-2",
        kind="evidence_frame",
        mime="image/png",
    )
    store.publish_input_copy(task_id, 1, original, mime="video/mp4")


# ---------------------------------------------------------------------------
# Happy path: stop, settle to deleted, per-item truth
# ---------------------------------------------------------------------------


def test_delete_stops_settles_and_reports_every_item(
    env: dict[str, Any], registry: ContractRegistry
) -> None:
    task_id = create_task(env, "happy")
    original = env["outside"] / "orig.mp4"
    original.write_bytes(b"original-video-bytes")
    publish_resources(env, task_id, original)
    run_pipeline_to_analyzing(env, task_id)

    result = env["delete"].delete_task(
        operation_id="op-del-1", task_id=task_id, version=1, confirmed=True, at=NOW
    )
    assert env["metadata"].task_state(task_id) == "deleted"  # absolute terminal
    statuses = {item["resource_id"]: item["status"] for item in result["items"]}
    assert set(statuses.values()) == {"deleted"}
    assert result["complete"] is True
    assert result["external_copies_uncontrolled"] is True
    assert result["runtime_uninstalled"] is False
    # workspace files are gone
    workspace = env["store"].workspace_path(task_id, 1)
    remaining = [p for p in workspace.rglob("*") if p.is_file()]
    assert remaining == []
    # the user's original outside the root is untouched (不误删)
    assert original.read_bytes() == b"original-video-bytes"
    # the frozen contract holds as returned
    assert registry.validate(registry.definition_ref("deletion", "delete_result"), result) == ()


def test_foreign_workspace_and_missing_files_reported_truthfully(env: dict[str, Any]) -> None:
    task_id = create_task(env, "missing")
    original = env["outside"] / "orig2.mp4"
    original.write_bytes(b"x")
    publish_resources(env, task_id, original)
    store = env["store"]
    # vanish one file before deletion; keep a foreign workspace untouched
    vanished = store.resource_path(
        [r for r in store.resources(task_id) if r.kind == "evidence_frame"][0]
    )
    vanished.unlink()
    foreign = env["tmp"] / "public" / "projects" / "task-other" / "v1" / "keep.png"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"keep-me")

    result = env["delete"].delete_task(
        operation_id="op-del-2", task_id=task_id, version=1, confirmed=True, at=NOW
    )
    statuses = {item["resource_id"]: item["status"] for item in result["items"]}
    assert "not_found" in statuses.values()
    assert result["complete"] is True  # not_found: nothing left to delete
    assert foreign.exists()  # foreign workspace untouched


def test_replay_without_reexecution(env: dict[str, Any]) -> None:
    task_id = create_task(env, "replay")
    original = env["outside"] / "orig3.mp4"
    original.write_bytes(b"x")
    publish_resources(env, task_id, original)
    first = env["delete"].delete_task(
        operation_id="op-del-3", task_id=task_id, version=1, confirmed=True, at=NOW
    )
    second = env["delete"].delete_task(
        operation_id="op-del-3", task_id=task_id, version=1, confirmed=True, at=NOW
    )
    assert second == first  # ledger replay: identical per-item result


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_unconfirmed_and_all_demo_refused(env: dict[str, Any]) -> None:
    task_id = create_task(env, "refused")
    with pytest.raises(DomainViolation) as error:
        env["delete"].delete_task(
            operation_id="op-del-4", task_id=task_id, version=1, confirmed=False, at=NOW
        )
    assert error.value.code == "DOMAIN_DELETE_UNCONFIRMED"
    with pytest.raises(DomainViolation) as error:
        env["delete"].delete_task(
            operation_id="op-del-5",
            task_id=task_id,
            version=1,
            confirmed=True,
            scope="all_demo",
            at=NOW,
        )
    assert error.value.code == "DOMAIN_DELETE_SCOPE_UNAVAILABLE"
    assert env["metadata"].task_state(task_id) != "deleted"  # nothing happened


def test_settled_task_deletes_directly(env: dict[str, Any]) -> None:
    task_id = create_task(env, "settled")
    original = env["outside"] / "orig4.mp4"
    original.write_bytes(b"x")
    publish_resources(env, task_id, original)
    env["pipeline"].run(
        operation_id=f"op-pipe-{task_id}",
        task_id=task_id,
        version=1,
        fixture_id="overlong",
        cancel=CancellationToken(),
        at=NOW,
    )
    result = env["delete"].delete_task(
        operation_id="op-del-6", task_id=task_id, version=1, confirmed=True, at=NOW
    )
    assert result["complete"] is True
    assert env["metadata"].task_state(task_id) == "deleted"


def test_failed_item_is_retryable(env: dict[str, Any]) -> None:
    task_id = create_task(env, "retry")
    original = env["outside"] / "orig5.mp4"
    original.write_bytes(b"x")
    publish_resources(env, task_id, original)
    store = env["store"]
    real_delete = store.delete
    failing = {"remaining": 1}

    def flaky_delete(resource_id: str, *, user_requested: bool = False) -> Any:
        # resource ids are content hashes, so fail the first call whatever
        # it targets — the retry must recover regardless of order
        if failing["remaining"] > 0:
            failing["remaining"] -= 1
            raise DomainViolation("DELETE_FAILED", "simulated transient failure")
        return real_delete(resource_id, user_requested=user_requested)

    store.delete = flaky_delete  # type: ignore[method-assign]
    first = env["delete"].delete_task(
        operation_id="op-del-7", task_id=task_id, version=1, confirmed=True, at=NOW
    )
    assert first["complete"] is False
    failed = [i for i in first["items"] if i["status"] == "failed"]
    assert failed and failed[0]["code"] == "DOMAIN_DELETE_FAILED"

    retry = env["delete"].delete_task(
        operation_id="op-del-7", task_id=task_id, version=1, confirmed=True, at=NOW
    )
    assert retry["complete"] is True  # retry of the failed operation succeeded
    statuses = {item["resource_id"]: item["status"] for item in retry["items"]}
    assert set(statuses.values()) == {"deleted"}


def run_pipeline_to_analyzing(env: dict[str, Any], task_id: str) -> None:
    env["pipeline"].run(
        operation_id=f"op-pipe-{task_id}",
        task_id=task_id,
        version=1,
        fixture_id="iphone-flat",
        cancel=CancellationToken(),
        at=NOW,
    )
