"""Subject detection and tracking over an approved video (Phase 2).

Runs the article-stack person pipeline locally: RF-DETR person detection per
frame + ByteTrack single-subject tracking, then records everything honestly:

- per-frame boxes (class/confidence), track id, and lost frames are recorded
  as-is; lost frames are never filled from neighbouring frames;
- a single stable track is reported as ``single``; more than one persistent
  track reports ``multi`` with ``requires_selection`` (FR-POSE-006: the user
  picks the subject, the backend never chooses);
- no persons at all reports ``none`` with the same degrade semantics.

The detector is injected so tests can drive known boxes; the default wraps
RF-DETR nano (requires the ``real-pose`` extra: ``uv sync --extra real-pose``).
Heavy model inference runs on the caller's thread — under the NDJSON transport
this is a BE-012 worker job, so status/cancel stay responsive.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np

from tennis_backend.domain.models import DomainViolation

_VIDEO_MISSING = "SUBJECT_VIDEO_MISSING"
_DEPS_MISSING = "REAL_POSE_DEPS_MISSING"
_EMPTY_ANNOTATED = "SUBJECT_EMPTY_RENDER"

#: Seconds of analysed footage in Phase 2; longer clips are capped honestly.
DEFAULT_MAX_SECONDS = 180.0  # FR-002: full video up to 180s

#: A track id is "persistent" when it holds at least this share of frames.
_PERSISTENT_SHARE = 0.2


class PersonDetector(Protocol):
    """One-frame person detector: returns person boxes in pixel coords."""

    def detect(self, frame_rgb: Any) -> list[dict[str, Any]]:
        """Return [{"class": "person", "confidence": float, "box": [x1,y1,x2,y2]}]."""


class RFDetrPersonDetector:
    """RF-DETR nano wrapped as a PersonDetector (lazy model load)."""

    def __init__(self, threshold: float = 0.5) -> None:
        self._threshold = threshold
        self._model: Any = None

    def detect(self, frame_rgb: Any) -> list[dict[str, Any]]:
        if self._model is None:
            try:
                import rfdetr
            except ImportError as error:
                raise DomainViolation(
                    _DEPS_MISSING,
                    "real-pose dependencies are not installed; run uv sync --extra real-pose",
                ) from error
            # RF-DETR's INFO handler targets stdout, reserved for NDJSON.
            # Replace only that logger's handlers; never redirect global stdout
            # while other transport/control threads are running.
            logger = logging.getLogger("rf-detr")
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
            logger.addHandler(logging.StreamHandler(sys.stderr))
            logger.propagate = False
            self._model = rfdetr.RFDETRNano()
        result = self._model.predict(frame_rgb, threshold=self._threshold)
        names = result.data.get("class_name") if hasattr(result, "data") else None
        out: list[dict[str, Any]] = []
        for i in range(len(result.xyxy)):
            label = str(names[i]) if names is not None else str(int(result.class_id[i]))
            if label != "person":
                continue
            out.append(
                {
                    "class": "person",
                    "confidence": round(float(result.confidence[i]), 3),
                    "box": [int(v) for v in result.xyxy[i]],
                }
            )
        return out


@dataclass
class SubjectRecord:
    """One frame's tracking truth, kept exactly as observed."""

    frame: int
    pts_s: float
    boxes: list[dict[str, Any]]
    track_id: int | None
    lost: bool


@dataclass(frozen=True)
class ActionSegment:
    """One complete-action candidate: a maximal run of tracked, moving frames.

    Boundaries are movement-based (low movement = idle between actions); the
    ball is not detected, so "complete stroke" is a CANDIDATE-level judgment,
    recorded as such — never claimed as a confirmed stroke.
    """

    start_frame: int
    end_frame: int  # inclusive
    start_s: float
    end_s: float
    frames: int


def derive_action_segments(
    records: list[SubjectRecord],
    *,
    fps: float,
    move_threshold: float = 0.02,
    min_duration_s: float = 1.0,
    merge_gap_s: float = 1.0,
) -> list[ActionSegment]:
    """Split tracked frames into movement-based action segments.

    ``move_threshold`` is scale-invariant: center displacement per frame as a
    fraction of subject box height. Frames where the subject is present but
    idle are excluded (they are not part of an action); lost frames break
    continuity. Heuristic segmentation — segments are CANDIDATE action
    windows, recorded as such.
    """
    centers: list[tuple[int, float, float, float]] = []
    for record in records:
        if record.boxes:
            box = record.boxes[0]["box"]
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            box_h = max(box[3] - box[1], 1)
            centers.append((record.frame, cx, cy, box_h))

    if len(centers) < 2:
        return []

    # transitions between consecutive present frames
    active_frames: set[int] = set()
    for (f1, cx1, cy1, h1), (f2, cx2, cy2, _h2) in zip(centers, centers[1:], strict=False):
        if f2 - f1 != 1:
            continue  # lost gap: continuity already broken
        disp = ((cx2 - cx1) ** 2 + (cy2 - cy1) ** 2) ** 0.5
        if disp >= move_threshold * h1:
            active_frames.add(f1)
            active_frames.add(f2)

    if not active_frames:
        return []

    ordered = sorted(active_frames)
    runs: list[list[int]] = []
    run = [ordered[0]]
    for f in ordered[1:]:
        if f == run[-1] + 1:
            run.append(f)
        else:
            runs.append(run)
            run = [f]
    runs.append(run)

    # merge runs separated by small gaps (<= merge_gap_s)
    merged: list[list[int]] = [runs[0]]
    for run in runs[1:]:
        if run[0] - merged[-1][-1] - 1 <= max(1, int(merge_gap_s * fps)):
            merged[-1].extend(range(merged[-1][-1] + 1, run[0]))
            merged[-1].extend(run)
        else:
            merged.append(run)

    segments = []
    for run in merged:
        duration = len(run) / fps
        if duration >= min_duration_s:
            segments.append(
                ActionSegment(
                    start_frame=run[0],
                    end_frame=run[-1],
                    start_s=round(run[0] / fps, 3),
                    end_s=round(run[-1] / fps, 3),
                    frames=len(run),
                )
            )
    return segments


def select_action_segments(
    segments: list[ActionSegment], *, max_actions: int = 10
) -> list[ActionSegment]:
    """Chronologically keep the first ``max_actions`` complete segments."""
    return sorted(segments, key=lambda s: s.start_frame)[:max_actions]


#: Max frames extracted per leading/trailing context run (bounded inference).
MAX_CONTEXT_FRAMES = 600


def pose_context_windows(
    records: list[SubjectRecord],
    selected_segments: list[ActionSegment],
    *,
    max_context_frames: int = MAX_CONTEXT_FRAMES,
) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """Leading/trailing tracked runs OUTSIDE the selected action segments.

    The 10-action cap skips non-playing frames to bound inference cost, but
    the subject standing idle before/after play is still measured data: the
    skeleton overlay must show it (workbench feedback — the first seconds of
    a clip had no skeleton although the subject was tracked there). Interior
    idle gaps BETWEEN action segments stay excluded — that is the explicit
    resource saving. Each run is truncated to the ``max_context_frames``
    frames NEAREST the play, so a long idle tail cannot blow the budget.

    Returns ``(head, tail)`` inclusive frame windows; either may be None.
    """
    tracked = sorted(
        record.frame for record in records if record.track_id is not None and record.boxes
    )
    if not tracked:
        return None, None
    first, last = tracked[0], tracked[-1]
    head: tuple[int, int] | None = None
    tail: tuple[int, int] | None = None
    if selected_segments:
        head_boundary = selected_segments[0].start_frame - 1
        if first <= head_boundary:
            head = (max(first, head_boundary - max_context_frames + 1), head_boundary)
        tail_boundary = selected_segments[-1].end_frame + 1
        if tail_boundary <= last:
            tail = (tail_boundary, min(last, tail_boundary + max_context_frames - 1))
    else:
        # no action segments at all: the tracked run itself is the context
        head = (first, min(last, first + max_context_frames - 1))
        if head[1] < last:
            tail = (head[1] + 1, min(last, head[1] + max_context_frames))
    return head, tail


@dataclass
class SubjectsOutcome:
    """The whole extraction: records, stability verdict, artifacts."""

    records: list[SubjectRecord]

    @property
    def majority_track_id(self) -> int | None:
        """The most frequently tracked id across non-lost frames."""
        seen: dict[int, int] = {}
        for record in self.records:
            if record.track_id is not None:
                seen[record.track_id] = seen.get(record.track_id, 0) + 1
        return max(seen, key=lambda k: seen[k]) if seen else None

    subject_stability: str  # "single" | "multi" | "none"
    requires_selection: bool
    retention: float
    annotated_video: Path | None
    per_frame_jsonl: Path | None
    notes: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "subject_stability": self.subject_stability,
            "requires_selection": self.requires_selection,
            "retention": self.retention,
            "frames": len(self.records),
            "lost_frames": sum(1 for r in self.records if r.lost),
            "annotated_video": (
                str(self.annotated_video.relative_to(self.annotated_video.parents[2]))
                if self.annotated_video
                else None
            ),
            "notes": list(self.notes),
        }


def extract_subjects(
    *,
    video: Path,
    out_dir: Path,
    detector: PersonDetector | None = None,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    should_cancel: Callable[[], bool] | None = None,
) -> SubjectsOutcome:
    """Detect and track persons over ``video``; record observations honestly.

    ``should_cancel`` is polled before every frame (Phase 5 cooperative
    cancellation): when it turns true the extraction stops at that frame and
    the outcome honestly notes the interruption — a cancelled task never
    keeps burning minutes of detector time.
    """
    video = Path(video)
    if not video.is_file():
        raise DomainViolation(_VIDEO_MISSING, f"video does not exist: {video.name}")
    if detector is None:
        detector = RFDetrPersonDetector()

    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise DomainViolation(_VIDEO_MISSING, "video cannot be opened for reading")
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    # output size from the ACTUAL first frame, not header metadata: header
    # dims can disagree with decoded pixels when a container carries rotation
    # the decoder did not apply (phase2-rotation diagnosis)
    ok, first_frame = capture.read()
    if not ok:
        raise DomainViolation(_VIDEO_MISSING, "video has no readable frames")
    height, width = int(first_frame.shape[0]), int(first_frame.shape[1])
    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)

    from boxmot import ByteTrack

    tracker = ByteTrack()  # per-extraction: continuity within one video only
    records: list[SubjectRecord] = []
    track_hits: dict[int, int] = {}
    detect_times: list[float] = []
    frame_index = 0

    annotated_path: Path | None = None
    writer: cv2.VideoWriter | None = None
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore[attr-defined]

    cancelled_at: int | None = None
    while True:
        ok, frame_bgr = capture.read()
        if not ok:
            break
        pts_s = frame_index / fps
        if pts_s >= max_seconds:
            break
        if should_cancel is not None and should_cancel():
            cancelled_at = frame_index
            break

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        t0 = time.perf_counter()
        detections = detector.detect(rgb)
        detect_times.append(time.perf_counter() - t0)

        track_id: int | None = None
        people = [d for d in detections if d.get("class") == "person"]
        if people:
            dets = np.array([[*d["box"], d["confidence"], 0.0] for d in people], dtype=np.float32)
            outputs = tracker.update(dets, frame_bgr)
            # boxmot row layout: [x1, y1, x2, y2, track_id, conf, cls, det_idx]
            # count EVERY track row: dropping secondary rows would misreport
            # a multi-subject scene as single (the BE-017 review class of bug)
            primary = True
            for row in outputs:
                tid = int(row[4])
                track_hits[tid] = track_hits.get(tid, 0) + 1
                # merge the id back onto its detection box (det_idx indexes
                # the people list): per-box ids make the per-window subject
                # check able to SEE a second person, not just the primary
                det_idx = int(row[7]) if len(row) > 7 else -1
                if 0 <= det_idx < len(people):
                    people[det_idx]["track_id"] = tid
                if primary:
                    track_id = tid
                    primary = False

        lost = track_id is None
        records.append(
            SubjectRecord(
                frame=frame_index,
                pts_s=round(pts_s, 4),
                boxes=people,
                track_id=track_id,
                lost=lost,
            )
        )

        if writer is None:
            out_dir.mkdir(parents=True, exist_ok=True)
            annotated_path = out_dir / "subjects-annotated.mp4"
            writer = cv2.VideoWriter(str(annotated_path), fourcc, fps, (width, height))
        for d in people:
            x1, y1, x2, y2 = d["box"]
            cv2.rectangle(frame_bgr, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(
                frame_bgr,
                f"id={d.get('track_id')} {d['confidence']}",
                (x1, max(y1 - 4, 12)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 255, 0),
                1,
            )
        if lost:
            cv2.putText(frame_bgr, "LOST", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        writer.write(frame_bgr)
        frame_index += 1
    capture.release()
    if writer is not None:
        writer.release()
    if annotated_path is not None and not annotated_path.exists():
        annotated_path = None

    analysed = len(records)
    persistent = {
        tid: hits
        for tid, hits in track_hits.items()
        if analysed and hits / analysed >= _PERSISTENT_SHARE
    }
    if analysed and track_hits and len(persistent) == 1:
        # exactly one persistent track: ids are stable now, so a single id
        # covering as little as the persistent-share threshold is a real subject
        stability = "single"
    elif len(persistent) > 1:
        stability = "multi"
    else:
        stability = "none"

    retention = round(sum(1 for r in records if not r.lost) / analysed, 3) if analysed else 0.0

    requires_selection = stability == "multi"
    notes = [
        (
            f"detector frames: {analysed}; avg {sum(detect_times) / analysed * 1000:.0f} ms/frame"
            if detect_times
            else "no frames analysed"
        ),
    ]
    if stability == "multi":
        notes.append(
            "more than one persistent subject: select one person before individual analysis"
        )
    if stability == "none":
        notes.append("no stable person track observed in this clip")
    if cancelled_at is not None:
        notes.append(f"cancelled at frame {cancelled_at} (cooperative cancellation)")

    per_frame_path = out_dir / "subjects-frames.jsonl"
    per_frame_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "frame": r.frame,
                    "pts_s": r.pts_s,
                    "boxes": r.boxes,
                    "track_id": r.track_id,
                    "lost": r.lost,
                },
                ensure_ascii=False,
            )
            for r in records
        ),
        encoding="utf-8",
    )

    return SubjectsOutcome(
        records=records,
        subject_stability=stability,
        requires_selection=requires_selection,
        retention=retention,
        annotated_video=annotated_path,
        per_frame_jsonl=per_frame_path,
        notes=tuple(notes),
    )
