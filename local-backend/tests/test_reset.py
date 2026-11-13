"""Demo-wide reset tests (BE-026, AC-SPEC-051).

Real stores and the real per-task deletion flow: multiple demo tasks with
published resources (including a user original outside the root), runtime
metadata states, and the reset's guarantees — confirmation not extended,
user originals and installed components untouched, failures recorded per
item, and idempotent replay of the whole batch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.application.cancel import CancelService
from tennis_backend.application.delete import DeleteService
from tennis_backend.application.idempotency import OperationLedger
from tennis_backend.application.reset import ResetService
from tennis_backend.application.tasks import TaskService
from tennis_backend.domain.models import DomainViolation, Grounded, UserInput
from tennis_backend.storage.metadata import MetadataStore
from tennis_backend.storage.resources import ResourceRegistry, ResourceStore

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)


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
    cancel = CancelService(metadata)
    delete = DeleteService(metadata=metadata, ledger=ledger, resources=store, cancel=cancel)
    reset = ResetService(
        metadata=metadata, ledger=ledger, resources=store, delete=delete, cancel=cancel
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    return {
        "metadata": metadata,
        "service": service,
        "store": store,
        "reset": reset,
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
            goal=Grounded.observed(f"稳定击球-{hint}"),  # distinct inputs: one real task per hint
            exercise_background=Grounded.observed("recreational"),
            safety=Grounded.observed("clear"),
        ),
        primary_route="technical_diagnosis",
        auxiliary_routes=["tactics"],
        at=NOW,
    )
    return outcome.task_id


def publish(env: dict[str, Any], task_id: str, original: Path) -> None:
    env["store"].publish_bytes(
        task_id,
        1,
        f"projects/{task_id}/v1/evidence/ev.png",
        b"png-bytes",
        kind="evidence_frame",
        mime="image/png",
    )
    env["store"].publish_input_copy(task_id, 1, original, mime="video/mp4")


def test_reset_all_demo_tasks_with_per_item_truth(env: dict[str, Any]) -> None:
    originals = []
    for index in ("a", "b"):
        original = env["outside"] / f"orig-{index}.mp4"
        original.write_bytes(f"bytes-{index}".encode())
        originals.append(original)
        task_id = create_task(env, index)
        publish(env, task_id, original)

    result = env["reset"].reset_all_demo(operation_id="op-reset-1", confirmed=True, at=NOW)
    assert result["scope"] == "all_demo"
    assert result["complete"] is True
    assert len(result["tasks"]) == 2
    assert all(task["complete"] for task in result["tasks"])
    assert result["runtime_uninstalled"] is False
    assert result["external_copies_uncontrolled"] is True
    # every workspace file is gone; every user original untouched
    workspace = env["tmp"] / "public" / "projects"
    assert list(workspace.rglob("*")) == []
    for original in originals:
        assert original.exists()
    states = [env["metadata"].task_state(f"task-{n:04d}") for n in (1, 2)]
    assert states == ["deleted", "deleted"]


def test_runtime_state_reset_and_honest_failure(env: dict[str, Any]) -> None:
    metadata = env["metadata"]
    # a state with no legal path back to not_initialized: recorded honestly
    metadata.record_runtime_transition("awaiting_consent", reason="setup", at=NOW)
    metadata.record_runtime_transition("preparing", reason="setup", at=NOW)
    metadata.record_runtime_transition("verifying", reason="setup", at=NOW)
    metadata.record_runtime_transition("ready", reason="setup", at=NOW)
    result = env["reset"].reset_all_demo(operation_id="op-reset-2", confirmed=True, at=NOW)
    assert result["runtime"]["status"] == "failed"
    assert "installed components untouched" in result["runtime"]["note"]
    assert result["complete"] is False  # honest: the reset is incomplete
    # nothing was force-uninstalled: the state is still ready
    assert metadata.runtime_state() == "ready"
    # a legal path resets honestly; a failed reset retries through the SAME
    # operation id (BE-008: a failed operation blocks new identical ones)
    metadata.record_runtime_transition("awaiting_consent", reason="bridge", at=NOW)
    result2 = env["reset"].reset_all_demo(operation_id="op-reset-2", confirmed=True, at=NOW)
    assert result2["runtime"]["status"] == "reset"
    assert metadata.runtime_state() == "not_initialized"


def test_concurrent_registration_is_drained_then_reset(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """BE-026 review blocking finding: a task registered concurrently could
    escape a single enumeration while the reset claimed complete=True. The
    drain loop must pick it up and complete honestly."""
    import tennis_backend.application.reset as reset_module

    create_task(env, "early")
    original = env["outside"] / "orig-e.mp4"
    original.write_bytes(b"x")
    publish(env, "task-0001", original)

    real_registry = reset_module.ResourceRegistry
    calls = {"n": 0}

    class LateRegistry:
        """First enumeration sees one task; later ones see the late arrival."""

        def __init__(self, path: Path) -> None:
            self._inner = real_registry(path)

        def entries(self) -> tuple[Any, ...]:
            calls["n"] += 1
            entries = self._inner.entries()
            if calls["n"] >= 2 and len({e.task_id for e in entries}) == 1:
                late_task = create_task(env, "late")
                late_original = env["outside"] / "orig-late.mp4"
                late_original.write_bytes(b"x")
                publish(env, late_task, late_original)
            return self._inner.entries()

    monkeypatch.setattr(reset_module, "ResourceRegistry", LateRegistry)
    result = env["reset"].reset_all_demo(operation_id="op-reset-8", confirmed=True, at=NOW)
    assert result["complete"] is True
    assert result["refusals"] == []
    reset_task_ids = {task["task_id"] for task in result["tasks"]}
    assert "task-0002" in reset_task_ids  # the late arrival was drained and reset
    workspace = env["tmp"] / "public" / "projects"
    assert list(workspace.rglob("*")) == []
    assert any(
        "coverage: every surviving demo task" in note and "draft tasks" in note
        for note in result["notes"]
    )


def test_never_ending_concurrency_refuses_complete(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import tennis_backend.application.reset as reset_module

    create_task(env, "seed")
    original = env["outside"] / "orig-s.mp4"
    original.write_bytes(b"x")
    publish(env, "task-0001", original)

    real_registry = reset_module.ResourceRegistry
    churn = {"n": 0}

    class ChurningRegistry:
        """Every enumeration simulates the real race: a concurrent task
        publishes a resource, so the registry keeps growing."""

        def __init__(self, path: Path) -> None:
            self._inner = real_registry(path)

        def entries(self) -> tuple[Any, ...]:
            self._inner.entries()
            churn["n"] += 1
            churn_task = env["service"].create_task(
                operation_id=f"op-churn-{churn['n']}-{len(env['tmp'].name)}",
                fixture_id=f"fixture-churn-{churn['n']}",
                user_input=UserInput(
                    question=Grounded.observed("q"),
                    age_band=Grounded.observed("adult"),
                    handedness=Grounded.observed("right"),
                    target_stroke=Grounded.observed("forehand"),
                    goal=Grounded.observed(f"churn-{churn['n']}"),
                    exercise_background=Grounded.observed("e"),
                    safety=Grounded.observed("clear"),
                ),
                primary_route="technical_diagnosis",
                auxiliary_routes=["tactics"],
                at=NOW,
            )
            env["store"].publish_bytes(
                churn_task.task_id,
                1,
                f"projects/{churn_task.task_id}/v1/evidence/c.png",
                b"churn",
                kind="evidence_frame",
                mime="image/png",
            )
            # re-read after the publish: the new entry is now visible
            return self._inner.entries()

    monkeypatch.setattr(reset_module, "ResourceRegistry", ChurningRegistry)
    result = env["reset"].reset_all_demo(operation_id="op-reset-9", confirmed=True, at=NOW)
    # never claims completion while the registry keeps changing
    assert result["complete"] is False
    assert "RESET_CONCURRENT_ACTIVITY" in result["refusals"]
    assert any("re-run the reset" in note for note in result["notes"])
    assert any("concurrent task registration" in note for note in result["notes"])


def test_metadata_survivor_reconciled_even_if_registry_blind(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """BE-023/026 review: a task invisible to the registry (published in a
    race window) must still be reconciled through metadata — never silently
    dropped while the reset claims complete."""
    import tennis_backend.application.reset as reset_module

    create_task(env, "visible")
    original = env["outside"] / "orig-v.mp4"
    original.write_bytes(b"x")
    publish(env, "task-0001", original)
    # a concurrent task whose publish raced the enumeration window:
    # registered in metadata, invisible to a blind registry reader
    late = create_task(env, "blind")
    late_original = env["outside"] / "orig-b.mp4"
    late_original.write_bytes(b"x")
    env["store"].publish_bytes(
        late,
        1,
        f"projects/{late}/v1/evidence/b.png",
        b"blind",
        kind="evidence_frame",
        mime="image/png",
    )

    class BlindRegistry:
        """A registry reader that only ever sees the first task."""

        def __init__(self, path: Path) -> None:
            self._inner = ResourceRegistry(path)

        def entries(self) -> tuple[Any, ...]:
            return tuple(e for e in self._inner.entries() if e.task_id == "task-0001")

    monkeypatch.setattr(reset_module, "ResourceRegistry", BlindRegistry)
    result = env["reset"].reset_all_demo(operation_id="op-reset-10", confirmed=True, at=NOW)
    # reconciliation through metadata found the blind task and reset it too
    task_ids = {task["task_id"] for task in result["tasks"]}
    assert late in task_ids
    assert env["metadata"].task_state(late) == "deleted"
    assert result["complete"] is True


def test_registry_never_stabilizes_refuses_complete(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import tennis_backend.application.reset as reset_module

    create_task(env, "unstable")
    original = env["outside"] / "orig-u.mp4"
    original.write_bytes(b"x")
    publish(env, "task-0001", original)

    class FlipFlopRegistry:
        """Consecutive reads never agree: no stable view may be claimed.

        The flip state is class-level because every registry read constructs
        a fresh instance — instability must survive that, like the real
        file-backed registry does."""

        flip = True

        def __init__(self, path: Path) -> None:
            self._inner = ResourceRegistry(path)

        def entries(self) -> tuple[Any, ...]:
            FlipFlopRegistry.flip = not FlipFlopRegistry.flip
            entries = self._inner.entries()
            if FlipFlopRegistry.flip:
                return entries
            return tuple()

    monkeypatch.setattr(reset_module, "ResourceRegistry", FlipFlopRegistry)
    result = env["reset"].reset_all_demo(operation_id="op-reset-11", confirmed=True, at=NOW)
    assert result["complete"] is False
    assert "RESET_REGISTRY_UNSTABLE" in result["refusals"]


def test_second_confirmed_reset_executes_on_new_demo_data(
    env: dict[str, Any],
) -> None:
    """BE-026 review round 2 blocking finding: with scope-only ledger input,
    BE-008's same-input replay made every reset after the first silently
    claim a stale completion. A NEW confirmed reset must execute and really
    delete the demo data created after the previous reset."""
    create_task(env, "first")
    first_original = env["outside"] / "orig-1st.mp4"
    first_original.write_bytes(b"first")
    publish(env, "task-0001", first_original)

    first = env["reset"].reset_all_demo(operation_id="op-reset-A", confirmed=True, at=NOW)
    assert first["complete"] is True

    # new demo data appears after the first reset
    second_original = env["outside"] / "orig-2nd.mp4"
    second_original.write_bytes(b"second")
    second_task = create_task(env, "second")
    publish(env, second_task, second_original)

    second = env["reset"].reset_all_demo(operation_id="op-reset-B", confirmed=True, at=NOW)
    # must NOT be a replay: the new task was really deleted
    assert second["complete"] is True
    reset_task_ids = {task["task_id"] for task in second["tasks"]}
    assert second_task in reset_task_ids
    workspace = env["tmp"] / "public" / "projects"
    assert list(workspace.rglob("*")) == []
    assert env["metadata"].task_state(second_task) == "deleted"
    # both user originals survive
    assert first_original.exists() and second_original.exists()


def test_replay_of_same_operation_id_is_still_stable(env: dict[str, Any]) -> None:
    # the same operation id replays its own batch (retry/replay semantics
    # preserved even though new invocations now execute)
    create_task(env, "stable")
    original = env["outside"] / "orig-s.mp4"
    original.write_bytes(b"x")
    publish(env, "task-0001", original)
    first = env["reset"].reset_all_demo(operation_id="op-reset-S", confirmed=True, at=NOW)
    second = env["reset"].reset_all_demo(operation_id="op-reset-S", confirmed=True, at=NOW)
    assert second == first


def test_draft_without_resources_is_deleted_and_recorded(
    env: dict[str, Any],
) -> None:
    """BE-026 review round 2 blocking finding 2: metadata reconciliation
    deletes resource-less draft tasks — the notes and this test must say so
    instead of claiming they survive."""
    create_task(env, "with-data")
    original = env["outside"] / "orig-d.mp4"
    original.write_bytes(b"x")
    publish(env, "task-0001", original)
    draft = create_task(env, "draft")  # no resources ever published

    result = env["reset"].reset_all_demo(operation_id="op-reset-D", confirmed=True, at=NOW)
    assert result["complete"] is True
    reset_task_ids = {task["task_id"] for task in result["tasks"]}
    assert draft in reset_task_ids  # reconciled through metadata
    assert env["metadata"].task_state(draft) == "deleted"
    assert any("draft tasks without resources" in note for note in result["notes"])


def test_confirmation_does_not_extend(env: dict[str, Any]) -> None:
    task_id = create_task(env, "noext")
    env["reset"].reset_all_demo(operation_id="op-reset-4", confirmed=True, at=NOW)
    # the reset's confirmation is not sticky: later flows still need their own
    with pytest.raises(DomainViolation) as error:
        env["delete"].delete_task(
            operation_id="op-del-x", task_id=task_id, version=1, confirmed=False, at=NOW
        )
    assert error.value.code == "DOMAIN_DELETE_UNCONFIRMED"
    with pytest.raises(DomainViolation) as error:
        env["reset"].reset_all_demo(operation_id="op-reset-5", confirmed=False, at=NOW)
    assert error.value.code == "DOMAIN_RESET_UNCONFIRMED"


def test_replay_returns_identical_batch(env: dict[str, Any]) -> None:
    create_task(env, "replay")
    original = env["outside"] / "orig-r.mp4"
    original.write_bytes(b"x")
    publish(env, "task-0001", original)
    first = env["reset"].reset_all_demo(operation_id="op-reset-6", confirmed=True, at=NOW)
    second = env["reset"].reset_all_demo(operation_id="op-reset-6", confirmed=True, at=NOW)
    assert second == first  # whole-batch replay, no re-deletion


def test_incomplete_batch_is_retryable(env: dict[str, Any]) -> None:
    create_task(env, "retry")
    original = env["outside"] / "orig-t.mp4"
    original.write_bytes(b"x")
    publish(env, "task-0001", original)
    store = env["store"]
    real_delete = store.delete
    attempts = {"n": 0}

    def flaky(resource_id: str, *, user_requested: bool = False) -> Any:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise DomainViolation("DELETE_FAILED", "simulated transient failure")
        return real_delete(resource_id, user_requested=user_requested)

    store.delete = flaky  # type: ignore[method-assign]
    first = env["reset"].reset_all_demo(operation_id="op-reset-7", confirmed=True, at=NOW)
    assert first["complete"] is False
    retry = env["reset"].reset_all_demo(operation_id="op-reset-7", confirmed=True, at=NOW)
    assert retry["complete"] is True
    assert all(task["complete"] for task in retry["tasks"])
