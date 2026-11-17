"""Task application service: create, lookup, versions and authorization (BE-011).

The service composes the B1 building blocks — ``MetadataStore`` (BE-006),
``OperationLedger`` (BE-008), the task/version/consent domain models (BE-004)
and the state machine (BE-005) — into the three behaviors this batch owns:

1. **Create and version** — a task starts in ``draft``/``input`` with one
   frozen version; a new round becomes a new version under the same task.
   Every create/version attempt is bound to an idempotency operation, so a
   repeated operation id or identical inputs replay the stored outcome and
   never produce a duplicate task, version or side effect (EX-005).
2. **Gap listing instead of analysis** — required declared inputs keep their
   absence states; a create with gaps still stores the draft but reports the
   gaps and ``started=False`` (AC-SPEC-003: list the gaps, do not analyze).
   There is no NTRP field anywhere and the service never interprets the
   user's goal text (FR-004).
3. **Current authorization** — a consent snapshot is only valid for the exact
   task version it names, one purpose never extends to another, a youth
   scope needs the simulated guardian confirmation, and an external purpose
   needs the same recipient and materials digests it was confirmed for
   (FR-005/FR-006, AC-SPEC-007/008). ``withdraw_purpose`` rebuilds a snapshot
   without exactly one purpose, leaving the others untouched.

Threading note (AG-B02): SQLite connections have thread affinity. The stores
handed to this service must be created on the same thread that calls it —
under the NDJSON transport (BE-012) that is the worker thread, per call.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tennis_backend.application.idempotency import OperationLedger
from tennis_backend.domain.models import (
    ConsentPurpose,
    ConsentSnapshot,
    DigestRef,
    DomainViolation,
    Task,
    TaskVersion,
    UserInput,
)
from tennis_backend.domain.states import assert_state_stage_consistent
from tennis_backend.storage.metadata import MetadataStore

#: Ledger method bound to task creation and version rounds. The shared
#: contract binds ``analysis.create`` to ``task:create_input``; version rounds
#: reuse the same wire method, so the ledger binds them under it too.
CREATE_METHOD = "analysis.create"

_CONTRACT_VERSION = "2.0.0"

#: Stable codes raised as :class:`DomainViolation` (prefix added by the base).
_GAPS_CODE = "INPUT_INCOMPLETE"
_CONSENT_SCOPE = "CONSENT_SCOPE"
_CONSENT_PURPOSE = "CONSENT_PURPOSE"
_GUARDIAN = "GUARDIAN_CONFIRMATION_REQUIRED"
_CONSENT_STALE = "CONSENT_STALE"


@dataclass(frozen=True)
class StartOutcome:
    """What one create or version-round attempt produced."""

    task_id: str
    version: int
    state: str
    started: bool
    missing_inputs: tuple[str, ...]
    replayed: bool

    def as_payload(self) -> dict[str, Any]:
        """JSON-safe shape stored in the ledger for exact replay."""
        return {
            "task_id": self.task_id,
            "version": self.version,
            "state": self.state,
            "started": self.started,
            "missing_inputs": list(self.missing_inputs),
            "replayed": False,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> StartOutcome:
        return cls(
            task_id=str(payload["task_id"]),
            version=int(payload["version"]),
            state=str(payload["state"]),
            started=bool(payload["started"]),
            missing_inputs=tuple(payload["missing_inputs"]),
            replayed=True,
        )


class TaskService:
    """Create/lookup task versions and check current purpose authorization."""

    def __init__(
        self,
        metadata: MetadataStore,
        ledger: OperationLedger,
        *,
        task_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._metadata = metadata
        self._ledger = ledger
        self._task_id_factory = task_id_factory or (lambda: f"task-{uuid.uuid4().hex[:12]}")

    # ------------------------------------------------------------------
    # Create and version rounds
    # ------------------------------------------------------------------

    def create_task(
        self,
        *,
        operation_id: str,
        fixture_id: str | None = None,
        user_input: UserInput,
        primary_route: str,
        auxiliary_routes: list[str],
        runtime_requirement: str = "not_required",
        media_id: str | None = None,
        consent_id: str | None = None,
        subject_id: str | None = None,
        config_version: str = "0",
        simulation: bool = True,
        at: datetime,
    ) -> StartOutcome:
        """Start a new task as ``draft``/``input`` with frozen version 1.

        Gaps do not block storage — they block analysis: the outcome lists the
        missing declared inputs and ``started=False`` (AC-SPEC-003).
        """
        outcome = self._ledger.begin(
            operation_id,
            CREATE_METHOD,
            self._create_inputs(
                fixture_id=fixture_id,
                user_input=user_input,
                primary_route=primary_route,
                auxiliary_routes=auxiliary_routes,
                runtime_requirement=runtime_requirement,
                media_id=media_id,
                consent_id=consent_id,
                subject_id=subject_id,
                config_version=config_version,
                simulation=simulation,
            ),
            at=at,
        )
        if outcome.replayed:
            return StartOutcome.from_payload(json.loads(outcome.record.result_json or "{}"))
        task_id = self._task_id_factory()
        version = self._build_version(
            task_id=task_id,
            version=1,
            user_input=user_input,
            primary_route=primary_route,
            auxiliary_routes=auxiliary_routes,
            runtime_requirement=runtime_requirement,
            media_id=media_id,
            consent_id=consent_id,
            subject_id=subject_id,
            config_version=config_version,
            simulation=simulation,
            input_digest=outcome.record.input_digest,
        )
        task = Task(
            task_id=task_id,
            versions=[version],
            state="draft",
            stage="input",
            simulation=simulation,
        )
        assert_state_stage_consistent(task.state, task.stage)
        self._metadata.create_task(task, at=at)
        missing = user_input.missing_inputs()
        payload = StartOutcome(
            task_id=task_id,
            version=1,
            state="draft",
            started=not missing,
            missing_inputs=missing,
            replayed=False,
        )
        self._ledger.succeed(
            operation_id,
            json.dumps(payload.as_payload()),
            at=at,
        )
        return payload

    def add_version(
        self,
        *,
        operation_id: str,
        task_id: str,
        fixture_id: str | None = None,
        user_input: UserInput,
        primary_route: str,
        auxiliary_routes: list[str],
        runtime_requirement: str = "not_required",
        media_id: str | None = None,
        consent_id: str | None = None,
        subject_id: str | None = None,
        config_version: str = "0",
        simulation: bool = True,
        at: datetime,
    ) -> StartOutcome:
        """Append the next frozen version to an existing task."""
        record = self._metadata.task_record(task_id)
        next_number = max((v.version for v in record.versions), default=0) + 1
        outcome = self._ledger.begin(
            operation_id,
            CREATE_METHOD,
            self._create_inputs(
                task_id=task_id,
                fixture_id=fixture_id,
                user_input=user_input,
                primary_route=primary_route,
                auxiliary_routes=auxiliary_routes,
                runtime_requirement=runtime_requirement,
                media_id=media_id,
                consent_id=consent_id,
                subject_id=subject_id,
                config_version=config_version,
                simulation=simulation,
            ),
            at=at,
        )
        if outcome.replayed:
            return StartOutcome.from_payload(json.loads(outcome.record.result_json or "{}"))
        version = self._build_version(
            task_id=task_id,
            version=next_number,
            user_input=user_input,
            primary_route=primary_route,
            auxiliary_routes=auxiliary_routes,
            runtime_requirement=runtime_requirement,
            media_id=media_id,
            consent_id=consent_id,
            subject_id=subject_id,
            config_version=config_version,
            simulation=simulation,
            input_digest=outcome.record.input_digest,
        )
        self._metadata.add_task_version(version, at=at)
        missing = user_input.missing_inputs()
        payload = StartOutcome(
            task_id=task_id,
            version=next_number,
            state=self._metadata.task_state(task_id) or "draft",
            started=not missing,
            missing_inputs=missing,
            replayed=False,
        )
        self._ledger.succeed(operation_id, json.dumps(payload.as_payload()), at=at)
        return payload

    def _create_inputs(self, **inputs: Any) -> dict[str, Any]:
        """Canonical ledger inputs: exact model dumps, no informal fields."""
        user_input: UserInput = inputs["user_input"]
        return {
            "task_id": inputs.get("task_id"),
            "fixture_id": inputs["fixture_id"],
            "user_input": user_input.model_dump(mode="json"),
            "primary_route": inputs["primary_route"],
            "auxiliary_routes": list(inputs["auxiliary_routes"]),
            "runtime_requirement": inputs["runtime_requirement"],
            "media_id": inputs["media_id"],
            "consent_id": inputs["consent_id"],
            "subject_id": inputs["subject_id"],
            "config_version": inputs["config_version"],
            "simulation": inputs["simulation"],
        }

    def _build_version(
        self,
        *,
        task_id: str,
        version: int,
        user_input: UserInput,
        primary_route: str,
        auxiliary_routes: list[str],
        runtime_requirement: str,
        media_id: str | None,
        consent_id: str | None,
        subject_id: str | None,
        config_version: str,
        simulation: bool,
        input_digest: str,
    ) -> TaskVersion:
        return TaskVersion(
            task_id=task_id,
            version=version,
            primary_route=primary_route,  # type: ignore[arg-type]
            auxiliary_routes=auxiliary_routes,  # type: ignore[arg-type]
            runtime_requirement=runtime_requirement,  # type: ignore[arg-type]
            contract_version=_CONTRACT_VERSION,
            config_version=config_version,
            media_id=media_id,
            consent_id=consent_id,
            subject_id=subject_id,
            input_digest=input_digest,
            simulation=simulation,
        )

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def task_summary(self, task_id: str, version: int | None = None) -> dict[str, Any]:
        """Frozen ``task:summary`` shape for the requested (or current) version."""
        record = self._metadata.task_record(task_id)
        numbers = [v.version for v in record.versions]
        if version is None:
            version = max(numbers)
        elif version not in numbers:
            raise DomainViolation("UNKNOWN_VERSION", f"task {task_id} has no version {version}")
        assert_state_stage_consistent(record.state, record.stage)
        return {
            "task_id": task_id,
            "version": version,
            "state": record.state,
            "stage": record.stage,
            "checkpoints": [c.name for c in record.checkpoints],
            "retained": [],
            "recovery_actions": [],
            "workspace_ref": f"projects/{task_id}/v{version}",
            "simulation": record.simulation,
        }

    # ------------------------------------------------------------------
    # Current authorization (FR-005/FR-006, AC-SPEC-007/008)
    # ------------------------------------------------------------------

    def check_consent(
        self,
        *,
        consent: ConsentSnapshot,
        purpose: ConsentPurpose,
        task_id: str,
        version: int,
        recipient_digest: DigestRef | None = None,
        materials_digest: DigestRef | None = None,
    ) -> None:
        """Refuse unless this snapshot grants exactly ``purpose`` right now.

        The snapshot must name the task and version being operated on (a
        confirmation covers exactly its task version, never another); a youth
        scope needs the simulated guardian confirmation; an external purpose
        needs the same recipient and materials digests as at confirmation
        time, so any change invalidates the old confirmation.
        """
        if consent.task_id != task_id or consent.version != version:
            raise DomainViolation(
                _CONSENT_SCOPE,
                f"consent {consent.consent_id} covers {consent.task_id} v{consent.version},"
                f" not {task_id} v{version}",
            )
        if not consent.covers(
            purpose,
            recipient_digest=recipient_digest,
            materials_digest=materials_digest,
        ):
            raise DomainViolation(
                self._consent_code(consent, purpose), self._consent_reason(consent, purpose)
            )

    def withdraw_purpose(
        self, consent: ConsentSnapshot, purpose: ConsentPurpose
    ) -> ConsentSnapshot:
        """Return a snapshot without exactly ``purpose``; others keep working.

        The snapshot is immutable, so withdrawal produces a fresh snapshot;
        withdrawing a purpose the snapshot never granted, or the last granted
        purpose, is refused.
        """
        if purpose not in consent.purposes:
            raise DomainViolation(
                _CONSENT_PURPOSE, f"consent {consent.consent_id} does not grant {purpose}"
            )
        if len(consent.purposes) == 1:
            raise DomainViolation(
                _CONSENT_PURPOSE,
                f"consent {consent.consent_id} grants only {purpose}; withdraw the consent instead",
            )
        remaining = [p for p in consent.purposes if p != purpose]
        return ConsentSnapshot(
            consent_id=consent.consent_id,
            task_id=consent.task_id,
            version=consent.version,
            purposes=remaining,
            text_version=consent.text_version,
            confirmed=consent.confirmed,
            guardian_required=consent.guardian_required,
            guardian_simulated=consent.guardian_simulated,
            confirmed_at=consent.confirmed_at,
            recipient_digest=consent.recipient_digest,
            materials_digest=consent.materials_digest,
        )

    def _consent_code(self, consent: ConsentSnapshot, purpose: ConsentPurpose) -> str:
        if not consent.confirmed:
            return _CONSENT_STALE
        if consent.guardian_required and not consent.guardian_simulated:
            return _GUARDIAN
        if purpose not in consent.purposes:
            return _CONSENT_STALE if purpose in ("share", "email") else _CONSENT_PURPOSE
        return _CONSENT_STALE  # granted external purpose: digests did not match

    def _consent_reason(self, consent: ConsentSnapshot, purpose: ConsentPurpose) -> str:
        if not consent.confirmed:
            return f"consent {consent.consent_id} is not confirmed"
        if consent.guardian_required and not consent.guardian_simulated:
            return (
                "youth scope needs the simulated guardian confirmation for"
                f" {consent.task_id} v{consent.version}"
            )
        if purpose not in consent.purposes:
            return f"consent {consent.consent_id} does not grant {purpose}"
        return (
            f"external purpose {purpose} needs unchanged recipient and materials"
            " digests; re-confirmation required"
        )
