"""Turn a ball/racket scan into stored per-frame evidence (no model here).

Given the per-frame detections of one video, this builds what the expert later
retrieves: a metadata row for **every** frame, the hit candidates (anchors with
their intervals) and the retained frame ranges that decide which images are
kept.

Retention follows the task route (user decision 2026-11-16):

- ``technical_diagnosis`` + ``route_trimmed``: only the 3-4 strongest actions,
  at most 80 frames each, around their anchors;
- ``tactics`` or ``retain_mode == "full"``: the whole analysed span;
- anything else keeps no images by default (metadata is still stored).

Metadata describes what was detected. It never says "this is a hit": anchors
appear on walking and ball-in-hand frames too, and the expert decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tennis_backend.analysis import hits
from tennis_backend.media.ball_detect import BallRacketScan
from tennis_backend.storage.evidence_store import (
    ANALYSIS_FRAMES_PER_ACTION,
    ANALYSIS_MAX_ACTIONS,
    EvidenceFrame,
    EvidenceStore,
    StoredCandidate,
)

#: Routes whose evidence is the whole analysed span.
_FULL_SPAN_ROUTES = frozenset({"tactics"})

#: ``basis_error_frames`` for the ball-first source, measured on near-camera
#: video (largest offset seen across Flow and 400).
BALL_RACKET_ERROR_FRAMES = 7


@dataclass(frozen=True)
class EvidenceSummary:
    """What a build stored; also what a storage estimate is based on."""

    frames: int
    candidates: int
    retained_ranges: list[tuple[int, int]]
    refused: int = 0

    @property
    def retained_frames(self) -> int:
        return sum(hi - lo + 1 for lo, hi in self.retained_ranges)


def _best(boxes: list[list[float]], floor: float) -> list[float] | None:
    kept = [b for b in boxes if b[4] >= floor]
    return max(kept, key=lambda b: b[4]) if kept else None


def plan_retention(
    scan: BallRacketScan,
    candidates: list[hits.HitCandidate],
    *,
    route: str,
    retain_mode: str,
) -> list[tuple[int, int]]:
    """Which frame ranges keep their images for this route and mode."""
    if not scan.frames:
        return []
    if retain_mode == "full" or route in _FULL_SPAN_ROUTES:
        return [(min(scan.frames), max(scan.frames))]
    if route == "technical_diagnosis":
        return hits.retained_frames(
            candidates,
            per_action_frames=ANALYSIS_FRAMES_PER_ACTION,
            max_actions=ANALYSIS_MAX_ACTIONS,
        )
    return []


def build_candidates(scan: BallRacketScan) -> list[hits.HitCandidate]:
    """One candidate per ball-racket anchor; refusals are not candidates."""
    series = hits.distance_series(scan.frames)
    found: dict[int, hits.HitCandidate] = {}
    for anchor in hits.find_anchors(series):
        result = hits.candidate_for(scan.frames, anchor, fps=scan.fps)
        if isinstance(result, hits.Refusal):
            continue
        found.setdefault(result.anchor_frame, result)
    return sorted(found.values(), key=lambda c: c.anchor_frame)


def build_evidence(
    store: EvidenceStore,
    scan: BallRacketScan,
    *,
    task_id: str,
    version: int,
    route: str,
    retain_mode: str,
    uploaded_at: datetime,
    pose_valid: dict[int, bool] | None = None,
) -> EvidenceSummary:
    """Store frames, candidates and retention for one task version."""
    candidates = build_candidates(scan)
    series = hits.distance_series(scan.frames)
    anchor_frames = hits.find_anchors(series)
    ids = {c.anchor_frame: f"cand-{c.anchor_frame}" for c in candidates}
    pose = pose_valid or {}

    def candidate_of(frame: int) -> str | None:
        for candidate in candidates:
            if candidate.start_frame <= frame <= candidate.end_frame:
                return ids[candidate.anchor_frame]
        return None

    rows: list[EvidenceFrame] = []
    for frame in sorted(scan.frames):
        entry = scan.frames[frame]
        ball = _best(entry.get("ball", []), hits.BALL_CONFIDENCE)
        racket = _best(entry.get("racket", []), hits.RACKET_CONFIDENCE)
        nearest = min((abs(frame - a) for a in anchor_frames), default=None)
        rows.append(
            EvidenceFrame(
                frame=frame,
                time_s=frame / scan.fps,
                ball=None if ball is None else ball[:4],
                ball_confidence=None if ball is None else ball[4],
                racket=None if racket is None else racket[:4],
                racket_confidence=None if racket is None else racket[4],
                pose_valid=pose.get(frame, False),
                markers=("ball_racket_anchor",) if frame in anchor_frames else (),
                candidate_id=candidate_of(frame),
                frames_to_anchor=nearest,
            )
        )
    store.record_frames(task_id, version, rows, uploaded_at=uploaded_at)
    store.record_candidates(
        task_id,
        version,
        [
            StoredCandidate(
                candidate_id=ids[c.anchor_frame],
                interval_start=c.start_frame,
                interval_end=c.end_frame,
                anchor_frame=c.anchor_frame,
                basis=c.basis,
                basis_error_frames=BALL_RACKET_ERROR_FRAMES,
                confidence=c.confidence,
                window_level=c.window_level,
            )
            for c in candidates
        ],
    )
    ranges = plan_retention(scan, candidates, route=route, retain_mode=retain_mode)
    for number, (lo, hi) in enumerate(ranges):
        store.record_retention(
            task_id,
            version,
            start_frame=lo,
            end_frame=hi,
            uploaded_at=uploaded_at,
            label=f"retained-{number}",
        )
    return EvidenceSummary(frames=len(rows), candidates=len(candidates), retained_ranges=ranges)
