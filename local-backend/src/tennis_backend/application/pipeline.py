"""Quality-first analysis pipeline with checkpoints and partial analysis (BE-017).

Composes the B2 building blocks into one orchestration: compatibility comes
first (BE-016 quality report), every completed stage leaves a durable
checkpoint (BE-005/BE-006), reruns replay instead of repeating (BE-008), and
results are gated by what the media actually shows (AC-SPEC-010/011/013/014/
015/019/045/046/047/052):

- **Compatibility first** — a failed quality gate blocks the task before any
  analysis: unsupported media (overlong, damaged, panorama, unknown stroke,
  broken time mapping) ends ``unsupported``; reshoot-level defects (subject
  too small, body out of frame) end ``reshoot_required``.
- **Pause instead of guessing** — multiple subjects pause the task until the
  user picks one (candidates are listed, never chosen for them, AC-SPEC-046);
  a mirror conflict pauses until the hitting direction is confirmed.
- **Partial analysis with honest gating** — missing feet disable footwork,
  support and leg-drive conclusions; a missing ball disables bounce/landing
  and ball-timing judgments; a missing racket disables racket-head
  conclusions; an out-of-frame body or feet means no full kinetic-chain
  conclusion. Reliable items and undecidable items are listed separately
  (AC-SPEC-019); the adapter's own not-visible timing events are the source
  of truth — the pipeline never re-enables a gated item.
- **Checkpoints and replay** — the quality gate result is stored as the
  ``checking_input`` checkpoint inside the state transition; a rerun of a
  settled operation replays the stored outcome, and a task that already
  carries the checkpoint skips re-running the gate (AC-SPEC-045). The
  ``analyzing`` checkpoint is recorded by the later render batch (BE-020)
  when the task leaves the ``analyzing`` state.

The pipeline runs inside the caller's thread (a BE-012 worker job), honours
the cancellation token between stages, and leaves the task in ``analyzing``
with results in the outcome; the render batch drives the final terminal
transition.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tennis_backend.analysis.adapters import StageRequest, run_stage
from tennis_backend.analysis.fixture import FixtureAnalysisAdapter
from tennis_backend.application.idempotency import OperationLedger
from tennis_backend.application.tasks import TaskService
from tennis_backend.application.worker import CancellationToken
from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.domain.models import DomainViolation, MediaProbe, TaskState
from tennis_backend.storage.metadata import MetadataStore

PIPELINE_METHOD = "analysis.create"

#: Issue codes that block with ``unsupported`` (outside the prototype's
#: supported range: format, action or scene).
_UNSUPPORTED_CODES = frozenset(
    {
        "duration_exceeded",
        "unreadable_media",
        "panorama_requires_flat_export",
        "unsupported_stroke",
    }
)

#: Issue codes that block with ``reshoot_required`` (fixable by re-shooting
#: or re-exporting: spec state table semantics and EX-043 recovery exits —
#: a broken time mapping is a media-quality defect, not out of range, and
#: the frozen manifest expects reshoot_required for bad-time-mapping).
_RESHOOT_CODES = frozenset({"subject_too_small", "body_out_of_frame", "time_mapping_invalid"})

#: Issue codes that pause the task awaiting the user (never guessed through).
_PAUSE_CODES = frozenset({"subject_selection_required", "direction_confirmation_required"})


def gate_fingerprint(fixture_id: str, quality: dict[str, Any]) -> str:
    """The authoritative gate-checkpoint digest, shared with BE-023 recovery.

    The digest binds the quality report to the fixture identity that
    produced it: quality-identical fixtures (several frozen samples share
    one quality report) must not collide, or recovery would wrongly reuse a
    checkpoint across different inputs (BE-023 review, blocking finding).
    Legacy digests recorded before this binding have no production data and
    were re-established by re-running the pipelines in this development
    store.
    """
    payload = {"fixture_id": fixture_id, "quality": quality}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


#: Object-specific capability gating (AC-SPEC-013/014/015 and FR-032/039).
_OBJECT_GATE: dict[str, tuple[str, ...]] = {
    "feet": (
        "footwork_evaluation",
        "support_and_leg_drive",
        "step_timing",
    ),
    "ball": (
        "bounce_and_landing",
        "incoming_ball_timing",
        "precise_contact_moment",
    ),
    "racket": (
        "racket_head_path",
        "racket_drop",
    ),
    "body": ("full_kinetic_chain",),
}


@dataclass(frozen=True)
class PipelineOutcome:
    """One pipeline run: the task's state, results and honest gating."""

    status: str  # "analyzed" | "paused" | "blocked"
    task_state: str
    stage_results: dict[str, dict[str, Any]]
    checkpoints: tuple[str, ...]
    reliable: tuple[str, ...]
    undecidable: tuple[str, ...]
    disabled: tuple[dict[str, str], ...]
    pending: dict[str, Any] | None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "task_state": self.task_state,
            "stage_results": self.stage_results,
            "checkpoints": list(self.checkpoints),
            "reliable": list(self.reliable),
            "undecidable": list(self.undecidable),
            "disabled": [dict(item) for item in self.disabled],
            "pending": self.pending,
            "notes": list(self.notes),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PipelineOutcome:
        return cls(
            status=str(payload["status"]),
            task_state=str(payload["task_state"]),
            stage_results=dict(payload["stage_results"]),
            checkpoints=tuple(payload["checkpoints"]),
            reliable=tuple(payload["reliable"]),
            undecidable=tuple(payload["undecidable"]),
            disabled=tuple(dict(item) for item in payload["disabled"]),
            pending=payload["pending"],
            notes=tuple(payload["notes"]),
        )


class AnalysisPipeline:
    """Quality-first, checkpointed fixture analysis orchestration."""

    def __init__(
        self,
        *,
        service: TaskService,
        metadata: MetadataStore,
        ledger: OperationLedger,
        adapter: FixtureAnalysisAdapter,
        registry: ContractRegistry,
    ) -> None:
        self._service = service
        self._metadata = metadata
        self._ledger = ledger
        self._adapter = adapter
        self._registry = registry

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        operation_id: str,
        task_id: str,
        version: int,
        fixture_id: str,
        cancel: CancellationToken,
        at: datetime,
    ) -> PipelineOutcome:
        """Run compatibility-first analysis for one task version.

        A settled operation replays its stored outcome; a task that already
        carries the ``checking_input`` checkpoint skips re-running the gate.
        """
        outcome = self._ledger.begin(
            operation_id,
            PIPELINE_METHOD,
            {"task_id": task_id, "version": version, "fixture_id": fixture_id},
            at=at,
        )
        if outcome.replayed:
            return PipelineOutcome.from_payload(json.loads(outcome.record.result_json or "{}"))

        record = self._metadata.task_record(task_id)
        current_state: TaskState = record.state
        if current_state not in ("draft", "checking_input", "analyzing"):
            raise DomainViolation(
                "PIPELINE_STATE", f"task {task_id} is {current_state}; pipeline cannot run"
            )
        if current_state == "draft":
            self._metadata.record_transition(
                task_id, current="draft", target="checking_input", reason="pipeline_start", at=at
            )
            current_state = "checking_input"

        existing_checkpoints = tuple(c.name for c in self._metadata.checkpoints(task_id))
        gated = self._run_quality_gate(
            task_id, version, fixture_id, current_state, existing_checkpoints, at
        )
        if gated is not None:
            self._ledger.succeed(operation_id, json.dumps(gated.to_payload()), at=at)
            return gated

        stage_results = self._run_analysis_stages(task_id, version, fixture_id, cancel)
        reliable, undecidable, disabled = self._gate_results(fixture_id, stage_results)
        result = PipelineOutcome(
            status="analyzed",
            task_state="analyzing",
            stage_results=stage_results,
            checkpoints=existing_checkpoints + ("checking_input",),
            reliable=reliable,
            undecidable=undecidable,
            disabled=disabled,
            pending=None,
            notes=(),
        )
        self._ledger.succeed(operation_id, json.dumps(result.to_payload()), at=at)
        return result

    # ------------------------------------------------------------------
    # Stage 1: compatibility gate (checkpointed)
    # ------------------------------------------------------------------

    def _run_quality_gate(
        self,
        task_id: str,
        version: int,
        fixture_id: str,
        current_state: TaskState,
        existing_checkpoints: tuple[str, ...],
        at: datetime,
    ) -> PipelineOutcome | None:
        """Decide compatibility; returns a terminal/paused outcome or None."""
        if "checking_input" in existing_checkpoints:
            return None  # resume: the gate result is already durable
        quality = self._adapter.quality_report(
            StageRequest(
                task_id=task_id, version=version, fixture_id=fixture_id, media=self._gate_media()
            )
        )
        issues = [str(issue["code"]) for issue in quality["issues"]]

        pause_codes = [code for code in issues if code in _PAUSE_CODES]
        if pause_codes:
            pending = self._pending_from(quality, pause_codes)
            return PipelineOutcome(
                status="paused",
                task_state="checking_input",
                stage_results={},
                checkpoints=existing_checkpoints,
                reliable=(),
                undecidable=(),
                disabled=(),
                pending=pending,
                notes=("awaiting_user",),
            )

        unsupported = [code for code in issues if code in _UNSUPPORTED_CODES]
        reshoot = [code for code in issues if code in _RESHOOT_CODES]
        if quality["status"] == "fail" and not unsupported and not reshoot:
            unsupported = ["quality_failed"]
        if unsupported or reshoot:
            target = "unsupported" if unsupported else "reshoot_required"
            self._metadata.record_transition(
                task_id,
                current=current_state,
                target=target,  # type: ignore[arg-type]
                reason=",".join(unsupported or reshoot),
                at=at,
            )
            return PipelineOutcome(
                status="blocked",
                task_state=target,
                stage_results={},
                checkpoints=existing_checkpoints,
                reliable=(),
                undecidable=(),
                disabled=(),
                pending=None,
                notes=tuple(unsupported or reshoot),
            )

        digest = gate_fingerprint(fixture_id, quality)
        self._metadata.record_transition(
            task_id,
            current=current_state,
            target="analyzing",
            reason="compatibility_passed",
            checkpoint="checking_input",
            checkpoint_digest=digest,
            at=at,
        )
        return None

    def _gate_media(self) -> MediaProbe:
        """A neutral probe stand-in for the fixture gate (metadata only)."""
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

    def _pending_from(self, quality: dict[str, Any], pause_codes: list[str]) -> dict[str, Any]:
        if "subject_selection_required" in pause_codes:
            return {
                "type": "subject_selection",
                "candidates": list(quality.get("subject_candidates", [])),
                "note": "select one person before individual analysis;"
                " evidence will bind to that subject",
            }
        return {
            "type": "direction_confirmation",
            "note": "confirm the hitting direction (mirror check) before analysis",
        }

    # ------------------------------------------------------------------
    # Stage 2: analysis stages through the validated adapter interface
    # ------------------------------------------------------------------

    def _run_analysis_stages(
        self, task_id: str, version: int, fixture_id: str, cancel: CancellationToken
    ) -> dict[str, dict[str, Any]]:
        request = StageRequest(
            task_id=task_id, version=version, fixture_id=fixture_id, media=self._gate_media()
        )
        results: dict[str, dict[str, Any]] = {}
        for stage in ("strokes", "timing", "metrics"):
            cancel.check_point()
            results[stage] = run_stage(
                self._adapter, stage, request, cancel=cancel, registry=self._registry
            )
        return results

    # ------------------------------------------------------------------
    # Stage 3: honest gating of what the media actually shows
    # ------------------------------------------------------------------

    def _gate_results(
        self, fixture_id: str, stage_results: dict[str, dict[str, Any]]
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[dict[str, str], ...]]:
        unavailable = self._unavailable_objects(fixture_id)
        disabled: list[dict[str, str]] = []
        reliable: list[str] = ["stroke_window"]
        undecidable: list[str] = []

        seen_objects: set[str] = set()
        for obj in unavailable:
            for capability in _OBJECT_GATE.get(obj, ()):
                if capability not in seen_objects:
                    disabled.append(
                        {"capability": capability, "reason": f"{obj}_not_visible", "objects": obj}
                    )
                    seen_objects.add(capability)

        undecidable_from_timing = sorted(
            {
                event["kind"]
                for event in stage_results.get("timing", {}).get("events", [])
                if event.get("value_state") != "observed"
            }
        )
        undecidable.extend(f"timing:{kind}" for kind in undecidable_from_timing)
        undecidable.extend(
            f"metric:{metric['metric_id']}"
            for metric in stage_results.get("metrics", {}).get("metrics", [])
            if metric.get("value_state") != "observed"
        )

        stroke = stage_results.get("strokes", {}).get("events", [{}])[0]
        if stroke.get("kind") == "unable":
            reliable = []
            undecidable.insert(0, "stroke_classification")
        reliable.extend(
            f"timing:{event['kind']}"
            for event in stage_results.get("timing", {}).get("events", [])
            if event.get("value_state") == "observed"
        )
        reliable.extend(
            f"metric:{metric['metric_id']}"
            for metric in stage_results.get("metrics", {}).get("metrics", [])
            if metric.get("value_state") == "observed"
        )
        return tuple(sorted(set(reliable))), tuple(sorted(set(undecidable))), tuple(disabled)

    def _unavailable_objects(self, fixture_id: str) -> tuple[str, ...]:
        quality = self._adapter.quality_report(
            StageRequest(task_id="gate", version=1, fixture_id=fixture_id, media=self._gate_media())
        )
        return tuple(str(obj) for obj in quality.get("unavailable_objects", []))
