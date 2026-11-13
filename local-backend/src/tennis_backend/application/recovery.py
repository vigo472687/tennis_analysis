"""Crash/timeout recovery with authorized stage reuse (BE-023).

After a crash or timeout, :func:`plan_recovery` decides — from durable state
only — how the task continues (AC-SPEC-045, AC-SPEC-008):

- **Resume from complete checkpoints**: every durable checkpoint is reused,
  incomplete stages are listed for redo; a re-render never re-runs completed
  media/analysis stages (``stage_actions`` maps each pipeline stage to
  ``reuse`` or ``redo``).
- **Input change voids reuse** (输入改变不能越权复用): the gate checkpoint
  digest stored by the pipeline (BE-017) is recomputed against the *current*
  fixture conditions; a mismatch means the interrupted run belonged to
  different inputs and nothing may be reused — the plan becomes a full
  restart with the ``input_changed`` refusal recorded.
- **Authorization change voids reuse** (授权改变不能越权复用): the caller
  presents the current consent snapshot; if it no longer covers ``analysis``
  for this exact task version (withdrawn purpose, unconfirmed, wrong
  version), reuse is refused. With no snapshot the caller asserts that
  authorization was handled upstream, recorded as a plan note.
- **No recovery for settled/terminal tasks**: complete/partial/unsupported/
  reshoot_required/safety_stop are finished (their results stand);
  ``cancelled`` stays cancelled until the user starts a new run;
  ``deleted`` is absolutely terminal.

The plan is a pure decision: executing it stays the pipeline's job (BE-017
resumes by skipping the stages marked ``reuse``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from tennis_backend.analysis.adapters import StageRequest
from tennis_backend.analysis.fixture import FixtureAnalysisAdapter
from tennis_backend.application.cancel import assert_derivative_writes_allowed
from tennis_backend.application.pipeline import gate_fingerprint
from tennis_backend.application.tasks import TaskService
from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.domain.models import ConsentSnapshot, DomainViolation, MediaProbe

_STAGE_ORDER: tuple[str, ...] = ("media", "analysis", "render")

_CHECKPOINTS_TO_STAGES: dict[str, str] = {
    # a durable gate checkpoint covers the input/media checking stage
    "checking_input": "media",
    # the analysis checkpoint (recorded when leaving analyzing) covers analysis
    "analyzing": "analysis",
    "researching": "analysis",
}

_INPUT_CHANGED = "RECOVERY_INPUT_CHANGED"
_NOT_AUTHORIZED = "RECOVERY_NOT_AUTHORIZED"


@dataclass(frozen=True)
class RecoveryPlan:
    """The durable-only decision for continuing one interrupted task."""

    task_id: str
    status: str  # "resume" | "restart" | "refused"
    state: str
    reused_checkpoints: tuple[str, ...]
    redo_stages: tuple[str, ...]
    stage_actions: dict[str, str]
    refusals: tuple[str, ...]
    notes: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "state": self.state,
            "reused_checkpoints": list(self.reused_checkpoints),
            "redo_stages": list(self.redo_stages),
            "stage_actions": dict(self.stage_actions),
            "refusals": list(self.refusals),
            "notes": list(self.notes),
        }


def plan_recovery(
    *,
    metadata: Any,
    service: TaskService,
    adapter: FixtureAnalysisAdapter,
    task_id: str,
    version: int,
    fixture_id: str,
    media: MediaProbe | None = None,
    consent: ConsentSnapshot | None = None,
    registry: ContractRegistry | None = None,
) -> RecoveryPlan:
    """Plan the continuation of one interrupted task from durable state."""
    state = metadata.task_state(task_id)
    if state is None:
        raise DomainViolation("UNKNOWN_TASK", f"task {task_id} is not stored")

    def plan(
        status: str,
        reused: tuple[str, ...],
        refusals: tuple[str, ...],
        notes: tuple[str, ...] = (),
    ) -> RecoveryPlan:
        incomplete = metadata.incomplete_stages(task_id)
        redo = tuple(stage for stage in incomplete if stage not in reused)
        actions: dict[str, str] = {}
        for stage in _STAGE_ORDER:
            covered = any(_CHECKPOINTS_TO_STAGES.get(cp) == stage for cp in reused)
            actions[stage] = "reuse" if covered else "redo"
        return RecoveryPlan(
            task_id=task_id,
            status=status,
            state=state,
            reused_checkpoints=reused,
            redo_stages=redo,
            stage_actions=actions,
            refusals=refusals,
            notes=notes
            or ("planned from durable checkpoints only; completed stages are not re-run",),
        )

    if state == "deleted":
        return plan("refused", (), ("task_deleted",), ("deleted data is never revived",))
    if state in ("complete", "partial", "reshoot_required", "unsupported", "safety_stop"):
        return plan(
            "refused",
            (),
            ("task_settled",),
            (f"task already finished as {state}; its results stand",),
        )
    if state == "cancelled":
        return plan(
            "refused",
            (),
            ("task_cancelled",),
            ("a cancelled task stays cancelled until a new run is started",),
        )
    if state == "failed":
        # failure recovery is legal only through explicit re-entry, which the
        # state machine gates; the plan reports what a re-entry may reuse
        assert_derivative_writes_allowed(metadata, task_id)

    checkpoints = {c.name: c for c in metadata.checkpoints(task_id)}
    refusals: list[str] = []
    recovery_notes: tuple[str, ...] = ()

    # authorization check before any reuse decision: a withdrawn/invalid
    # consent voids reuse regardless of which checkpoints exist
    if consent is not None:
        if registry is None:
            # a presented consent that cannot be verified must never be
            # silently skipped (BE-023 review)
            raise DomainViolation(
                _NOT_AUTHORIZED,
                "a consent snapshot was presented without the contract registry"
                " needed to verify it",
            )
        try:
            service.check_consent(
                consent=consent, purpose="analysis", task_id=task_id, version=version
            )
        except DomainViolation:
            return plan(
                "refused",
                (),
                (_NOT_AUTHORIZED,),
                (
                    "presented consent no longer covers analysis for this task version;"
                    " re-confirm authorization before resuming",
                ),
            )
    else:
        recovery_notes = (
            "no consent snapshot presented; the caller asserts authorization upstream",
        )

    gate = checkpoints.get("checking_input")
    reused: tuple[str, ...] = ()
    if gate is not None:
        # input check: recompute the gate fingerprint over the CURRENT
        # fixture conditions with the shared fixture-bound digest; a
        # mismatch proves the interrupted run used different inputs and
        # nothing may be reused (quality-identical fixtures still collide
        # without the fixture identity — BE-023 review blocking finding)
        current_quality = adapter.quality_report(
            StageRequest(
                task_id=task_id,
                version=version,
                fixture_id=fixture_id,
                media=media or _neutral_probe(),
            )
        )
        if gate_fingerprint(fixture_id, current_quality) != gate.payload_digest:
            refusals.append(_INPUT_CHANGED)
            return plan(
                "restart",
                (),
                tuple(refusals),
                (
                    "stored checkpoint belongs to different inputs; every stage restarts"
                    " and nothing is reused",
                ),
            )
        reused = ("checking_input",)

    if not checkpoints:
        return plan(
            "restart",
            (),
            tuple(refusals),
            (
                "no durable checkpoints; every stage restarts",
                *recovery_notes,
            ),
        )
    planned = plan("resume", reused, tuple(refusals))
    if recovery_notes:
        planned = replace(planned, notes=(*planned.notes, *recovery_notes))
    return planned


def _neutral_probe() -> MediaProbe:
    """The same neutral metadata stand-in the BE-017 gate records."""
    return MediaProbe(
        container="mp4",
        codec="h264",
        duration_s=3.0,
        fps=30.0,
        width=1920,
        height=1080,
        rotation="0",
        slow_motion="unknown",
        hdr="no",
        device_source="unknown",
        panorama="flat",
        observation_kind="measured",
    )
