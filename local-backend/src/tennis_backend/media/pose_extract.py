"""Per-frame wholebody pose extraction for the tracked subject (Phase 3).

Runs RTMW-x (rtmlib Wholebody, balanced) over the standardized video and
records per-frame observations honestly:

- raw keypoints (x, y, score) in full-frame pixel coordinates; NO smoothing
  or interpolation in V1 — raw detections and any future derivatives stay
  separate (real-pose/plan.md §2);
- visibility/effectiveness mask per keypoint (score >= threshold);
- frames without a tracked subject box record ``no_subject_box`` and carry
  no keypoints — lost frames are never filled from neighbours;
- when the pose model's detected person does not overlap the tracked
  subject box, the frame records ``subject_not_matched`` — the pose belongs
  to someone else and must not be attributed to the subject.

The skeleton topology (COCO-WholeBody: 133 keypoints, 65 links) is
published alongside the records so the workbench can draw the skeleton
without hardcoding it. Heavy inference runs on the caller's thread — under
the NDJSON transport this is a BE-012 worker job.
"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np

from tennis_backend.domain.models import DomainViolation

_VIDEO_MISSING = "POSE_VIDEO_MISSING"
_DEPS_MISSING = "REAL_POSE_DEPS_MISSING"
_NO_READABLE_FRAMES = "POSE_NO_READABLE_FRAMES"

DEFAULT_KPT_THRESHOLD = 0.3
#: Crop margin around the subject box before inference (fraction of size).
#: Keypoints are recorded in FULL-FRAME coordinates regardless.
CROP_MARGIN = 0.25


class PoseModel(Protocol):
    """One-frame wholebody pose inference."""

    def infer(self, frame_bgr: Any) -> tuple[np.ndarray, np.ndarray]:
        """Return (keypoints [K,2] float, scores [K] float) for the person."""


class RtmlibWholebodyPose:
    """RTMW-x via rtmlib Wholebody (lazy load; balanced mode per Phase 1)."""

    def __init__(self, mode: str = "balanced") -> None:
        self._mode = mode
        self._model: Any = None

    def infer(self, frame_bgr: Any) -> tuple[np.ndarray, np.ndarray]:
        """Return ALL detected persons: (keypoints [N,K,2], scores [N,K]).

        rtmlib's internal detector picks every person it sees; subject
        association (which one is OUR tracked subject) is the caller's job —
        the backend never attributes keypoints to a subject without overlap
        evidence.
        """
        if self._model is None:
            try:
                from rtmlib import Wholebody
            except ImportError as error:
                raise DomainViolation(
                    _DEPS_MISSING,
                    "real-pose dependencies are not installed; run uv sync --extra real-pose",
                ) from error
            # rtmlib's loader uses module-level print for diagnostics. Route
            # only these modules to stderr; global stdout belongs to NDJSON
            # and must remain available to concurrent control responses.
            for name in ("rtmlib.tools.base", "rtmlib.tools.file"):
                module: Any = importlib.import_module(name)
                module.print = _model_diagnostic
            self._model = Wholebody(mode=self._mode)
        kpts, scores = self._model(frame_bgr)
        kpts = np.asarray(kpts, dtype=np.float32)
        scores = np.asarray(scores, dtype=np.float32)
        if kpts.ndim == 2:  # single-person form [K, 2]
            kpts = kpts[None]
        if scores.ndim == 1:
            scores = scores[None]
        if scores.ndim == 3:  # [N, K, 1] batch form
            scores = scores.reshape(scores.shape[0], -1)
        return kpts, scores


def _model_diagnostic(*values: Any, **kwargs: Any) -> None:
    kwargs["file"] = sys.stderr
    print(*values, **kwargs)


def skeleton_spec() -> dict[str, Any]:
    """COCO-WholeBody topology from rtmlib: 133 keypoints, 65 links.

    Published with the pose records so the workbench draws exactly the
    topology the model was run with (no hardcoded guessing).
    """
    from rtmlib.visualization.skeleton.coco133 import coco133

    keypoints = [
        {
            "index": index,
            "name": info["name"],
            "color": info["color"],
        }
        for index, info in coco133["keypoint_info"].items()
    ]
    skeleton = [
        {
            "index": index,
            "from": info["link"][0],
            "to": info["link"][1],
            "color": info["color"],
        }
        for index, info in coco133["skeleton_info"].items()
    ]
    return {
        "name": coco133["name"],
        "keypoints": keypoints,
        "skeleton": skeleton,
    }


@dataclass
class PoseOutcome:
    """Per-frame pose observations plus the published artifact paths."""

    records: list[dict[str, Any]]
    pose_jsonl: Path | None
    skeleton_spec_path: Path | None
    kpt_count: int
    frames_with_pose: int
    frames_without_subject: int
    notes: tuple[str, ...]


def _expand_box(
    box: list[int], width: int, height: int, margin: float
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    mx, my = int(bw * margin), int(bh * margin)
    return (
        max(0, x1 - mx),
        max(0, y1 - my),
        min(width, x2 + mx),
        min(height, y2 + my),
    )


def _center_in_box(kpts: np.ndarray, valid: np.ndarray, box: tuple[int, int, int, int]) -> bool:
    if not valid.any():
        return False
    xs = kpts[valid, 0]
    ys = kpts[valid, 1]
    cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
    x1, y1, x2, y2 = box
    return bool(x1 <= cx <= x2 and y1 <= cy <= y2)


#: A box edge within this many pixels of the frame border counts as touching it.
EDGE_MARGIN_PX = 3
#: Joints a swing analysis READS: shoulders, elbows, wrists, hips. A cut-off
#: here corrupts trunk-turn and arm measurements. Feet/knees/head are tracked
#: separately (``lower_body_outside``) because they leave a handheld frame
#: all the time without hurting an upper-body swing analysis.
_UPPER_BODY = (5, 6, 7, 8, 9, 10, 11, 12)
_LOWER_BODY = (13, 14, 15, 16)
#: Overshoot tolerated before a joint counts as "outside": a few pixels past
#: the border is detector jitter on a joint that IS at the edge, not a body
#: part hidden beyond it. Fraction of the subject box height.
OUTSIDE_TOLERANCE = 0.02
#: When the person box touches a SIDE border, an upper-body joint lying within
#: this many pixels of that border is on the very edge of the picture: the body
#: part beyond it is invisible and the joint is a boundary estimate. Measured
#: on the real clip, 0 px flagged 26 frames, 10 px flagged 33 and 30 px 38 —
#: the count flattens after ~10 px, so that is the cut.
EDGE_BAND_PX = 10


def clip_state(
    box: list[int] | None,
    keypoints: list[list[float]] | None,
    valid: list[bool] | None,
    width: int,
    height: int,
    margin: int = EDGE_MARGIN_PX,
) -> dict[str, Any]:
    """Is the subject's UPPER body cut off by the frame, i.e. are joints guessed?

    A detector keeps producing "valid" (high-score) keypoints for body parts
    that are OUTSIDE the image — it extrapolates from the visible part. Those
    joints are guesses, not observations, so the frame is flagged — the raw
    keypoints/valid arrays stay untouched (raw observations are never edited;
    downstream derivations decide what to do with the flag).

    Calibrated on a real 544×960 back-view clip (291 frames): counting ANY
    out-of-image joint flagged 56% of frames, but 113 of those were just the
    ANKLES 6–17 px below the bottom edge (feet clipped, person intact). The
    swing metrics read shoulders/elbows/wrists/hips, so only those joints
    decide ``clipped``; lower-body overshoot is reported separately and does
    not discard the frame.

    Returns ``{"clipped", "edges", "upper_body_outside", "upper_body_on_edge",
    "lower_body_outside"}``. ``clipped`` is true when an upper-body joint lies
    outside the image by more than OUTSIDE_TOLERANCE of the box height, OR the
    box touches a side border and an upper-body joint sits within EDGE_BAND_PX
    of it.
    """
    edges: list[str] = []
    if box is not None:
        x1, y1, x2, y2 = box
        if x1 <= margin:
            edges.append("left")
        if x2 >= width - margin:
            edges.append("right")
        if y1 <= margin:
            edges.append("top")
        if y2 >= height - margin:
            edges.append("bottom")
    tolerance = 0.0
    if box is not None:
        tolerance = OUTSIDE_TOLERANCE * max(1, box[3] - box[1])

    def outside(indices: tuple[int, ...]) -> int:
        if keypoints is None or valid is None:
            return 0
        count = 0
        for index in indices:
            if index >= len(keypoints) or index >= len(valid) or not valid[index]:
                continue
            x, y = keypoints[index][0], keypoints[index][1]
            if max(-x, x - width, -y, y - height, 0.0) > tolerance:
                count += 1
        return count

    def on_side_edge() -> int:
        """Upper-body joints on the picture edge of a side border the box touches."""
        if keypoints is None or valid is None:
            return 0
        left, right = "left" in edges, "right" in edges
        if not (left or right):
            return 0
        count = 0
        for index in _UPPER_BODY:
            if index >= len(keypoints) or index >= len(valid) or not valid[index]:
                continue
            x = keypoints[index][0]
            if (left and x <= EDGE_BAND_PX) or (right and x >= width - EDGE_BAND_PX):
                count += 1
        return count

    upper, lower, on_edge = outside(_UPPER_BODY), outside(_LOWER_BODY), on_side_edge()
    return {
        "clipped": upper > 0 or on_edge > 0,
        "edges": edges,
        "upper_body_outside": upper,
        "upper_body_on_edge": on_edge,
        "lower_body_outside": lower,
    }


def extract_pose(
    *,
    video: Path,
    subject_boxes: list[list[int] | None],
    out_dir: Path,
    model: PoseModel | None = None,
    kpt_threshold: float = DEFAULT_KPT_THRESHOLD,
    segments: list[tuple[int, int]] | None = None,
    origin_frame: int = 0,
    should_cancel: Callable[[], bool] | None = None,
) -> PoseOutcome:
    """Extract per-frame pose for the tracked subject over ``video``.

    ``subject_boxes[i]`` is the tracked subject's box for video frame
    ``origin_frame + i`` (or None when the tracker lost the subject). Frames are read sequentially;
    keypoints are recorded raw, in full-frame coordinates.

    ``segments`` (inclusive frame ranges) restricts inference to selected
    action windows: frames outside are read and discarded without records
    or inference — the expensive model only sees selected action frames.
    Selection metadata is the caller's responsibility (published as
    selection.json by the dispatch layer).
    """
    video = Path(video)
    if not video.is_file():
        raise DomainViolation(_VIDEO_MISSING, f"video does not exist: {video.name}")
    if model is None and segments != []:
        model = RtmlibWholebodyPose()

    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise DomainViolation(_VIDEO_MISSING, "video cannot be opened for reading")

    records: list[dict[str, Any]] = []
    frames_with_pose = 0
    frames_without_subject = 0
    kpt_count = 0
    frame_index = 0

    # extraction window = len(subject_boxes), optionally restricted to the
    # selected action segments (two-pass 10-action collection)
    selected: set[int] | None = None
    last_selected = -1
    if segments is not None:
        selected = {f for start, end in segments for f in range(start, end + 1)}
        last_selected = max((end for _, end in segments), default=-1)

    # seek to the origin frame: boxes are indexed by ABSOLUTE frame number,
    # so the video read must start there (phase2/3 review — slicing boxes to
    # segment-local indices misaligned pixels and frame labels)
    capture.set(cv2.CAP_PROP_POS_FRAMES, origin_frame)
    frame_index = origin_frame

    cancelled_at: int | None = None
    while frame_index < len(subject_boxes) and (selected is None or frame_index <= last_selected):
        ok, frame_bgr = capture.read()
        if not ok:
            break
        if should_cancel is not None and should_cancel():
            # Phase 5 cooperative cancellation: stop at this frame; the
            # outcome notes the interruption and the records so far stay
            # honest partial observations
            cancelled_at = frame_index
            break
        pts_s = round(capture.get(cv2.CAP_PROP_POS_MSEC) / 1000.0, 4)
        height, width = frame_bgr.shape[0], frame_bgr.shape[1]
        if selected is not None and frame_index not in selected:
            frame_index += 1
            continue
        box = subject_boxes[frame_index]
        record: dict[str, Any] = {
            "frame": frame_index,
            "pts_s": pts_s,
            "subject_box": box,
            "keypoints": None,
            "valid": None,
            "subject_overlap": None,
            "failure": None,
            "clip": None,
        }
        if box is None:
            record["failure"] = "no_subject_box"
            records.append(record)
            frames_without_subject += 1
            frame_index += 1
            continue

        x1, y1, x2, y2 = _expand_box(box, width, height, CROP_MARGIN)
        crop = frame_bgr[y1:y2, x1:x2]
        if crop.size == 0:
            record["failure"] = "empty_crop"
            records.append(record)
            frame_index += 1
            continue

        try:
            assert model is not None
            kpts_all, scores_all = model.infer(crop)
        except Exception as error:  # noqa: BLE001 - inference faults recorded,
            # never crash the whole extraction for one frame
            record["failure"] = f"inference_error:{type(error).__name__}"
            records.append(record)
            frame_index += 1
            continue

        # normalize shapes: kpts [N, K, 2], scores [N, K]
        if kpts_all.ndim == 2:
            kpts_all = kpts_all[None]
        if scores_all.ndim == 1:
            scores_all = scores_all[None]
        if scores_all.ndim == 3:
            scores_all = scores_all.reshape(scores_all.shape[0], -1)

        # SUBJECT ASSOCIATION: pick the person whose keypoint center lies in
        # the tracked subject box; a pose belonging to someone else is never
        # attributed to the subject (real-pose/plan.md §2). Keypoints are
        # translated crop-local -> full-frame.
        box_tuple = (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
        chosen: int | None = None
        chosen_valid: np.ndarray | None = None
        chosen_full: np.ndarray | None = None
        for p in range(kpts_all.shape[0]):
            pk = kpts_all[p].copy()
            ps = scores_all[p].reshape(-1)
            pk[:, 0] += x1
            pk[:, 1] += y1
            valid_p = ps >= kpt_threshold
            if _center_in_box(pk, valid_p, box_tuple):
                chosen = p
                chosen_valid = valid_p
                chosen_full = pk
                break

        if chosen is None:
            # a pose matching someone else is never attributed to the subject
            record["failure"] = "subject_not_matched"
            records.append(record)
            frame_index += 1
            continue

        assert chosen_valid is not None and chosen_full is not None
        valid = chosen_valid
        kpt_list = [
            [round(float(x), 2), round(float(y), 2), round(float(s), 3)]
            for (x, y), s in zip(chosen_full, scores_all[chosen], strict=False)
        ]
        valid_list = [bool(v) for v in valid]

        record["keypoints"] = kpt_list
        record["valid"] = valid_list
        record["subject_overlap"] = True
        record["clip"] = clip_state(box, kpt_list, valid_list, width, height)
        frames_with_pose += 1
        records.append(record)
        kpt_count = int(kpts_all.shape[1])
        frame_index += 1
        if selected is not None and frame_index > last_selected:
            break  # everything selected has been processed
    capture.release()

    out_dir.mkdir(parents=True, exist_ok=True)
    pose_jsonl = out_dir / "pose-frames.jsonl"
    pose_jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records),
        encoding="utf-8",
    )
    spec_path = out_dir / "skeleton-spec.json"
    spec_path.write_text(
        json.dumps(skeleton_spec(), ensure_ascii=False, indent=1), encoding="utf-8"
    )

    notes = [
        "raw per-frame keypoints, no smoothing or interpolation (plan.md §2)",
        "keypoints in full-frame pixel coordinates; score >= threshold = valid",
    ]
    if frames_without_subject:
        notes.append(f"{frames_without_subject} frames had no tracked subject box")
    if cancelled_at is not None:
        notes.append(f"cancelled at frame {cancelled_at} (cooperative cancellation)")
    return PoseOutcome(
        records=records,
        pose_jsonl=pose_jsonl,
        skeleton_spec_path=spec_path,
        kpt_count=kpt_count,
        frames_with_pose=frames_with_pose,
        frames_without_subject=frames_without_subject,
        notes=tuple(notes),
    )
