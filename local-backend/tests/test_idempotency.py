"""Operation-id idempotency tests: replay, conflict, restart, races (BE-008).

The cases pin the ledger invariants from spec EX-005, FR-072 and plan §10.2:
an operation id is bound to the digest of its canonical inputs, repeats replay
the stored result, conflicting inputs are refused, a running or unknown
operation is never silently re-executed - not after a restart - and only an
explicit resolution reopens it. Concurrency is exercised with two real
connections racing on one SQLite file; the namespace package and temporary
databases replace any mock.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.application.idempotency import (
    OperationLedger,
    OperationRecord,
    canonical_input,
    input_digest,
)
from tennis_backend.domain.models import DomainViolation, Task, TaskVersion
from tennis_backend.storage.metadata import MetadataStore

UTC = dt.UTC
T0 = dt.datetime(2026, 11, 6, 13, 0, 0, tzinfo=UTC)


def tick(seconds: int) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


def analysis_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "fixture_id": "fx-forehand-1",
        "media_ref": "projects/demo/originals/clip.mp4",
        "primary_route": "technical_diagnosis",
        "auxiliary_routes": ["question_answer"],
    }
    params.update(overrides)
    return params


def draft_task() -> Task:
    return Task(
        task_id="task-1",
        versions=[
            TaskVersion(
                task_id="task-1",
                version=1,
                primary_route="technical_diagnosis",
                auxiliary_routes=["question_answer"],
                runtime_requirement="required",
                contract_version="1.1.0",
                config_version="fixture-config-1",
                media_id="media-1",
                consent_id="consent-1",
                simulation=True,
            )
        ],
        state="draft",
        stage="input",
        simulation=True,
    )


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "metadata.sqlite3"


@pytest.fixture
def ledger(database: Path) -> OperationLedger:
    """One ledger per test; the dedicated table lives in the configured file."""
    store = OperationLedger(database)
    yield store
    store.close()


def begin(
    ledger: OperationLedger,
    operation_id: str = "op-1",
    params: dict[str, Any] | None = None,
    method: str = "analysis.create",
    second: int = 0,
) -> Any:
    return ledger.begin(operation_id, method, params or analysis_params(), at=tick(second))


def begin_and_succeed(
    ledger: OperationLedger,
    operation_id: str = "op-1",
    result: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    outcome = begin(ledger, operation_id, params)
    ledger.succeed(
        operation_id, json.dumps(result or {"task_id": "task-1", "version": 1}), at=tick(1)
    )
    return outcome


# ---------------------------------------------------------------------------
# Canonical inputs and binding
# ---------------------------------------------------------------------------


def test_canonical_input_is_key_order_independent() -> None:
    first = canonical_input({"a": 1, "b": {"c": 3, "d": 4}})
    second = canonical_input({"b": {"d": 4, "c": 3}, "a": 1})
    assert first == second
    assert input_digest({"a": 1, "b": {"c": 3, "d": 4}}) == input_digest(
        {"b": {"d": 4, "c": 3}, "a": 1}
    )


def test_list_order_stays_meaningful() -> None:
    assert canonical_input({"routes": ["a", "b"]}) != canonical_input({"routes": ["b", "a"]})
    assert input_digest({"routes": ["a", "b"]}) != input_digest({"routes": ["b", "a"]})


def test_operation_id_binds_to_its_input_version(ledger: OperationLedger) -> None:
    outcome = begin(ledger)
    assert outcome.replayed is False
    assert outcome.record.state == "running"
    assert outcome.record.input_digest == input_digest(analysis_params())
    assert outcome.record.attempt == 1


@pytest.mark.parametrize("params", [{"other": 1}, analysis_params(primary_route="tactics")])
def test_same_operation_id_with_different_inputs_conflicts(
    ledger: OperationLedger, params: dict[str, Any]
) -> None:
    begin_and_succeed(ledger, result={"task_id": "task-1", "version": 1})
    before = ledger.lookup("op-1")
    with pytest.raises(DomainViolation) as raised:
        begin(ledger, params=params)
    assert raised.value.code == "DOMAIN_INPUT_CONFLICT"
    assert ledger.lookup("op-1") == before


def test_same_operation_id_with_a_changed_method_conflicts_review_reproduction(
    ledger: OperationLedger,
) -> None:
    """The reviewed defect: same id, same params, different method replayed cancel.

    The binding is method *and* canonical input digest, so reusing ``op-1`` for
    ``analysis.delete`` must be refused and must leave the stored
    ``analysis.cancel`` record exactly as it was.
    """
    begin(
        ledger,
        operation_id="op-1",
        method="analysis.cancel",
        params={"task_id": "t1", "version": 1},
    )
    ledger.succeed("op-1", "{}", at=tick(1))
    with pytest.raises(DomainViolation) as raised:
        begin(
            ledger,
            operation_id="op-1",
            method="analysis.delete",
            params={"task_id": "t1", "version": 1},
            second=2,
        )
    assert raised.value.code == "DOMAIN_INPUT_CONFLICT"
    record = ledger.lookup("op-1")
    assert record is not None
    assert record.method == "analysis.cancel"
    assert record.state == "succeeded"
    assert record.result_json == "{}"
    assert record.settled_at == tick(1)


def test_a_new_input_version_under_a_fresh_operation_id_starts(ledger: OperationLedger) -> None:
    begin_and_succeed(ledger, operation_id="op-1", result={"task_id": "task-1", "version": 1})
    second = begin(
        ledger,
        operation_id="op-2",
        params=analysis_params(input_version=2, fixture_id="fx-forehand-2"),
        second=4,
    )
    assert second.replayed is False
    assert second.record.state == "running"
    assert second.record.input_digest != ledger.lookup("op-1").input_digest  # type: ignore[union-attr]


def test_non_json_inputs_are_rejected(ledger: OperationLedger) -> None:
    with pytest.raises(DomainViolation):
        begin(ledger, params={"media": b"raw-bytes"})  # type: ignore[dict-item]
    assert ledger.lookup("op-1") is None


@pytest.mark.parametrize(
    ("operation_id", "method"),
    [("op-1", "bare"), ("op-1", "Analysis.Create"), ("", "analysis.create"), ("op-1", "")],
)
def test_malformed_identifiers_are_rejected(
    ledger: OperationLedger, operation_id: str, method: str
) -> None:
    with pytest.raises(DomainViolation):
        begin(ledger, operation_id=operation_id, method=method)


# ---------------------------------------------------------------------------
# Replay of settled results
# ---------------------------------------------------------------------------


def test_repeated_operation_id_replays_the_stored_result(
    ledger: OperationLedger,
) -> None:
    begin_and_succeed(ledger, result={"task_id": "task-1", "version": 1})
    replay = begin(ledger, second=2)
    assert replay.replayed is True
    assert replay.record.state == "succeeded"
    assert json.loads(replay.record.result_json or "{}") == {"task_id": "task-1", "version": 1}
    assert replay.record.settled_at == tick(1)


def test_identical_inputs_under_a_new_operation_id_replay_too(
    ledger: OperationLedger,
) -> None:
    begin_and_succeed(ledger, operation_id="op-1", result={"task_id": "task-1", "version": 1})
    replay = begin(ledger, operation_id="op-2", second=3)
    assert replay.replayed is True
    assert replay.same_inputs is True
    assert replay.record.operation_id == "op-1"
    assert ledger.lookup("op-2") is None


def test_lookup_by_inputs_finds_the_current_record(ledger: OperationLedger) -> None:
    begin_and_succeed(ledger, operation_id="op-1")
    found = ledger.lookup_by_inputs("analysis.create", analysis_params())
    assert isinstance(found, OperationRecord)
    assert found.operation_id == "op-1"
    assert (
        ledger.lookup_by_inputs("analysis.create", analysis_params(fixture_id="fx-other")) is None
    )
    assert ledger.lookup("op-404") is None


# ---------------------------------------------------------------------------
# Unresolved operations: running, restart and explicit resolution
# ---------------------------------------------------------------------------


def test_running_operation_cannot_start_a_second_execution(
    ledger: OperationLedger,
) -> None:
    begin(ledger)
    for operation_id in ("op-1", "op-2"):
        with pytest.raises(DomainViolation) as raised:
            begin(ledger, operation_id=operation_id)
        assert raised.value.code == "DOMAIN_OPERATION_UNRESOLVED"
    assert ledger.lookup("op-2") is None


def test_unknown_result_is_not_silently_redone(ledger: OperationLedger) -> None:
    begin(ledger)
    with pytest.raises(DomainViolation):
        ledger.resolve("op-1", "confirm_success", result_json=None, at=tick(1))
    assert ledger.lookup("op-1") is not None
    assert ledger.lookup("op-1").state == "running"  # type: ignore[union-attr]


def test_restart_does_not_reexecute_an_interrupted_operation(
    database: Path,
) -> None:
    """A crash mid-operation leaves a running row that a new process must not redo."""
    with OperationLedger(database) as first_process:
        first_process.begin("op-1", "analysis.create", analysis_params(), at=T0)
        assert first_process.lookup("op-1").state == "running"  # type: ignore[union-attr]
    # The process dies here without settling; the ledger is reopened later.
    with OperationLedger(database) as restarted:
        with pytest.raises(DomainViolation) as raised:
            restarted.begin("op-1", "analysis.create", analysis_params(), at=tick(9))
        assert raised.value.code == "DOMAIN_OPERATION_UNRESOLVED"
        assert restarted.unresolved() and restarted.unresolved()[0].attempt == 1
        # Explicit recovery is the only way forward.
        settled = restarted.resolve(
            "op-1", "confirm_success", result_json='{"task_id": "task-1"}', at=tick(10)
        )
        assert settled.state == "succeeded"
        replay = restarted.begin("op-1", "analysis.create", analysis_params(), at=tick(11))
        assert replay.replayed is True
        assert json.loads(replay.record.result_json or "{}") == {"task_id": "task-1"}


def test_confirm_failure_reopens_the_operation_for_a_deliberate_retry(
    database: Path,
) -> None:
    with OperationLedger(database) as ledger:
        ledger.begin("op-1", "analysis.create", analysis_params(), at=T0)
        settled = ledger.resolve(
            "op-1", "confirm_failure", error_code="ANALYSIS_FAILED", at=tick(1)
        )
        assert settled.state == "failed"
        retry = ledger.begin("op-1", "analysis.create", analysis_params(), at=tick(2))
        assert retry.replayed is False
        assert retry.record.state == "running"
        assert retry.record.attempt == 2
        assert retry.record.error_code is None


def test_resolution_requires_matching_payload(ledger: OperationLedger) -> None:
    begin(ledger)
    with pytest.raises(DomainViolation):
        ledger.resolve("op-1", "confirm_failure", error_code=None, at=tick(1))
    settled = ledger.resolve("op-1", "confirm_failure", error_code="MEDIA_UNREADABLE", at=tick(2))
    assert settled.state == "failed"
    with pytest.raises(DomainViolation) as raised:
        ledger.resolve("op-1", "confirm_failure", error_code="AGAIN", at=tick(3))
    assert raised.value.code == "DOMAIN_ALREADY_SETTLED"


@pytest.mark.parametrize("resolution", ["invalid_action", "retry", "auto_success", ""])
def test_invalid_resolution_is_rejected_before_any_mutation(
    ledger: OperationLedger, resolution: str
) -> None:
    """The reviewed defect: an unknown action name silently failed the operation.

    ``Literal`` only guides static callers, so the ledger validates the value
    itself, before reading or writing; the operation stays running and a valid
    resolution still works afterwards.
    """
    begin(ledger)
    with pytest.raises(DomainViolation) as raised:
        ledger.resolve("op-1", resolution, error_code="FAILED", at=tick(1))  # type: ignore[arg-type]
    assert raised.value.code == "DOMAIN_INVALID_RESOLUTION"
    record = ledger.lookup("op-1")
    assert record is not None and record.state == "running"
    assert record.error_code is None
    assert record.settled_at is None
    settled = ledger.resolve("op-1", "confirm_failure", error_code="FAILED", at=tick(2))
    assert settled.state == "failed"


def test_failing_records_the_stable_error_code(ledger: OperationLedger) -> None:
    begin(ledger)
    ledger.fail("op-1", "MEDIA_UNREADABLE", at=tick(1))
    record = ledger.lookup("op-1")
    assert record is not None and record.state == "failed"
    assert record.error_code == "MEDIA_UNREADABLE"
    retry = begin(ledger, second=2)
    assert retry.record.attempt == 2


def test_settling_requires_a_running_attempt(ledger: OperationLedger) -> None:
    begin(ledger)
    ledger.succeed("op-1", '{"task_id": "task-1"}', at=tick(1))
    with pytest.raises(DomainViolation):
        ledger.succeed("op-1", '{"task_id": "task-2"}', at=tick(2))
    assert json.loads(ledger.lookup("op-1").result_json or "{}") == {  # type: ignore[union-attr]
        "task_id": "task-1"
    }


# ---------------------------------------------------------------------------
# Concurrency: two real connections race on one file
# ---------------------------------------------------------------------------


def test_concurrent_attempts_produce_exactly_one_valid_result(
    database: Path,
) -> None:
    with OperationLedger(database) as worker_a, OperationLedger(database) as worker_b:
        outcome_a = worker_a.begin("op-1", "analysis.create", analysis_params(), at=T0)
        assert outcome_a.replayed is False
        # A second worker with the same id cannot start a parallel execution.
        with pytest.raises(DomainViolation):
            worker_b.begin("op-1", "analysis.create", analysis_params(), at=tick(1))
        worker_a.succeed("op-1", '{"task_id": "task-1", "version": 1}', at=tick(2))
        replay_b = worker_b.begin("op-1", "analysis.create", analysis_params(), at=tick(3))
        assert replay_b.replayed is True
        assert json.loads(replay_b.record.result_json or "{}")["task_id"] == "task-1"
        # Only one row exists for these inputs.
        assert worker_a.lookup_by_inputs("analysis.create", analysis_params()) == worker_b.lookup(
            "op-1"
        )


def test_ledger_shares_the_configured_metadata_file(
    database: Path,
) -> None:
    """The dedicated operation table lives next to the task tables, not in a second store."""
    with MetadataStore(database) as store, OperationLedger(database) as ledger:
        store.create_task(draft_task(), at=T0)
        begin_and_succeed(ledger, operation_id="op-1")
        operation_tables = {
            name
            for (name,) in ledger._connection.execute(  # noqa: SLF001
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"tasks", "task_state_events", "operations"} <= operation_tables
        assert store.task_state("task-1") == "draft"
        assert ledger.lookup("op-1") is not None


def test_concurrent_settle_is_compare_and_set(database: Path) -> None:
    """A stale settle cannot overwrite the outcome another worker recorded."""
    with OperationLedger(database) as worker_a, OperationLedger(database) as worker_b:
        worker_a.begin("op-1", "analysis.create", analysis_params(), at=T0)
        worker_b.fail("op-1", "MEDIA_UNREADABLE", at=tick(1))
        with pytest.raises(DomainViolation) as raised:
            worker_a.succeed("op-1", '{"task_id": "task-9"}', at=tick(2))
        assert raised.value.code == "DOMAIN_STATE_MISMATCH"
        record = worker_a.lookup("op-1")
        assert record is not None and record.state == "failed"
        assert record.error_code == "MEDIA_UNREADABLE"


# ---------------------------------------------------------------------------
# Storage shape and decoupling
# ---------------------------------------------------------------------------


def test_operation_table_declares_no_blob_column(ledger: OperationLedger) -> None:
    types = set(ledger.column_types("operations").values())
    assert types <= {"TEXT", "INTEGER", "REAL"}
    assert ledger.column_types("operations")["result_json"] == "TEXT"


def test_ledger_module_has_no_runtime_routing_or_delivery_words() -> None:
    import ast

    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "tennis_backend"
        / "application"
        / "idempotency.py"
    ).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)
    assert imported <= {
        "__future__",
        "collections.abc",
        "contextlib",
        "dataclasses",
        "datetime",
        "hashlib",
        "json",
        "pathlib",
        "re",
        "sqlite3",
        "tennis_backend.domain.models",
        "typing",
    }
    lowered = source.lower()
    for forbidden in ("workbuddy", "smtp", "smtplib", "requests", "cv2", "ffmpeg"):
        assert forbidden not in lowered
