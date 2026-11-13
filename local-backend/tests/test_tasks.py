"""Application-service tests for task create/lookup/version/authorization (BE-011).

Covers: gap listing without analysis (AC-SPEC-003), youth guardian and stale
external consent refusal (AC-SPEC-007), per-purpose withdrawal leaving other
purposes untouched (AC-SPEC-008), idempotent create/version replay with no
duplicate results (EX-005), no NTRP field anywhere (FR-004, US-002), and the
summary shape satisfying the frozen ``task:summary`` contract. All payloads
are synthetic; no media, network or real user data is involved.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tennis_backend.application.idempotency import OperationLedger
from tennis_backend.application.tasks import TaskService
from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.domain.models import (
    ConsentSnapshot,
    DomainViolation,
    Grounded,
    UserInput,
)
from tennis_backend.storage.metadata import MetadataStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "v1"

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


def complete_user_input(**overrides: Grounded[Any]) -> UserInput:
    fields: dict[str, Grounded[Any]] = {
        "question": Grounded.observed("看看我的正手"),
        "age_band": Grounded.observed("adult"),
        "handedness": Grounded.observed("right"),
        "target_stroke": Grounded.observed("forehand"),
        "goal": Grounded.observed("稳定击球点"),
        "exercise_background": Grounded.observed("two years of recreational play"),
        "safety": Grounded.observed("clear"),
    }
    fields.update(overrides)
    return UserInput(**fields)


def adult_consent(task_id: str, version: int = 1, **overrides: Any) -> ConsentSnapshot:
    payload: dict[str, Any] = {
        "consent_id": "consent-1",
        "task_id": task_id,
        "version": version,
        "purposes": ["analysis", "save"],
        "text_version": "consent-text-1",
        "confirmed": True,
        "guardian_required": False,
        "guardian_simulated": False,
        "confirmed_at": NOW,
        "recipient_digest": None,
        "materials_digest": None,
    }
    payload.update(overrides)
    return ConsentSnapshot(**payload)


def youth_consent(task_id: str, **overrides: Any) -> ConsentSnapshot:
    payload: dict[str, Any] = {"guardian_required": True, "guardian_simulated": True}
    payload.update(overrides)
    return adult_consent(task_id, **payload)


def external_consent(task_id: str) -> ConsentSnapshot:
    return adult_consent(
        task_id,
        purposes=["share"],
        recipient_digest="digest-recipient-1",
        materials_digest="digest-materials-1",
    )


@pytest.fixture
def service(tmp_path: Path) -> TaskService:
    counter = {"n": 0}

    def factory() -> str:
        counter["n"] += 1
        return f"task-{counter['n']:04d}"

    return TaskService(
        MetadataStore(tmp_path / "meta.sqlite"),
        OperationLedger(tmp_path / "ledger.sqlite"),
        task_id_factory=factory,
    )


def create_kwargs(user_input: UserInput | None = None, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operation_id": "op-create-1",
        "fixture_id": "fixture-flat-1",
        "user_input": user_input or complete_user_input(),
        "primary_route": "technical_diagnosis",
        "auxiliary_routes": ["tactics"],
        "at": NOW,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Create: draft storage, gaps, idempotent replay
# ---------------------------------------------------------------------------


def test_complete_create_stores_draft_and_starts(service: TaskService) -> None:
    outcome = service.create_task(**create_kwargs())
    assert (outcome.task_id, outcome.version, outcome.state) == ("task-0001", 1, "draft")
    assert outcome.started is True
    assert outcome.missing_inputs == ()
    assert outcome.replayed is False
    summary = service.task_summary(outcome.task_id)
    assert summary["state"] == "draft"
    assert summary["stage"] == "input"
    assert summary["workspace_ref"] == "projects/task-0001/v1"


def test_missing_required_inputs_lists_gaps_without_analysis(service: TaskService) -> None:
    outcome = service.create_task(
        **create_kwargs(
            complete_user_input(
                goal=Grounded.absent("not_provided"),
                safety=Grounded.absent("unknown"),
            )
        )
    )
    assert outcome.started is False
    assert outcome.missing_inputs == ("goal", "safety")
    assert outcome.state == "draft"  # stored, but analysis must not start
    summary = service.task_summary(outcome.task_id)
    assert summary["state"] == "draft"
    assert summary["checkpoints"] == []  # no analysis checkpoint exists


def test_duplicate_operation_replays_without_duplicate_task(service: TaskService) -> None:
    first = service.create_task(**create_kwargs())
    second = service.create_task(**create_kwargs(operation_id="op-create-2"))
    assert second.replayed is True
    assert second.task_id == first.task_id  # no second task row was produced


def test_same_inputs_new_operation_id_replays(service: TaskService) -> None:
    first = service.create_task(**create_kwargs())
    replay = service.create_task(**create_kwargs(operation_id="op-create-3"))
    assert replay.task_id == first.task_id
    assert replay.replayed is True


def test_same_operation_different_inputs_conflicts(service: TaskService) -> None:
    service.create_task(**create_kwargs())
    with pytest.raises(DomainViolation) as error:
        service.create_task(
            **create_kwargs(
                operation_id="op-create-1",
                user_input=complete_user_input(goal=Grounded.observed("另一种目标")),
            )
        )
    assert error.value.code == "DOMAIN_INPUT_CONFLICT"
    # the conflicting attempt did not create anything
    replay = service.create_task(**create_kwargs(operation_id="op-create-9"))
    assert replay.task_id == "task-0001"


# ---------------------------------------------------------------------------
# Versions: frozen rounds
# ---------------------------------------------------------------------------


def test_version_round_appends_next_frozen_version(service: TaskService) -> None:
    created = service.create_task(**create_kwargs())
    outcome = service.add_version(
        **create_kwargs(
            operation_id="op-version-2",
            task_id=created.task_id,
            user_input=complete_user_input(goal=Grounded.observed("第二轮目标")),
        )
    )
    assert outcome.version == 2
    summary = service.task_summary(created.task_id)
    assert summary["version"] == 2  # current version is the highest submitted
    assert service.task_summary(created.task_id, 1)["version"] == 1


def test_repeated_version_round_replays(service: TaskService) -> None:
    created = service.create_task(**create_kwargs())
    first = service.add_version(
        **create_kwargs(operation_id="op-version-2", task_id=created.task_id)
    )
    replay = service.add_version(
        **create_kwargs(operation_id="op-version-3", task_id=created.task_id)
    )
    assert replay.replayed is True and replay.version == first.version


def test_unknown_version_lookup_refused(service: TaskService) -> None:
    created = service.create_task(**create_kwargs())
    with pytest.raises(DomainViolation) as error:
        service.task_summary(created.task_id, 7)
    assert error.value.code == "DOMAIN_UNKNOWN_VERSION"


# ---------------------------------------------------------------------------
# Contract shape: the summary satisfies the frozen task:summary definition
# ---------------------------------------------------------------------------


def test_summary_passes_frozen_contract_validation(service: TaskService) -> None:
    outcome = service.create_task(**create_kwargs())
    summary = service.task_summary(outcome.task_id)
    registry = ContractRegistry.load(CONTRACTS_DIR)
    violations = registry.validate(registry.definition_ref("task", "summary"), summary)
    assert violations == ()
    result = json.loads(json.dumps(summary))
    assert result["simulation"] is True


# ---------------------------------------------------------------------------
# Authorization: youth guardian, purpose scope, stale external digests
# ---------------------------------------------------------------------------


def test_consent_covers_only_named_purpose_for_exact_version(service: TaskService) -> None:
    consent = adult_consent("task-1")
    service.check_consent(
        consent=consent, purpose="analysis", task_id="task-1", version=1
    )  # granted: passes
    with pytest.raises(DomainViolation) as error:
        service.check_consent(consent=consent, purpose="retest", task_id="task-1", version=1)
    assert error.value.code == "DOMAIN_CONSENT_PURPOSE"


def test_version_mismatch_refuses(service: TaskService) -> None:
    consent = adult_consent("task-1", version=1)
    with pytest.raises(DomainViolation) as error:
        service.check_consent(consent=consent, purpose="analysis", task_id="task-1", version=2)
    assert error.value.code == "DOMAIN_CONSENT_SCOPE"
    with pytest.raises(DomainViolation) as error:
        service.check_consent(consent=consent, purpose="analysis", task_id="task-9", version=1)
    assert error.value.code == "DOMAIN_CONSENT_SCOPE"


def test_youth_without_guardian_confirmation_refused(service: TaskService) -> None:
    # The domain model refuses to even construct such a snapshot (BE-004);
    # the service-level guardian check stays as defense in depth.
    with pytest.raises(ValidationError, match="simulated guardian confirmation"):
        youth_consent("task-1", guardian_simulated=False)
    # with the simulated guardian confirmation the youth scope passes
    service.check_consent(
        consent=youth_consent("task-1"), purpose="analysis", task_id="task-1", version=1
    )


def test_external_purpose_invalidated_by_changed_recipient(service: TaskService) -> None:
    consent = external_consent("task-1")
    service.check_consent(
        consent=consent,
        purpose="share",
        task_id="task-1",
        version=1,
        recipient_digest="digest-recipient-1",
        materials_digest="digest-materials-1",
    )
    with pytest.raises(DomainViolation) as error:
        service.check_consent(
            consent=consent,
            purpose="share",
            task_id="task-1",
            version=1,
            recipient_digest="digest-recipient-2",
            materials_digest="digest-materials-1",
        )
    assert error.value.code == "DOMAIN_CONSENT_STALE"
    # presenting no digests at all is equally refused
    with pytest.raises(DomainViolation):
        service.check_consent(consent=consent, purpose="share", task_id="task-1", version=1)


def test_unconfirmed_consent_refused(service: TaskService) -> None:
    consent = adult_consent("task-1", confirmed=False, confirmed_at=None)
    with pytest.raises(DomainViolation) as error:
        service.check_consent(consent=consent, purpose="analysis", task_id="task-1", version=1)
    assert error.value.code == "DOMAIN_CONSENT_STALE"


# ---------------------------------------------------------------------------
# Withdrawal: exactly one purpose stops, others keep working (AC-SPEC-008)
# ---------------------------------------------------------------------------


def test_withdraw_one_purpose_leaves_other_working(service: TaskService) -> None:
    consent = adult_consent("task-1")
    withdrawn = service.withdraw_purpose(consent, "save")
    assert withdrawn.purposes == ["analysis"]
    service.check_consent(
        consent=withdrawn, purpose="analysis", task_id="task-1", version=1
    )  # still granted
    with pytest.raises(DomainViolation):
        service.check_consent(
            consent=withdrawn, purpose="save", task_id="task-1", version=1
        )  # stopped
    # the original snapshot is immutable
    assert consent.purposes == ["analysis", "save"]


def test_withdraw_unknown_or_last_purpose_refused(service: TaskService) -> None:
    consent = external_consent("task-1")
    with pytest.raises(DomainViolation):
        service.withdraw_purpose(consent, "analysis")
    with pytest.raises(DomainViolation):
        service.withdraw_purpose(consent, "share")  # the only purpose


# ---------------------------------------------------------------------------
# FR-004: no NTRP field, no goal interpretation
# ---------------------------------------------------------------------------


def test_no_ntrp_field_exists_on_user_input() -> None:
    with pytest.raises(ValueError):
        UserInput(
            **{
                **complete_user_input().model_dump(),
                "ntrp_self_assessment": Grounded.observed("3.0"),
            }
        )


def test_goal_text_is_stored_verbatim_never_interpreted(service: TaskService) -> None:
    raw = "我想在一个月内打到4.0并且学会上旋发球!!!"
    outcome = service.create_task(
        **create_kwargs(user_input=complete_user_input(goal=Grounded.observed(raw)))
    )
    # the service exposes no classification of the goal anywhere
    assert set(outcome.as_payload()) == {
        "task_id",
        "version",
        "state",
        "started",
        "missing_inputs",
        "replayed",
    }
    summary = service.task_summary(outcome.task_id)
    assert "goal" not in json.dumps(summary)
    assert outcome.started is True  # the goal text alone never blocks creation


# ---------------------------------------------------------------------------
# Runtime requirement invariant wired through the service
# ---------------------------------------------------------------------------


def test_runtime_required_without_media_refused(service: TaskService) -> None:
    with pytest.raises(ValueError, match="local capability must reference media"):
        service.create_task(**create_kwargs(runtime_requirement="required"))
