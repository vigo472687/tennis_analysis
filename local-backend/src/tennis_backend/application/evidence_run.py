"""One end-to-end evidence run: video in, stored frames and images out.

This is the missing link between the detectors and what the expert retrieves.
It runs the steps in a fixed order and never decides whether a hit happened:

1. pick the detector (the expert's choice, else the backend fallback rule) and
   read the video with exactly one of them — never both;
2. near view (COCO): per-frame ball/racket boxes feed the ball-first anchors;
   far view (WASB): the ball track's turnarounds feed ``ball_track``
   candidates. Far view has no usable racket box, so it produces no near-view
   anchors;
3. store one metadata row per analysed frame plus the candidates;
4. plan retention from the route and the user's approved ``retain_mode``;
5. write the images of the **retained** ranges only.

Storage: :func:`estimate` answers "how much will this need" *before* an image
is written, from a cheap strided detection pass, so the user can choose a full
run or a trimmed one. The estimate counts frames and applies a measured
bytes-per-image figure; it does not render.

The steps are separate functions so a caller can estimate, get the user's
approval, and only then run — the order the user decided (2026-11-16).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tennis_backend.analysis import hits as hit_rules
from tennis_backend.analysis import track_candidates
from tennis_backend.analysis.hits import HitCandidate
from tennis_backend.application.evidence_builder import (
    EvidenceSummary,
    build_candidates,
    build_evidence,
    plan_retention,
)
from tennis_backend.application.worker import CancellationToken
from tennis_backend.domain.models import DomainViolation
from tennis_backend.evidence.frame_images import FrameImage, retained_only, write_frames
from tennis_backend.media.ball_detect import (
    BallRacketScan,
    RFDetrBallRacketDetector,
    detect_video,
)
from tennis_backend.media.wasb_track import BallTrack, HeatmapModel, track_video
from tennis_backend.storage.evidence_store import EvidenceFrame, EvidenceStore

#: Byte figure per stored frame: annotated 720p JPEG plus a 160x160 close-up,
#: measured on the B0 experiment frames.
BYTES_PER_FRAME = 29_000

#: Stride of the cheap detection pass used only for a storage estimate.
ESTIMATE_STRIDE = 4

#: Far-view ball box side (px): the ball itself is 3-6 px in broadcast video.
FAR_BOX_PX = 32

#: Detector values the backend accepts; the expert chooses, never both.
DETECTORS = {"coco", "wasb"}

_FULL = "full"
_NEAR_HEIGHT_RATIO = 0.28


@dataclass
class RunOutcome:
    """What one evidence run did, in the terms the caller reports back."""

    detector: str
    fallback: bool
    summary: EvidenceSummary
    images: tuple[FrameImage, ...] = ()
    notes: list[str] = field(default_factory=list)


def choose_detector(subject_height_ratio: float | None, requested: str | None) -> tuple[str, bool]:
    """The detector to use, and whether the backend picked it by fallback."""
    if requested is not None:
        if requested not in DETECTORS:
            raise DomainViolation(
                "DETECTOR_UNKNOWN", f"detector {requested!r} is not one of {sorted(DETECTORS)}"
            )
        return requested, False
    if subject_height_ratio is not None and subject_height_ratio >= _NEAR_HEIGHT_RATIO:
        return "coco", True
    return "wasb", True


def scan_near(
    video: Path, *, cancel: CancellationToken | None = None, max_seconds: float = 180.0
) -> BallRacketScan:
    """COCO ball/racket scan of a near-camera video."""
    return detect_video(video, RFDetrBallRacketDetector(), cancel=cancel, max_seconds=max_seconds)


def track_far(
    video: Path, *, cancel: CancellationToken | None = None, max_seconds: float = 180.0
) -> BallTrack:
    """WASB ball track of a far-view video (one inference pass)."""
    return track_video(video, HeatmapModel(), cancel=cancel, max_seconds=max_seconds)


def far_frames(track: BallTrack, candidates: list[HitCandidate]) -> list[EvidenceFrame]:
    """Metadata rows for far view: ball centre and markers, nothing invented.

    There is no racket box and no detection confidence in far view, so a row
    carries the ball position (a fixed :data:`FAR_BOX_PX` square around the
    centre), the heatmap peak as its confidence, and the bases whose anchor
    lands on the frame.
    """
    anchors = {c.anchor_frame: c for c in candidates}
    half = FAR_BOX_PX / 2
    rows: list[EvidenceFrame] = []
    for frame in sorted(track.points):
        point = track.points[frame]
        markers = tuple(sorted(f"{c.basis}_anchor" for a, c in anchors.items() if a == frame))
        rows.append(
            EvidenceFrame(
                frame=frame,
                time_s=frame / track.fps,
                ball=[point.x - half, point.y - half, point.x + half, point.y + half],
                ball_confidence=point.peak,
                pose_valid=False,
                markers=markers,
                candidate_id=None,
                frames_to_anchor=min((abs(frame - a) for a in anchors), default=None),
            )
        )
    return rows


def run(
    video: Path,
    *,
    public_root: Path,
    relative_evidence_dir: str,
    store: EvidenceStore,
    task_id: str,
    version: int,
    route: str,
    retain_mode: str,
    uploaded_at: datetime,
    detector: str | None = None,
    subject_height_ratio: float | None = None,
    cancel: CancellationToken | None = None,
    max_seconds: float = 180.0,
) -> RunOutcome:
    """Detect, store, plan retention and write the retained images.

    ``detector`` is the expert's answer (``coco`` / ``wasb``); ``None`` means
    the backend applied its fallback rule, and the result says so, so the
    expert knows to re-check the choice.
    """
    chosen, fell_back = choose_detector(subject_height_ratio, detector)
    notes: list[str] = []
    if fell_back:
        notes.append(
            f"backend fallback chose the {chosen} detector "
            f"(subject height ratio {subject_height_ratio}); please re-check"
        )
    extra: list[HitCandidate] = []
    if chosen == "coco":
        scan = scan_near(video, cancel=cancel, max_seconds=max_seconds)
        if scan.truncated:
            notes.append(f"video cut to the first {max_seconds:.0f} s (FR-002)")
    else:
        track = track_far(video, cancel=cancel, max_seconds=max_seconds)
        extra = track_candidates.candidates_from_track(track, fps=track.fps)
        scan = BallRacketScan(fps=track.fps, width=track.width, height=track.height)
        store.record_frames(task_id, version, far_frames(track, extra), uploaded_at=uploaded_at)
    summary = build_evidence(
        store,
        scan,
        task_id=task_id,
        version=version,
        route=route,
        retain_mode=retain_mode,
        uploaded_at=uploaded_at,
        extra_candidates=extra,
    )
    ranges = store.retained_ranges(task_id, version)
    images = write_frames(
        video,
        public_root,
        relative_evidence_dir,
        retained_only(store.between(task_id, version, 0, _MAX_FRAME), ranges),
        far=chosen == "wasb",
        cancel=cancel,
    )
    return RunOutcome(
        detector=chosen, fallback=fell_back, summary=summary, images=images, notes=notes
    )


def estimate(
    video: Path,
    *,
    route: str,
    retain_mode: str,
    detector: str | None = None,
    subject_height_ratio: float | None = None,
    bytes_per_frame: int = BYTES_PER_FRAME,
    max_seconds: float = 180.0,
) -> dict[str, Any]:
    """Frames and bytes this run would keep, before anything is written.

    A strided detection pass supplies the candidate anchors, so the ranges are
    accurate to that stride; the byte figure is a measured constant. Both are
    for the user's decision, not a promise. ``requires_confirmation`` is true
    for a full run, and the caller must forward it to the user.
    """
    chosen, fell_back = choose_detector(subject_height_ratio, detector)
    if chosen == "coco":
        scan = detect_video(
            video, RFDetrBallRacketDetector(), max_seconds=max_seconds, stride=ESTIMATE_STRIDE
        )
        candidates = build_candidates(scan)
        ranges = plan_retention(scan, candidates, route=route, retain_mode=retain_mode)
        span_end = max(scan.frames, default=0)
    else:
        track = track_far(video, max_seconds=max_seconds)
        candidates = track_candidates.candidates_from_track(track, fps=track.fps)
        ranges = hit_rules.retained_frames(candidates)
        span_end = max(track.points, default=0)
    if retain_mode == _FULL:
        ranges = [(0, span_end)] if span_end else []
    frames = sum(hi - lo + 1 for lo, hi in ranges if hi >= lo)
    return {
        "mode": retain_mode,
        "detector": chosen,
        "detector_fallback": fell_back,
        "retained_frames": frames,
        "estimated_bytes": frames * bytes_per_frame,
        "ranges": [{"start": lo, "end": hi} for lo, hi in ranges],
        "requires_confirmation": retain_mode == _FULL,
        "estimate_only": True,
    }


def purge_expired(
    *, store: EvidenceStore, metadata_path: Path, public_root: Path, now: datetime
) -> dict[str, int]:
    """Expire evidence in one step: database rows first, then their images.

    Runs the two deletions the retention policy promises in the order that can
    never leave images without a row (rows go first, so a crash leaves only
    orphans, which :func:`delete_expired_images` sweeps later).
    """
    from tennis_backend.evidence.frame_images import delete_expired_images
    from tennis_backend.storage.evidence_store import RETENTION

    rows = store.purge_expired(now=now)
    images = delete_expired_images(public_root, "projects", now=now, retention=RETENTION)
    _ = metadata_path
    return {"frames": rows, "images": images}


_MAX_FRAME = 10**9

__all__ = [
    "BYTES_PER_FRAME",
    "DETECTORS",
    "ESTIMATE_STRIDE",
    "FAR_BOX_PX",
    "RunOutcome",
    "choose_detector",
    "estimate",
    "far_frames",
    "purge_expired",
    "run",
    "scan_near",
    "track_far",
]
