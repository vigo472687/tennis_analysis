"""Frozen-fixture analysis adapter (BE-016).

Reads the frozen fixture library (``fixtures/manifest.json`` plus the frozen
``fixtures/expected`` scenario files) and returns predefined structured
results for the strokes/timing/metrics stages through the shared typed
interface (BE-015), plus the quality report the orchestration batch (BE-017)
consumes.

Honesty rules (AC-SPEC-011/018/047/052, plan §13.2):

- The adapter never pretends to run a real vision model: every response is
  ``simulation=true`` (forced by the BE-015 base class) and carries the
  synthetic-source limitation.
- An unknown ``fixture_id`` is refused (``FIXTURE_UNKNOWN``) — nothing is
  invented for unlisted samples.
- A fixture whose frozen stroke kind is not one of the seven supported
  strokes is reported as ``kind="unable"`` with ``confidence="low"`` — an
  unknown action is never force-classified.
- Timing events only carry an ``original_s`` time when every object the
  frozen ``event_requires`` table names is visible in the sample; otherwise
  the event reports ``value_state="not_visible"`` with ``original_s=None``
  (missing objects are never interpolated back in).
- Metrics use the frozen relative definitions only; a metric whose reference
  phase is not in the fixture's phases reports
  ``value_state="insufficient_evidence"`` with ``value=None``.

All values are deterministic functions of the frozen files, so a re-run
yields byte-identical results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tennis_backend.analysis.adapters import (
    AdapterCapabilities,
    AnalysisAdapter,
    StageRequest,
)
from tennis_backend.application.worker import CancellationToken
from tennis_backend.domain.models import DomainViolation

_FIXTURE_UNKNOWN = "FIXTURE_UNKNOWN"

#: Frozen file names inside the fixture library root.
_MANIFEST = "manifest.json"
_ACTIONS = "expected/actions.json"
_VISIBILITY = "expected/visibility.json"

#: Frozen issue code → the ``media:quality`` object it concerns.
_ISSUE_OBJECTS: dict[str, str] = {
    "duration_exceeded": "media",
    "unreadable_media": "media",
    "panorama_requires_flat_export": "media",
    "time_mapping_invalid": "media",
    "missing_feet": "feet",
    "missing_ball": "ball",
    "missing_racket": "racket",
    "body_out_of_frame": "body",
    "occlusion": "body",
    "subject_too_small": "subject",
    "subject_selection_required": "subject",
    "direction_confirmation_required": "direction",
}

#: Fixed reshoot guidance per frozen issue code (synthetic scenarios only).
_ISSUE_GUIDANCE: dict[str, str] = {
    "duration_exceeded": "Trim the clip to 180 seconds or less and resubmit.",
    "unreadable_media": "Re-export a standard MP4/MOV file and resubmit.",
    "panorama_requires_flat_export": "Export a flat (non-panorama) video and resubmit.",
    "time_mapping_invalid": "Re-export the original video; timing cannot be mapped.",
    "missing_feet": "Frame both feet fully inside the view and reshoot.",
    "missing_ball": "Frame the ball trajectory inside the view and reshoot.",
    "missing_racket": "Keep the racket visible through the swing and reshoot.",
    "body_out_of_frame": "Frame the whole body inside the view and reshoot.",
    "occlusion": "Choose an angle without occlusion and reshoot.",
    "subject_too_small": "Move closer or zoom so the subject fills more of the frame.",
    "subject_selection_required": "Select which person to analyse before analysis.",
    "direction_confirmation_required": "Confirm the hitting direction (mirror check).",
}


@dataclass(frozen=True)
class FixtureLibrary:
    """The frozen fixture files, loaded once and served read-only."""

    manifest: dict[str, Any]
    actions: dict[str, Any]
    visibility: dict[str, Any]

    @classmethod
    def load(cls, root: Path) -> FixtureLibrary:
        manifest = cls._read(root / _MANIFEST)
        actions = cls._read(root / _ACTIONS)
        visibility = cls._read(root / _VISIBILITY)
        return cls(manifest=manifest, actions=actions, visibility=visibility)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            contents = json.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise DomainViolation(
                "FIXTURE_LIBRARY_MISSING", f"fixture file is missing: {path.name}"
            ) from error
        except ValueError as error:
            raise DomainViolation(
                "FIXTURE_LIBRARY_INVALID", f"fixture file is not valid JSON: {path.name}"
            ) from error
        if not isinstance(contents, dict):
            raise DomainViolation(
                "FIXTURE_LIBRARY_INVALID", f"fixture file is not an object: {path.name}"
            )
        return contents

    def sample(self, fixture_id: str) -> dict[str, Any]:
        candidates: list[Any] = list(self.manifest.get("samples", []))
        for candidate in candidates:
            record: dict[str, Any] = candidate
            if record.get("fixture_id") == fixture_id:
                return record
        raise DomainViolation(
            _FIXTURE_UNKNOWN, f"fixture {fixture_id} is not in the frozen library"
        )

    def action_case(self, fixture_id: str) -> dict[str, Any]:
        candidates: list[Any] = list(self.actions.get("cases", []))
        for candidate in candidates:
            record: dict[str, Any] = candidate
            if record.get("fixture_id") == fixture_id:
                return record
        raise DomainViolation(_FIXTURE_UNKNOWN, f"fixture {fixture_id} has no frozen action case")

    def visibility_case(self, fixture_id: str) -> dict[str, Any]:
        candidates: list[Any] = list(self.visibility.get("cases", []))
        for candidate in candidates:
            record: dict[str, Any] = candidate
            if record.get("fixture_id") == fixture_id:
                return record
        raise DomainViolation(
            _FIXTURE_UNKNOWN, f"fixture {fixture_id} has no frozen visibility case"
        )


class FixtureAnalysisAdapter(AnalysisAdapter):
    """Predefined fixture responses through the shared adapter interface."""

    def __init__(self, library: FixtureLibrary) -> None:
        super().__init__(kind="fixture", simulation=True)
        self._library = library
        self._supported = tuple(library.actions.get("supported_strokes", []))
        self._timing_order: list[str] = list(library.actions.get("timing_order", []))
        self._event_requires: dict[str, list[str]] = dict(library.actions.get("event_requires", {}))

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            kind="fixture",
            simulation=True,
            supported_stages=("strokes", "timing", "metrics"),
            limitations=(
                "fixed synthetic fixture responses, not a measured model",
                "no real pose, ball or racket detection",
                "all values are deterministic functions of the frozen fixture files",
            ),
        )

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    def run(
        self, stage: str, request: StageRequest, *, cancel: CancellationToken
    ) -> dict[str, Any]:
        if request.fixture_id is None:
            raise DomainViolation(_FIXTURE_UNKNOWN, "a fixture adapter requires a fixture_id")
        sample = self._library.sample(request.fixture_id)
        cancel.check_point()
        if stage == "strokes":
            return self._strokes(request, sample)
        if stage == "timing":
            return self._timing(request, sample)
        if stage == "metrics":
            return self._metrics(request, sample)
        raise DomainViolation("ADAPTER_STAGE_UNSUPPORTED", f"unknown stage {stage}")

    def _strokes(self, request: StageRequest, sample: dict[str, Any]) -> dict[str, Any]:
        case = self._library.action_case(request.fixture_id or "")
        frozen_kind = str(case.get("kind", "unable"))
        if frozen_kind in self._supported:
            kind: str = frozen_kind
            confidence = str(case.get("confidence", "low"))
            phases = [str(p) for p in case.get("phases", [])]
        else:
            # unknown action: never force-classified (AC-SPEC-018)
            kind = "unable"
            confidence = "low"
            phases = []
        limitations = ["synthetic fixture stroke window, not a measured action detection"]
        if kind == "unable":
            # surfaced through the adapter capabilities, never as fake data
            limitations.append("frozen sample action is not one of the seven supported strokes")
        return {
            "events": [
                {
                    "event_id": f"stroke-{request.task_id}-v{request.version}",
                    "kind": kind,
                    "start_s": float(case.get("start_s", 0.0)),
                    "end_s": float(case.get("end_s", 0.0)),
                    "phases": phases,
                    "confidence": confidence,
                    "evidence_ids": [],
                }
            ],
            "simulation": True,
        }

    def _timing(self, request: StageRequest, sample: dict[str, Any]) -> dict[str, Any]:
        case = self._library.action_case(request.fixture_id or "")
        visible = set(sample.get("visible_objects", []))
        start = float(case.get("start_s", 0.0))
        end = float(case.get("end_s", 1.0))
        phases = [str(p) for p in case.get("phases", [])]
        events: list[dict[str, Any]] = []
        for phase in phases:
            required = list(self._event_requires.get(phase, []))
            missing = [obj for obj in required if obj not in visible]
            event: dict[str, Any] = {
                "event_id": f"timing-{request.task_id}-v{request.version}-{phase}",
                "kind": phase,
                "requires": required,
                "evidence_ids": [],
            }
            if missing:
                # a required object is not visible: no time is invented
                event["original_s"] = None
                event["value_state"] = "not_visible"
            else:
                event["original_s"] = self._deterministic_time(phase, start, end)
                event["value_state"] = "observed"
            events.append(event)
        return {
            "events": events,
            "limitations": [
                "synthetic fixture timing; values are deterministic sample constants,",
                "not measured event detection",
            ],
            "simulation": True,
        }

    def _deterministic_time(self, phase: str, start: float, end: float) -> float:
        order = self._timing_order
        if phase in order and len(order) > 1:
            position = order.index(phase) / (len(order) - 1)
        else:
            position = 0.5
        return round(start + (end - start) * position, 3)

    def _metrics(self, request: StageRequest, sample: dict[str, Any]) -> dict[str, Any]:
        case = self._library.action_case(request.fixture_id or "")
        phases = [str(p) for p in case.get("phases", [])]
        start = float(case.get("start_s", 0.0))
        end = float(case.get("end_s", 1.0))
        metrics: list[dict[str, Any]] = []
        for definition in self._library.actions.get("relative_metric_definitions", []):
            reference = str(definition.get("reference", ""))
            value: float | str | None
            if reference in self._timing_order and reference in phases:
                value = self._deterministic_time(reference, start, end)
                value_state = "observed"
            else:
                value = None
                value_state = "insufficient_evidence"
            metrics.append(
                {
                    "metric_id": str(definition.get("metric_id", "metric")),
                    "definition": str(definition.get("definition", "")),
                    "value": value,
                    "value_state": value_state,
                    "unit": str(definition.get("unit", "relative")),
                    "reference": reference,
                    "evidence_ids": [],
                    "limitations": [str(item) for item in definition.get("limitations", [])]
                    + ["synthetic fixture value, not a measured physical quantity"],
                }
            )
        return {"metrics": metrics, "simulation": True}

    # ------------------------------------------------------------------
    # Quality report (consumed by the BE-017 orchestration batch)
    # ------------------------------------------------------------------

    def quality_report(self, request: StageRequest) -> dict[str, Any]:
        """Build the frozen ``media:quality`` shape for one fixture sample."""
        if request.fixture_id is None:
            raise DomainViolation(_FIXTURE_UNKNOWN, "a fixture adapter requires a fixture_id")
        sample = self._library.sample(request.fixture_id)
        case = self._library.visibility_case(request.fixture_id)
        issues: list[dict[str, Any]] = []
        for code in case.get("issue_codes", []):
            issues.append(
                {
                    "code": code,
                    "object": _ISSUE_OBJECTS.get(code, "media"),
                    "impact": _ISSUE_GUIDANCE.get(code, "blocks or limits this analysis"),
                    "evidence_ids": [],
                    "reshoot_guidance": _ISSUE_GUIDANCE.get(
                        code, "Review the listed object and reshoot if applicable."
                    ),
                }
            )
        media_summary = {
            "container": request.media.container,
            "codec": request.media.codec,
            "duration_s": request.media.duration_s,
            "fps": request.media.fps,
            "width": request.media.width,
            "height": request.media.height,
            "rotation": request.media.rotation,
            "slow_motion": request.media.slow_motion,
            "hdr": request.media.hdr,
            "device_source": request.media.device_source,
            "panorama": request.media.panorama,
            "support": "supported",  # only supported media reaches analysis
            "observation_kind": "measured",
        }
        return {
            "status": str(case.get("quality", "fail")),
            "issues": issues,
            "unavailable_objects": [str(o) for o in case.get("unavailable_objects", [])],
            "subject_candidates": [str(s) for s in sample.get("subject_candidates", [])],
            "time_mapping_valid": False,
            "media": media_summary,
            "evidence": [],
            "simulation": True,
        }
