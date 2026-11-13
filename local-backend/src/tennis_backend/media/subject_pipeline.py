"""Per-subject pose pipeline and subject candidate selection (联调 request).

A clip can hold several people (a match can hold four). The user either
confirms ONE subject to analyze or asks for ALL of them:

- ``subject_id=None`` — unconfirmed: the dominant subject per window (the
  historical heuristic, unchanged), flagged as unconfirmed;
- ``subject_id="track-<n>"`` — exactly that tracked person;
- ``subject_id="all"`` — every candidate (capped at :data:`MAX_ALL_SUBJECTS`),
  each with its OWN action segments, pose records and metrics.

Both ids are valid frozen-contract ``Id`` strings, so no schema changes.
Track ids come from ByteTrack and are deterministic for one video, which is
what makes selecting by track id reproducible across re-runs.

Candidates are deliberately NOT size-filtered: a small figure in the
background is listed (with its ``size_ratio``) and the user decides. The
size heuristic only shapes the *unconfirmed default*, never the candidate list.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tennis_backend.media import subjects as _subjects
from tennis_backend.media.subjects import (
    _PERSISTENT_SHARE,
    ActionSegment,
    SubjectRecord,
)

#: Candidate listing bounds.
CANDIDATE_MIN_FRAMES = 25  # ~1s at 25fps; shorter tracks are tracker noise
MAX_CANDIDATES = 12
#: ``subject_id="all"`` analyzes at most this many candidates (longest first).
MAX_ALL_SUBJECTS = 6
MAX_ACTIONS = 10

_LABEL = re.compile(r"^track-(\d+)$")


def subject_label(track_id: int) -> str:
    return f"track-{track_id}"


def parse_subject_label(value: str) -> int | None:
    match = _LABEL.match(value)
    return int(match.group(1)) if match else None


def estimate_fps(records: list[SubjectRecord]) -> float:
    if len(records) > 1:
        delta = (records[-1].pts_s - records[0].pts_s) / (len(records) - 1)
        if delta > 0:
            return round(1.0 / delta, 1)
    return 25.0


def subject_candidates(records: list[SubjectRecord]) -> list[dict[str, Any]]:
    """Every tracked person worth offering, longest-tracked first (unfiltered by size)."""
    analysed = len(records)
    stats: dict[int, dict[str, Any]] = {}
    for record in records:
        for box in record.boxes:
            tid = box.get("track_id")
            if tid is None:
                continue
            entry = stats.setdefault(
                tid, {"frames": 0, "heights": [], "first": record, "last": record}
            )
            entry["frames"] += 1
            entry["heights"].append(box["box"][3] - box["box"][1])
            entry["last"] = record
    min_frames = min(CANDIDATE_MIN_FRAMES, max(3, analysed // 20))
    rows: list[dict[str, Any]] = []
    for tid, entry in stats.items():
        if entry["frames"] < min_frames:
            continue
        share = entry["frames"] / analysed if analysed else 0.0
        rows.append(
            {
                "subject_id": subject_label(tid),
                "track_id": tid,
                "frames": entry["frames"],
                "share": round(share, 3),
                "persistent": share >= _PERSISTENT_SHARE,
                "first_frame": entry["first"].frame,
                "last_frame": entry["last"].frame,
                "first_s": entry["first"].pts_s,
                "last_s": entry["last"].pts_s,
                "median_box_height": round(statistics.median(entry["heights"]), 1),
            }
        )
    rows.sort(key=lambda row: (-row["frames"], row["track_id"]))
    rows = rows[:MAX_CANDIDATES]
    biggest = max((row["median_box_height"] for row in rows), default=0) or 1
    for row in rows:
        row["size_ratio"] = round(row["median_box_height"] / biggest, 3)
    return rows


@dataclass(frozen=True)
class SubjectPlan:
    """What the create call will analyze, resolved against the candidates."""

    mode: str  # unconfirmed | single | all | invalid
    requested: str | None
    track_ids: tuple[int | None, ...]  # None = the dominant-subject heuristic
    error: str | None = None


def resolve_subject_plan(requested: str | None, candidates: list[dict[str, Any]]) -> SubjectPlan:
    if requested is None:
        return SubjectPlan("unconfirmed", None, (None,))
    by_label = {row["subject_id"]: row["track_id"] for row in candidates}
    listing = ", ".join(by_label) or "none"
    if requested == "all":
        picked = tuple(row["track_id"] for row in candidates[:MAX_ALL_SUBJECTS])
        if not picked:
            return SubjectPlan("invalid", requested, (), "no candidate subject tracks to analyze")
        return SubjectPlan("all", requested, picked)
    if requested in by_label:
        return SubjectPlan("single", requested, (by_label[requested],))
    return SubjectPlan(
        "invalid",
        requested,
        (),
        f"subject_id {requested} is not a candidate (choose one of: {listing}, or 'all')",
    )


def track_view(records: list[SubjectRecord], track_id: int) -> list[SubjectRecord]:
    """The clip as seen for ONE person: only that track's box, per frame."""
    view: list[SubjectRecord] = []
    for record in records:
        mine = [box for box in record.boxes if box.get("track_id") == track_id]
        view.append(
            SubjectRecord(
                frame=record.frame,
                pts_s=record.pts_s,
                boxes=mine[:1],
                track_id=track_id if mine else None,
                lost=not mine,
            )
        )
    return view


@dataclass
class SubjectPoseResult:
    """One subject's complete pose outcome, ready to publish."""

    label: str | None  # "track-33", or None for the unconfirmed default
    track_id: int | None
    action_segments_found: int
    selected_segments: list[ActionSegment]
    context_windows: list[tuple[int, int]]
    selection_payload: dict[str, Any]
    segment_states: list[dict[str, Any]] = field(default_factory=list)
    context_states: list[dict[str, Any]] = field(default_factory=list)
    pose_records: list[dict[str, Any]] = field(default_factory=list)
    context_records: list[dict[str, Any]] = field(default_factory=list)
    skeleton_spec_path: Path | None = None
    notes: list[str] = field(default_factory=list)
    cancelled: bool = False


def run_subject_pose(
    *,
    records: list[SubjectRecord],
    video: Path,
    out_dir: Path,
    pose_model: Any,
    track_id: int | None,
    fps: float,
    should_cancel: Callable[[], bool] | None = None,
) -> SubjectPoseResult:
    """Select actions and extract pose for one subject (or the dominant default)."""
    from tennis_backend.media.pose_extract import extract_pose

    label = subject_label(track_id) if track_id is not None else None
    view = records if track_id is None else track_view(records, track_id)
    action_segments = _subjects.derive_action_segments(view, fps=fps)
    selected = _subjects.select_action_segments(action_segments, max_actions=MAX_ACTIONS)
    head, tail = _subjects.pose_context_windows(view, selected)
    context_windows = [w for w in (head, tail) if w is not None]
    context_frames = sum(end - start + 1 for start, end in context_windows)
    window_frames = len(records)
    selected_frames = sum(s.frames for s in selected)
    excluded = window_frames - selected_frames
    payload: dict[str, Any] = {
        "max_actions": MAX_ACTIONS,
        "segments_found": len(action_segments),
        "selected_count": len(selected),
        "selected_segments": [
            {
                "start_frame": s.start_frame,
                "end_frame": s.end_frame,
                "start_s": s.start_s,
                "end_s": s.end_s,
                "frames": s.frames,
            }
            for s in selected
        ],
        "context_windows": [{"start_frame": a, "end_frame": b} for a, b in context_windows],
        "context_frames": context_frames,
        "window_frames": window_frames,
        "selected_frames": selected_frames,
        "excluded_frames": excluded,
        "selection": (
            "first 10 complete action segments, chronological; idle head/tail context"
            " is posed too, interior gaps between actions stay excluded"
        ),
        "note": (
            "frames outside selected segments and context windows carry no records"
            " (explicit 10-action cap, not a silent drop)"
        ),
    }
    if label is not None:
        payload["subject_id"] = label
    result = SubjectPoseResult(
        label=label,
        track_id=track_id,
        action_segments_found=len(action_segments),
        selected_segments=selected,
        context_windows=context_windows,
        selection_payload=payload,
    )
    tag = f"[{label}] " if label else ""
    result.notes.append(
        f"{tag}10-action cap: {len(selected)}/{len(action_segments)} segments selected,"
        f" {excluded} window frames excluded"
    )
    if not selected:
        result.notes.append(f"{tag}motion deferred: no action segments to analyze")

    windows: list[tuple[int, int, str, int | None]] = [
        (start, end, "context", None) for start, end in context_windows
    ]
    windows += [(s.start_frame, s.end_frame, "action", s.frames) for s in selected]
    windows.sort(key=lambda window: window[0])
    out_dir.mkdir(parents=True, exist_ok=True)

    for win_start, win_end, kind, win_frames in windows:
        if should_cancel is not None and should_cancel():
            result.cancelled = True
            result.notes.append(
                f"{tag}cancelled during pose extraction at window {win_start}-{win_end};"
                " remaining windows skipped"
            )
            break
        seg_recs = [r for r in view if win_start <= r.frame <= win_end]
        name = "segment" if kind == "action" else "context"
        states = result.segment_states if kind == "action" else result.context_states

        if track_id is None:
            # unconfirmed default: dominant subject per window (the historical
            # heuristic). Only boxes comparable to the window's tallest box
            # can create subject AMBIGUITY — tiny background figures do not.
            ids_in_seg: dict[int, int] = {}
            heights = [
                box["box"][3] - box["box"][1]
                for r in seg_recs
                for box in r.boxes
                if box.get("track_id") is not None
            ]
            reference_h = max(heights) if heights else 0
            for r in seg_recs:
                for box in r.boxes:
                    tid = box.get("track_id")
                    if tid is None:
                        continue
                    if box["box"][3] - box["box"][1] >= 0.5 * reference_h:
                        ids_in_seg[tid] = ids_in_seg.get(tid, 0) + 1
            tracked_n = sum(ids_in_seg.values())
        else:
            ids_in_seg = {track_id: sum(1 for r in seg_recs if r.boxes)}
            tracked_n = ids_in_seg[track_id]

        payload_state: dict[str, Any] = {
            name: [win_start, win_end],
            "frames": win_frames if kind == "action" else tracked_n,
        }
        if tracked_n == 0:
            payload_state["status"] = "skipped_no_subject"
            states.append(payload_state)
            continue
        majority_id = max(ids_in_seg, key=lambda k: ids_in_seg[k])
        share = ids_in_seg[majority_id] / tracked_n
        if track_id is None and len(ids_in_seg) > 1 and share <= 0.5:
            payload_state["status"] = "deferred_multi_subject"
            states.append(payload_state)
            result.notes.append(
                f"{name} {win_start}-{win_end}: multiple subjects, pose deferred (select one)"
            )
            continue

        full_boxes: list[list[int] | None] = [None] * (win_end + 1)
        for r in seg_recs:
            for box in r.boxes:
                if box.get("track_id") == majority_id and full_boxes[r.frame] is None:
                    full_boxes[r.frame] = list(box["box"])
        outcome = extract_pose(
            video=video,
            subject_boxes=full_boxes,
            out_dir=out_dir,
            model=pose_model,
            origin_frame=win_start,
            segments=[(win_start, win_end)],
            should_cancel=should_cancel,
        )
        (result.pose_records if kind == "action" else result.context_records).extend(
            outcome.records
        )
        if result.skeleton_spec_path is None:
            result.skeleton_spec_path = outcome.skeleton_spec_path
        payload_state["status"] = "posed"
        payload_state["pose_frames"] = outcome.frames_with_pose
        states.append(payload_state)
        result.notes.append(
            f"{tag}{name} {win_start}-{win_end}: {outcome.frames_with_pose} frames posed"
            + (" (idle before/after play)" if kind == "context" else "")
        )
    return result
