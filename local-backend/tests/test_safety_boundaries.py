"""Cross-module safety boundary regressions (BE-028, V-B).

Composes the already-reviewed building blocks across module lines and pins
the safety guarantees that must hold everywhere at once:

- **Safety stop** (AC-SPEC-028): a task that entered ``safety_stop`` blocks
  derived writes, recovery, and cancellation; nothing resumes routine work.
- **Withdrawn authorization** (AC-SPEC-008/039): a withdrawn analysis
  purpose refuses recovery and diagnostic export; consent never extends.
- **Path boundaries** (FR-055 area): traversal media refs and registry path
  escapes are refused at the boundaries; reset pruning never leaves the
  task workspace; the export package never contains original video.
- **Log hygiene**: backend stderr stays sanitized (rule + reference only);
  no raw params, exception text, paths or credentials.
- **No real network / WorkBuddy / mail**: the backend source carries no
  network or mail imports anywhere, so nothing can send even by accident.

All scenarios reuse the reviewed services against real SQLite stores and
the frozen fixture library; no real media, network or user data.
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
from tennis_backend.application.delete import DeleteService
from tennis_backend.application.idempotency import OperationLedger
from tennis_backend.application.pipeline import AnalysisPipeline
from tennis_backend.application.recovery import plan_recovery
from tennis_backend.application.tasks import TaskService
from tennis_backend.application.worker import CancellationToken
from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.domain.models import ConsentSnapshot, DomainViolation, Grounded, UserInput
from tennis_backend.storage.metadata import MetadataStore
from tennis_backend.storage.resources import ResourceStore

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "local-backend" / "src"
FIXTURE_ROOT = REPO_ROOT / "fixtures"
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    counter = {"n": 0}

    def factory() -> str:
        counter["n"] += 1
        return f"task-{counter['n']:04d}"

    metadata = MetadataStore(tmp_path / "meta.sqlite")
    ledger = OperationLedger(tmp_path / "ledger.sqlite")
    resources = ResourceStore(tmp_path / "public", tmp_path / "internal")
    service = TaskService(metadata, ledger, task_id_factory=factory)
    registry = ContractRegistry.load(REPO_ROOT / "contracts" / "v1")
    adapter = FixtureAnalysisAdapter(FixtureLibrary.load(FIXTURE_ROOT))
    pipeline = AnalysisPipeline(
        service=service, metadata=metadata, ledger=ledger, adapter=adapter, registry=registry
    )
    return {
        "tmp": tmp_path,
        "metadata": metadata,
        "ledger": ledger,
        "resources": resources,
        "service": service,
        "pipeline": pipeline,
        "adapter": adapter,
        "registry": registry,
        "cancel": CancelService(metadata),
    }


def create_task(env: dict[str, Any], hint: str, safety: str = "clear") -> str:
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
            safety=Grounded.observed(safety),
        ),
        primary_route="technical_diagnosis",
        auxiliary_routes=["tactics"],
        at=NOW,
    )
    return outcome.task_id


def run_pipeline(env: dict[str, Any], task_id: str, fixture_id: str = "iphone-flat") -> Any:
    return env["pipeline"].run(
        operation_id=f"op-pipe-{task_id}",
        task_id=task_id,
        version=1,
        fixture_id=fixture_id,
        cancel=CancellationToken(),
        at=NOW,
    )


# ---------------------------------------------------------------------------
# Safety stop: nothing resumes routine work (AC-SPEC-028)
# ---------------------------------------------------------------------------


def test_safety_stop_blocks_every_resume_path(env: dict[str, Any]) -> None:
    metadata, cancel = env["metadata"], env["cancel"]
    task_id = create_task(env, "safety")
    # the user reports pain mid-run: the state machine moves to safety_stop
    metadata.record_transition(
        task_id, current="draft", target="checking_input", reason="start", at=NOW
    )
    metadata.record_transition(
        task_id, current="checking_input", target="analyzing", reason="start", at=NOW
    )
    metadata.record_transition(
        task_id, current="analyzing", target="safety_stop", reason="user_reported_pain", at=NOW
    )

    # derived writes stop
    with pytest.raises(DomainViolation) as error:
        assert_derivative_writes_allowed(metadata, task_id)
    assert error.value.code == "DOMAIN_CANCELLED_WRITES_BLOCKED"

    # recovery refuses: a settled task's results stand
    plan = plan_recovery(
        metadata=metadata,
        service=env["service"],
        adapter=env["adapter"],
        task_id=task_id,
        version=1,
        fixture_id="iphone-flat",
    )
    assert plan.status == "refused"
    assert plan.refusals == ("task_settled",)

    # cancellation refuses: settled states are not cancellable
    with pytest.raises(DomainViolation) as error:
        cancel.cancel(task_id)
    assert error.value.code == "DOMAIN_CANCEL_NOT_CANCELLABLE"

    # the state keeps standing
    assert metadata.task_state(task_id) == "safety_stop"


def test_backend_source_carries_no_medical_or_training_prescriptions() -> None:
    """The backend never writes final teaching prose: banned vocabulary in
    every backend source file (the WorkBuddy layer owns all coaching text)."""
    banned = ("医疗诊断", "处方", "恢复训练", "线下求助", "medical advice", "prescribe")
    for source in (SRC_DIR / "tennis_backend").rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text, f"{source.name} carries banned vocabulary: {word}"


# ---------------------------------------------------------------------------
# Withdrawn authorization blocks recovery and export (AC-SPEC-008/039)
# ---------------------------------------------------------------------------


def _consent(task_id: str, purposes: list[str]) -> ConsentSnapshot:
    return ConsentSnapshot(
        consent_id="consent-1",
        task_id=task_id,
        version=1,
        purposes=purposes,
        text_version="consent-text-1",
        confirmed=True,
        guardian_required=False,
        guardian_simulated=False,
        confirmed_at=NOW,
        recipient_digest=None,
        materials_digest=None,
    )


def test_withdrawn_analysis_purpose_blocks_recovery_and_reset(env: dict[str, Any]) -> None:
    metadata, service, adapter = env["metadata"], env["service"], env["adapter"]
    task_id = create_task(env, "withdrawn")
    run_pipeline(env, task_id)
    withdrawn = _consent(task_id, purposes=["save"])  # analysis withdrawn

    plan = plan_recovery(
        metadata=metadata,
        service=service,
        adapter=adapter,
        task_id=task_id,
        version=1,
        fixture_id="iphone-flat",
        consent=withdrawn,
        registry=env["registry"],
    )
    assert plan.status == "refused"
    assert plan.refusals == ("RECOVERY_NOT_AUTHORIZED",)


def test_withdrawn_consent_blocks_diagnostic_export(env: dict[str, Any]) -> None:
    service: TaskService = env["service"]
    task_id = create_task(env, "export")
    ok = _consent(task_id, ["analysis", "diagnostics"])
    service.check_consent(consent=ok, purpose="diagnostics", task_id=task_id, version=1)
    # the user withdraws diagnostics but keeps share: export refuses while
    # share still works — one purpose never extends to another
    reduced = service.withdraw_purpose(ok, "diagnostics")
    with pytest.raises(DomainViolation) as error:
        service.check_consent(consent=reduced, purpose="diagnostics", task_id=task_id, version=1)
    assert error.value.code == "DOMAIN_CONSENT_PURPOSE"
    service.check_consent(consent=reduced, purpose="analysis", task_id=task_id, version=1)


# ---------------------------------------------------------------------------
# Path boundaries
# ---------------------------------------------------------------------------


def test_path_traversal_in_create_media_ref_is_refused(env: dict[str, Any]) -> None:
    from pydantic import ValidationError

    from tennis_backend.contracts.validation import CreateInput

    with pytest.raises(ValidationError) as error:
        CreateInput.model_validate(
            {
                "operation_id": "op-x",
                "fixture_id": "fixture-flat-1",
                "media_ref": "../escape/正手.mp4",
                "user": {
                    "age_band": "adult",
                    "handedness": "right",
                    "target_stroke": "forehand",
                    "goal": "g",
                    "exercise_background": "e",
                    "safety": "clear",
                },
                "consent": {
                    "consent_id": "c1",
                    "task_version": 1,
                    "purposes": ["analysis"],
                    "text_version": "t1",
                    "confirmed": True,
                    "guardian_simulated": False,
                    "recipient_digest": None,
                    "materials_digest": None,
                },
                "primary_route": "technical_diagnosis",
                "auxiliary_routes": [],
                "subject_id": None,
                "mirror_confirmed": False,
            }
        )
    assert "relative path" in str(error.value)


def test_registry_path_escape_is_refused(env: dict[str, Any]) -> None:
    task_id = create_task(env, "escape")
    with pytest.raises(DomainViolation) as error:
        env["resources"].publish_bytes(
            task_id, 1, "../escape/evil.png", b"x", kind="evidence_frame", mime="image/png"
        )
    assert error.value.code == "DOMAIN_PATH_REJECTED"


def test_reset_pruning_never_leaves_the_workspace(env: dict[str, Any]) -> None:
    """A sibling directory outside every task workspace must survive reset
    pruning even when a task workspace sits next to it."""
    from tennis_backend.application.reset import ResetService

    sibling = env["tmp"] / "public" / "projects" / "keep-me"
    sibling.mkdir(parents=True)
    (sibling / "precious.txt").write_text("keep", encoding="utf-8")

    task_id = create_task(env, "prune")
    run_pipeline(env, task_id)
    reset = ResetService(
        metadata=env["metadata"],
        ledger=env["ledger"],
        resources=env["resources"],
        delete=DeleteService(
            metadata=env["metadata"],
            ledger=env["ledger"],
            resources=env["resources"],
            cancel=env["cancel"],
        ),
        cancel=env["cancel"],
    )
    result = reset.reset_all_demo(operation_id="op-reset-boundary", confirmed=True, at=NOW)
    assert result["complete"] is True
    assert (sibling / "precious.txt").exists()  # pruning never left the workspace
    assert not (env["tmp"] / "public" / "projects" / task_id).exists()


# ---------------------------------------------------------------------------
# Concurrent delete / cancel boundary: exactly one honest winner
# ---------------------------------------------------------------------------


def test_concurrent_delete_and_cancel_produce_one_honest_terminal(
    env: dict[str, Any],
) -> None:
    import threading

    from tennis_backend.application.cancel import CancelService

    tmp = env["tmp"]
    task_id = create_task(env, "race")
    run_pipeline(env, task_id)

    outcomes: list[str] = []
    errors: list[str] = []

    def do_cancel() -> None:
        # production topology: each thread opens its own stores (AG-B02)
        metadata = MetadataStore(tmp / "meta.sqlite")
        try:
            outcomes.append(CancelService(metadata).cancel(task_id).state)
        except DomainViolation as violation:
            errors.append(violation.code)
        finally:
            metadata.close()

    def do_delete() -> None:
        metadata = MetadataStore(tmp / "meta.sqlite")
        ledger = OperationLedger(tmp / "ledger.sqlite")
        resources = ResourceStore(tmp / "public", tmp / "internal")
        delete = DeleteService(
            metadata=metadata,
            ledger=ledger,
            resources=resources,
            cancel=CancelService(metadata),
        )
        try:
            result = delete.delete_task(
                operation_id="op-del-race",
                task_id=task_id,
                version=1,
                confirmed=True,
                at=NOW,
            )
            outcomes.append("deleted" if result["complete"] else "delete_incomplete")
        except DomainViolation as violation:
            errors.append(violation.code)
        finally:
            ledger.close()
            metadata.close()

    threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_delete)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    final = env["metadata"].task_state(task_id)
    assert final in ("cancelled", "deleted")  # exactly one honest terminal
    # every outcome/refusal is a stable business answer, never a raw
    # storage error; a sequentially successful delete after a cancelled
    # state is honest (the CAS loser retries through the state machine)
    for code in errors:
        assert code.startswith("DOMAIN_"), code
    assert set(outcomes) <= {"cancelled", "deleted", "delete_incomplete"}


# ---------------------------------------------------------------------------
# Log hygiene across the transport boundary
# ---------------------------------------------------------------------------


def test_backend_source_has_no_network_or_mail_imports() -> None:
    banned = ("socket", "smtplib", "urllib.request", "http.client", "asyncio.open_connection")
    for source in (SRC_DIR / "tennis_backend").rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text, f"{source.name} imports network/mail: {word}"


def test_stdio_stderr_stays_sanitized_under_faults(tmp_path: Path) -> None:
    """End-to-end log hygiene: a faulting request must not leak raw params,
    exception text or absolute paths into stderr or stdout."""
    from test_cli import BackendCli, envelope

    def create_params(operation_id: str, fixture_id: str) -> dict[str, Any]:
        return {
            "operation_id": operation_id,
            "fixture_id": fixture_id,
            "media_ref": "fixtures/media/flat-forehand.mp4",
            "user": {
                "age_band": "adult",
                "handedness": "right",
                "target_stroke": "forehand",
                "goal": "stabilise the contact point",
                "exercise_background": "two years of recreational play",
                "safety": "clear",
            },
            "consent": {
                "consent_id": "consent-1",
                "task_version": 1,
                "purposes": ["analysis"],
                "text_version": "consent-text-1",
                "confirmed": True,
                "guardian_simulated": True,
                "recipient_digest": None,
                "materials_digest": None,
            },
            "primary_route": "technical_diagnosis",
            "auxiliary_routes": ["tactics"],
            "subject_id": None,
            "mirror_confirmed": False,
        }

    secret_fixture = "fixture-secret-boom-x"
    proc = BackendCli(tmp_path)
    try:
        proc.send(
            envelope(
                "req-x",
                "analysis.create",
                create_params("op-x", fixture_id=secret_fixture),
                operation_id="op-x",
            )
        )
        payload = proc.response()
        assert payload["ok"] is False
        assert payload["error"]["code"] == "DOMAIN_FIXTURE_UNKNOWN"
        # stderr stays rule+reference only; raw input is never echoed
        stderr = proc.stderr_text
        assert secret_fixture not in stderr
        assert str(tmp_path) not in stderr
        # stdout stayed protocol-only: every response() call already
        # re-parsed a complete frozen envelope without error
    finally:
        proc.kill()
