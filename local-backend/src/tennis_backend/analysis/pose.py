"""Real pose analysis adapter (Phase 4) — the BE-015 interface, realized.

Reads the ALREADY published per-task records (pose-frames.jsonl,
motion-angles.jsonl, motion-trajectories.json, action-selection.json,
segment-states.json) and answers the frozen query stages honestly:

- **strokes** — ``events=[]``: V1 is pose-only. Without a detected ball or
  racket there is no basis for a stroke-kind classification, so nothing is
  classified rather than guessing (FR-POSE-004). The explanation is carried
  in this module's limitations and the task's ``analysis-notes.json`` record
  (the frozen ``strokes_result`` shape has no limitations field).
- **timing** — the eleven frozen event kinds are each reported with their
  frozen ``requires`` objects: ball/racket requirements are
  ``value_state="not_visible"`` (V1 never detects them); body/feet
  requirements are ``value_state="insufficient_evidence"`` (V1 has no
  validated phase detector). ``original_s`` is always ``None`` — no phase
  time is invented (FR-POSE-004).
- **metrics** — the first real measurement batch derived from published
  motion records: joint-angle ranges (2D image-plane proxy), wrist
  trajectory envelopes (body-size normalized), and action-segment
  duration/gap. A metric with fewer than :data:`MIN_VALID_FRAMES` usable
  frames reports ``insufficient_evidence`` with ``value=None`` instead of a
  thin number.

Honesty boundaries (FR-POSE-006 and the grounded-value rules):

- the adapter is constructed with ``simulation=False`` (forced by the
  BE-015 base) and every result validates against the frozen contracts;
- a caller asking for stages without the published records gets the stable
  ``POSE_RECORDS_MISSING`` refusal from the wiring — the adapter itself
  treats empty records as an honest "nothing measurable" input, never as a
  reason to fall back to fixture data;
- every metric is camera-dependent and relative to this clip only; no
  physical force, no professional ratings, no cross-session comparability
  claims.
"""

from __future__ import annotations

import statistics
from typing import Any

from tennis_backend.analysis.adapters import (
    AdapterCapabilities,
    AnalysisAdapter,
    StageRequest,
)
from tennis_backend.application.worker import CancellationToken
from tennis_backend.domain.models import DomainViolation, MediaAsset
from tennis_backend.evidence.catalog import original_time

#: Minimum frames/points a derived value needs before it is reported as
#: ``observed``; below this the metric is ``insufficient_evidence``.
MIN_VALID_FRAMES = 3

_RECORDS_MISSING = "POSE_RECORDS_MISSING"

#: The eleven frozen timing kinds with the objects each requires (the frozen
#: fixture table ``fixtures/expected/actions.json → event_requires``). Kept in
#: one place so the real adapter answers with the same requires as the
#: frozen samples do.
EVENT_REQUIRES: dict[str, tuple[str, ...]] = {
    "preparation": ("body",),
    "first_step": ("feet",),
    "main_movement": ("feet",),
    "moving_backswing": ("body",),
    "bounce_candidate": ("ball",),
    "adjustment": ("feet",),
    "support": ("feet",),
    "racket_drop": ("racket",),
    "leg_drive_rotation": ("feet", "body"),
    "forward_swing": ("body",),
    "recovery": ("feet",),
}

_LIMITATIONS: tuple[str, ...] = (
    "all values are 2D image-plane proxies from body keypoints; "
    "camera-position dependent, not 3D biomechanics or physical force",
    "no ball or racket detection in V1; strokes are not classified and "
    "ball/racket timing events are not_visible",
    "values are relative to this clip only; not comparable across camera "
    "setups or sessions without the comparability gate",
    "no professional ratings, levels or medical conclusions",
)


class RealPoseAdapter(AnalysisAdapter):
    """Analysis adapter over published real pose records (simulation=False)."""

    def __init__(
        self,
        *,
        pose_records: list[dict[str, Any]],
        motion: dict[str, Any],
        selection: dict[str, Any],
        segment_states: list[dict[str, Any]],
        event_requires: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(kind="real_pose", simulation=False)
        self._pose_records = pose_records
        self._motion = motion
        self._selection = selection
        self._segment_states = segment_states
        self._event_requires: dict[str, list[str]] = {
            kind: list(objs) for kind, objs in (event_requires or EVENT_REQUIRES).items()
        }

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            kind=self.kind,
            simulation=self.simulation,
            supported_stages=("strokes", "timing", "metrics"),
            limitations=_LIMITATIONS,
        )

    def run(
        self, stage: str, request: StageRequest, *, cancel: CancellationToken
    ) -> dict[str, Any]:
        self.assert_stage_supported(stage)
        cancel.check_point()
        if stage == "strokes":
            return {"events": [], "simulation": False}
        if stage == "timing":
            return self._timing()
        return {"metrics": self._metrics(), "simulation": False}

    # ------------------------------------------------------------------
    # timing: eleven honest, unobserved event stamps (FR-POSE-004)
    # ------------------------------------------------------------------

    def _timing(self) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        for kind, required in self._event_requires.items():
            if any(obj in ("ball", "racket") for obj in required):
                state = "not_visible"
            else:
                # body/feet keypoints exist, but V1 has no validated phase
                # detector: the honest answer is "cannot judge", not a guess
                state = "insufficient_evidence"
            events.append(
                {
                    "event_id": f"timing-{kind}",
                    "kind": kind,
                    "original_s": None,
                    "value_state": state,
                    "requires": list(required),
                    "evidence_ids": [],
                }
            )
        return {
            "events": events,
            "limitations": [
                "V1 reports no phase times: ball/racket are not detected "
                "(not_visible) and body phases have no validated detector "
                "(insufficient_evidence); no time is invented",
                *_LIMITATIONS,
            ],
            "simulation": False,
        }

    # ------------------------------------------------------------------
    # metrics: first real measurement batch from published motion records
    # ------------------------------------------------------------------

    def _metrics(self) -> list[dict[str, Any]]:
        angles_per_frame = self._motion.get("angles_per_frame", [])
        trajectories = self._motion.get("trajectories", {})
        summary = self._motion.get("summary", {})
        metrics: list[dict[str, Any]] = []

        # 1. angle ranges: max-min of each derived angle over posed frames
        for name in summary.get("angle_names", []):
            values = [
                frame["angles"][name]
                for frame in angles_per_frame
                if frame.get("angles", {}).get(name) is not None
            ]
            metrics.append(
                self._metric(
                    metric_id=f"pose.range.{name}",
                    definition=(
                        f"range (max-min) of the 2D image-plane {name} in degrees "
                        f"across the {len(values)} posed frames where all involved "
                        "keypoints were valid"
                    ),
                    value=round(max(values) - min(values), 1) if values else None,
                    observed=len(values) >= MIN_VALID_FRAMES,
                    unit="relative",
                    detail=f"{len(values)} valid frames",
                )
            )

        # 2. wrist trajectory envelope: body-size normalized spans (the
        #    median subject-box height is the size reference, so the span is
        #    scale-robust within one clip)
        box_heights = [
            float(record["subject_box"][3]) - float(record["subject_box"][1])
            for record in self._pose_records
            if record.get("subject_box")
        ]
        reference = statistics.median(box_heights) if box_heights else 0.0
        for side in ("left", "right"):
            points = [point for point in trajectories.get(f"{side}_wrist", []) if point is not None]
            xs = [point["x"] for point in points]
            ys = [point["y"] for point in points]
            span_x = (max(xs) - min(xs)) / reference if xs and reference > 0 else None
            span_y = (max(ys) - min(ys)) / reference if ys and reference > 0 else None
            for axis, span in (("x", span_x), ("y", span_y)):
                metrics.append(
                    self._metric(
                        metric_id=f"pose.envelope.{side}_wrist_{axis}",
                        definition=(
                            f"{side} wrist path span along the image {axis} axis, "
                            "normalized by the median subject-box height (body-size "
                            "normalized within this clip)"
                        ),
                        value=round(span, 3) if span is not None else None,
                        observed=sum(1 for _ in points) >= MIN_VALID_FRAMES and span is not None,
                        unit="normalized",
                        detail=f"{len(points)} wrist points",
                    )
                )

        # 3. action-segment durations and gaps (from the published selection).
        # Sample gates: a mean over a single segment or a single gap is a
        # one-sample statistic and stays insufficient_evidence (phase4
        # review D1) — durations need ≥ MIN_VALID_FRAMES segments, gaps
        # need ≥ 2 gaps.
        segments = [
            seg
            for seg in self._selection.get("selected_segments", [])
            if float(seg.get("end_s", 0.0)) > float(seg.get("start_s", 0.0))
        ]
        durations = [float(seg["end_s"]) - float(seg["start_s"]) for seg in segments]
        metrics.append(
            self._metric(
                metric_id="pose.action_segment_mean_duration_s",
                definition=(
                    "mean duration of the selected action segments in seconds of "
                    "normalized derivative time"
                ),
                value=round(statistics.fmean(durations), 3) if durations else None,
                observed=len(durations) >= MIN_VALID_FRAMES,
                unit="relative",
                detail=f"{len(durations)} selected segments",
            )
        )
        gaps = [
            float(next_seg["start_s"]) - float(seg["end_s"])
            for seg, next_seg in zip(segments, segments[1:], strict=False)
        ]
        metrics.append(
            self._metric(
                metric_id="pose.action_segment_mean_gap_s",
                definition=(
                    "mean gap between consecutive selected action segments in "
                    "seconds of normalized derivative time"
                ),
                value=round(statistics.fmean(gaps), 3) if gaps else None,
                observed=len(gaps) >= 2,
                unit="relative",
                detail=f"{len(gaps)} gaps",
            )
        )
        return metrics

    def _metric(
        self,
        *,
        metric_id: str,
        definition: str,
        value: float | None,
        observed: bool,
        unit: str,
        detail: str,
    ) -> dict[str, Any]:
        return {
            "metric_id": metric_id,
            "definition": definition,
            "value": value if observed else None,
            "value_state": "observed" if observed else "insufficient_evidence",
            "unit": unit,
            "reference": "this clip only; no external baseline",
            "evidence_ids": [],
            "limitations": [
                f"derived from {detail}",
                *_LIMITATIONS,
            ],
        }


# ---------------------------------------------------------------------------
# Real evidence entries: selected action segments back-linked to original time
# ---------------------------------------------------------------------------


def real_evidence_entries(
    *,
    task_id: str,
    version: int,
    media: MediaAsset,
    selection: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Build evidence entries for the selected action segments.

    Intervals come from the published selection (normalized derivative time)
    and are mapped to ORIGINAL time through the measured BE-014 mapping —
    never estimated. ``visible_objects`` claims only ``body``: V1 pose
    keypoints are body observations. Entries are ``simulation=False`` — the
    pose is measured, not simulated.
    """
    if not media.time_mapping_valid:
        raise DomainViolation(
            "EVIDENCE_TIME_UNMAPPED",
            "media has no valid time mapping; no evidence can be published (EX-043)",
        )
    span = media.time_mapping[-1].normalized_s
    entries: list[dict[str, Any]] = []
    for index, segment in enumerate(selection.get("selected_segments", [])):
        start_n = float(segment.get("start_s", 0.0))
        end_n = float(segment.get("end_s", 0.0))
        if end_n <= start_n:
            continue
        if start_n > span or end_n > span:
            # a partial mapping never silently covers the whole clip: an
            # interval outside the measured span is refused, not clamped
            # (phase4 review B1 — the catalog enforces the same gate)
            raise DomainViolation(
                "EVIDENCE_OUTSIDE_MAPPING",
                f"action segment reaches {end_n:.3f}s but the mapping covers only"
                f" {span:.3f}s of normalized time; publish nothing beyond it",
            )
        entries.append(
            {
                "evidence_id": f"ev-{task_id}-v{version}-seg-{index}",
                "kind": "video_clip",
                "asset_id": media.media_id,
                "subject_id": "unspecified",
                "original_start_s": round(max(original_time(media, start_n), 0.0), 6),
                "original_end_s": round(max(original_time(media, end_n), 0.0), 6),
                "visible_objects": ["body"],
                "resource": None,
                "simulation": False,
            }
        )
    return tuple(entries)
