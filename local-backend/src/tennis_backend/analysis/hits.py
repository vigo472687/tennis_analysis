"""Ball-first hit candidates over per-frame ball/racket detections.

The expert judges "hit point early or late" mainly from posture; the backend's
job is to hand over a *candidate interval*, never a frame-precise contact
time (FR-POSE-004, AC-POSE-004). This module turns per-frame COCO
``sports ball`` / ``tennis racket`` detections into such candidates without
any model inference of its own:

1. per frame, the minimum normalised distance between a ball box centre and a
   racket box centre (divided by the racket box's longer side);
2. anchors = local minima of that distance below :data:`ANCHOR_DISTANCE`,
   with a minimum gap between anchors;
3. refinement inside ``+-REFINE_WINDOW`` frames: the frame with the largest
   ball velocity change (the ball's direction reversing is what contact does
   to it), when at least two ball points exist on each side;
4. widening: a representative frame with no anchor in its window widens
   20 -> 40 -> 80 frames per side; after two widenings the answer is a
   refusal, never a fabricated anchor.

Honesty boundaries (measured, see the B0 experiment record):

- a fluorescent-green racket head is detected as a ball, so a ball box that
  nearly coincides with a racket box is one of the three recurring false
  anchor sources (:func:`ball_on_racket`);
- a ball held in the hand near the racket satisfies the distance test while
  walking, picking up balls or preparing to serve;
- background discs (score plaques) are detected as balls;
- finding an anchor does NOT mean "a hit happened here": on non-hit frames
  the widening still finds anchors 14-27% of the time at +-20 and up to 62%
  at +-80. The returned ``window_level`` says which widening produced the
  anchor so the expert can weigh it.

No I/O, deterministic, pure functions over detection dictionaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: A ball/racket pair counts as "together" when their centre distance,
#: divided by the racket box's longer side, is at most this.
ANCHOR_DISTANCE = 0.7

#: Minimum separation between two anchors (frames).
ANCHOR_MIN_GAP = 15

#: Locally-low search radius for an anchor (frames).
ANCHOR_LOCAL_WINDOW = 6

#: Default starting half-window around a candidate's representative frame.
START_WINDOW = 20

#: Widening ladder, applied in order; after the last level the answer is a
#: refusal. Frozen values, not configurable (user decision 2026-11-16).
WIDENING_LADDER: tuple[int, ...] = (20, 40, 80)

#: Frames to look each side of a candidate frame when computing a velocity change.
VELOCITY_SPAN = 4

#: Minimum ball points needed on each side of a refinement frame.
_MIN_BALL_POINTS = 2

#: Minimum ball confidence for a point to take part (COCO detections).
BALL_CONFIDENCE = 0.4
#: Minimum racket confidence for a box to take part.
RACKET_CONFIDENCE = 0.3


@dataclass(frozen=True)
class HitCandidate:
    """One hit candidate: an interval, its basis and how it was found."""

    start_frame: int
    end_frame: int
    anchor_frame: int
    basis: str
    window_level: int
    confidence: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "interval_start_frame": self.start_frame,
            "interval_end_frame": self.end_frame,
            "anchor_frame": self.anchor_frame,
            "basis": self.basis,
            "window_level": self.window_level,
            "confidence": round(self.confidence, 3),
        }


@dataclass(frozen=True)
class Refusal:
    """No anchor could be found even after widening twice."""

    representative_frame: int
    widenings: int
    reason: str = "no_candidate"

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "representative_frame": self.representative_frame,
            "widenings": self.widenings,
            "message": "该区间未找到击球候选",
        }


def _centre(box: list[float]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _side(box: list[float]) -> float:
    return max(box[2] - box[0], box[3] - box[1], 1.0)


def ball_racket_distance(
    balls: list[list[float]], rackets: list[list[float]]
) -> tuple[float, list[float], list[float]] | None:
    """Closest normalised ball-racket distance in one frame.

    Boxes are ``[x1, y1, x2, y2, confidence]``. Returns ``(distance, ball,
    racket)`` for the closest pair, or ``None`` when either list is empty.
    """
    if not balls or not rackets:
        return None
    best: tuple[float, list[float], list[float]] | None = None
    for ball in balls:
        bc = _centre(ball)
        for racket in rackets:
            rc = _centre(racket)
            distance = ((bc[0] - rc[0]) ** 2 + (bc[1] - rc[1]) ** 2) ** 0.5 / _side(racket)
            if best is None or distance < best[0]:
                best = (distance, ball, racket)
    return best


def ball_on_racket(ball: list[float], racket: list[float], *, tolerance: float = 0.25) -> bool:
    """True when the ball box sits almost entirely inside the racket box.

    A fluorescent racket head is detected as a ball; its "ball" box coincides
    with the racket box. Reported as a false-anchor source, never dropped here
    (the expert verifies with the images; the backend does not filter).
    """
    bc, rc = _centre(ball), _centre(racket)
    if (abs(bc[0] - rc[0]) + abs(bc[1] - rc[1])) > tolerance * _side(racket):
        return False
    ball_side = _side(ball)
    return ball_side <= tolerance * 2.0 * _side(racket)


def distance_series(
    detections: dict[int, dict[str, list[list[float]]]],
    *,
    ball_confidence: float = BALL_CONFIDENCE,
    racket_confidence: float = RACKET_CONFIDENCE,
) -> dict[int, float | None]:
    """Per-frame closest ball-racket distance, ``None`` where either is missing."""
    series: dict[int, float | None] = {}
    for frame, entry in detections.items():
        balls = [b for b in entry.get("ball", []) if b[4] >= ball_confidence]
        rackets = [r for r in entry.get("racket", []) if r[4] >= racket_confidence]
        closest = ball_racket_distance(balls, rackets)
        series[frame] = None if closest is None else closest[0]
    return series


def find_anchors(
    series: dict[int, float | None],
    *,
    threshold: float = ANCHOR_DISTANCE,
    min_gap: int = ANCHOR_MIN_GAP,
    local_window: int = ANCHOR_LOCAL_WINDOW,
) -> list[int]:
    """Frames that are local minima of the distance series below ``threshold``."""
    anchors: list[int] = []
    frames = sorted(series)
    for position, frame in enumerate(frames):
        value = series[frame]
        if value is None or value > threshold:
            continue
        window = [
            v
            for f in frames[max(0, position - local_window) : position + local_window + 1]
            if (v := series[f]) is not None
        ]
        if not window or value > min(window):
            continue
        if anchors and frame - anchors[-1] < min_gap:
            if value < (series[anchors[-1]] or value):
                anchors[-1] = frame
            continue
        anchors.append(frame)
    return anchors


def ball_positions(
    detections: dict[int, dict[str, list[list[float]]]],
    *,
    ball_confidence: float = BALL_CONFIDENCE,
) -> dict[int, tuple[float, float]]:
    """Highest-confidence ball centre per frame."""
    out: dict[int, tuple[float, float]] = {}
    for frame, entry in detections.items():
        balls = [b for b in entry.get("ball", []) if b[4] >= ball_confidence]
        if balls:
            out[frame] = _centre(max(balls, key=lambda b: b[4]))
    return out


def _mean_velocity(
    positions: dict[int, tuple[float, float]], frames: list[int]
) -> tuple[float, float] | None:
    if len(frames) < _MIN_BALL_POINTS:
        return None
    first, last = frames[0], frames[-1]
    span = last - first
    if span <= 0:
        return None
    start, end = positions[first], positions[last]
    return ((end[0] - start[0]) / span, (end[1] - start[1]) / span)


def velocity_change(
    positions: dict[int, tuple[float, float]], frame: int, span: int = VELOCITY_SPAN
) -> float | None:
    """Size of the ball's velocity change across ``frame`` (px/frame)."""
    before = [f for f in range(frame - span, frame) if f in positions]
    after = [f for f in range(frame + 1, frame + span + 1) if f in positions]
    vin = _mean_velocity(positions, before)
    vout = _mean_velocity(positions, after)
    if vin is None or vout is None:
        return None
    change: float = ((vout[0] - vin[0]) ** 2 + (vout[1] - vin[1]) ** 2) ** 0.5
    return change


def refine_anchor(
    anchor: int, positions: dict[int, tuple[float, float]], *, window: int = START_WINDOW
) -> tuple[int, float]:
    """Largest ball velocity change inside ``anchor +- window``.

    Returns ``(frame, change)``; falls back to ``(anchor, 0.0)`` when no frame
    has enough ball points on both sides (a gap is kept as a gap).
    """
    best: tuple[int, float] | None = None
    lo = max(0, anchor - window)
    for frame in range(lo, anchor + window + 1):
        change = velocity_change(positions, frame)
        if change is not None and (best is None or change > best[1]):
            best = (frame, change)
    return best if best is not None else (anchor, 0.0)


def anchors_near(anchors: list[int], frame: int, half_window: int) -> list[int]:
    return [a for a in anchors if abs(a - frame) <= half_window]


def _confidence(distance: float | None) -> float:
    """Closer ball-to-racket at the anchor is stronger evidence.

    Only a monotone mapping from the measured distance; it is not calibrated
    against labels and must not be read as a probability.
    """
    if distance is None:
        return 0.0
    return max(0.0, min(1.0, 1.0 - distance / ANCHOR_DISTANCE))


def candidate_for(
    detections: dict[int, dict[str, list[list[float]]]],
    representative_frame: int,
    *,
    fps: float = 30.0,
    ladder: tuple[int, ...] = WIDENING_LADDER,
    basis: str = "ball_racket",
) -> HitCandidate | Refusal:
    """One candidate (or a refusal) around ``representative_frame``.

    ``representative_frame`` is the frame the candidate is *about* (an anchor,
    a ball-track event, or a wrist peak). Anchors inside ``+-ladder[0]`` are
    used first; only when there are none is the window widened. The interval
    always covers the anchor +- ``ladder[0]`` frames so the expert receives the
    approach and the follow-through, whichever level produced the anchor.
    """
    series = distance_series(detections)
    anchors = find_anchors(series)
    for half_window in ladder:
        near = anchors_near(anchors, representative_frame, half_window)
        if not near:
            continue
        anchor = min(near, key=lambda a: abs(a - representative_frame))
        positions = ball_positions(detections)
        refined, _ = refine_anchor(anchor, positions, window=START_WINDOW)
        margin = ladder[0]
        start = max(min(anchor, refined) - margin, min(detections) if detections else 0)
        end = max(anchor, refined) + margin
        return HitCandidate(
            start_frame=start,
            end_frame=end,
            anchor_frame=refined,
            basis=basis,
            window_level=half_window,
            confidence=_confidence(series.get(anchor)),
        )
    return Refusal(representative_frame=representative_frame, widenings=len(ladder) - 1)


def interval_seconds(start_frame: int, end_frame: int, fps: float) -> tuple[float, float]:
    if fps <= 0:
        return (0.0, 0.0)
    return (start_frame / fps, end_frame / fps)


def retained_frames(
    candidates: list[HitCandidate], *, per_action_frames: int = 80, max_actions: int = 4
) -> list[tuple[int, int]]:
    """Frame ranges worth keeping for a technical-diagnosis task.

    Keeps at most ``max_actions`` candidates (the strongest confidence first,
    then re-ordered by time), each trimmed to at most ``per_action_frames``
    frames centred on its anchor. Overlapping ranges merge.
    """
    chosen = sorted(candidates, key=lambda c: (-c.confidence, c.anchor_frame))[:max_actions]
    ranges: list[tuple[int, int]] = []
    for candidate in sorted(chosen, key=lambda c: c.start_frame):
        half = per_action_frames // 2
        lo = max(candidate.anchor_frame - half, candidate.start_frame)
        hi = min(candidate.anchor_frame + half, candidate.end_frame)
        if ranges and lo <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], hi))
        else:
            ranges.append((lo, hi))
    return ranges
