"""Far-view hit candidates from a WASB ball track (no COCO here).

Near-camera video has usable ball/racket boxes, so :mod:`tennis_backend.analysis.hits`
finds anchors from their distance. In far view the ball is 3-6 px and the
racket box is absent, so the only signal is the **ball track itself**: contact
is the point where the ball stops approaching and starts leaving.

This module turns track turnarounds into the same :class:`HitCandidate` shape
the near-view path produces, so storage, retention and the expert's retrieval
work identically for both. What it must not do:

- use ``estimated`` points as evidence. They are hold-fill between two
  detections (the ball is invisible on the strings); a turnaround measured
  across them is a guess about a period the detector could not see. They are
  excluded from the measurement, and a turnaround that only exists because of
  them is never produced;
- pretend the confidence is calibrated: it is a monotone function of how sharp
  the direction change is, nothing more (see the honesty note in ``hits``).

Measured limits (B0 experiment record): the ball is invisible to the detector
while it is on the strings and while a leg hides it, so the true contact can
sit inside a short gap on either side of the returned candidate; the returned
interval therefore stays the same ``+-START_WINDOW`` the near-view path uses.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from tennis_backend.analysis.hits import HitCandidate
from tennis_backend.media.wasb_track import BallTrack

#: Point sources that count as a measurement of the ball.
_MEASURED = frozenset({"detected", "recovered", "lowthr"})

#: Frames each side used to estimate the incoming and outgoing direction.
_DIRECTION_SPAN = 5

#: Minimum number of measured points on each side of a turnaround.
_MIN_SIDE_POINTS = 3

#: Minimum direction change (degrees) to call it a turnaround.
MIN_TURN_DEGREES = 60.0

#: Minimum separation between two turnarounds (frames), same as the near-view
#: anchor gap so the two bases produce comparable candidate counts.
TURN_MIN_GAP = 15


@dataclass(frozen=True)
class TrackEvent:
    """One turnaround of the ball track."""

    frame: int
    degrees: float


def measured_points(track: BallTrack) -> dict[int, tuple[float, float]]:
    """Track points that are measurements, never ``estimated`` hold-fill."""
    return {
        frame: (point.x, point.y)
        for frame, point in track.points.items()
        if point.source in _MEASURED
    }


def _direction(
    points: dict[int, tuple[float, float]], frames: list[int]
) -> tuple[float, float] | None:
    if len(frames) < _MIN_SIDE_POINTS:
        return None
    first, last = frames[0], frames[-1]
    span = last - first
    if span <= 0:
        return None
    a, b = points[first], points[last]
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    if length <= 0:
        return None
    return ((b[0] - a[0]) / length, (b[1] - a[1]) / length)


def _angle(a: tuple[float, float], b: tuple[float, float]) -> float:
    dot = max(-1.0, min(1.0, a[0] * b[0] + a[1] * b[1]))
    return math.degrees(math.acos(dot))


def _turn_angle(
    points: dict[int, tuple[float, float]], frames: list[int], position: int, span: int
) -> float | None:
    """Direction change at ``frames[position]``, measured on its two sides.

    The incoming direction is the motion from ``span`` frames before up to the
    frame itself; the outgoing one is from the frame out to ``span`` frames
    after. ``None`` when either side has too few measured points. The frame
    under test is the shared endpoint, so the two sides cannot leak into each
    other.

    A sharp reversal registers a high angle on every frame of a short stretch
    around it (each of them sits between the approaching and the departing
    stroke). The stretch is narrowed to its vertex by :func:`_vertex`, which
    is why the returned frame is where the ball actually turned.
    """
    left = frames[max(0, position - span) : position + 1]
    right = frames[position : position + 1 + span]
    if len(left) <= _MIN_SIDE_POINTS or len(right) <= _MIN_SIDE_POINTS:
        return None
    incoming = _direction(points, left)
    outgoing = _direction(points, right)
    if incoming is None or outgoing is None:
        return None
    return _angle(incoming, outgoing)


def _vertex(
    points: dict[int, tuple[float, float]], frames: list[int], position: int, span: int
) -> int:
    """The apex of the reversal the test at ``frames[position]`` fired on.

    A reversal is an extreme of the track along its main axis: a ball that
    flies right and comes back is at its rightmost point at contact. Within the
    ``position +- span`` window the frame farthest along that axis is therefore
    the turn — and using the extreme rather than "the first frame where the
    angle passed the threshold" is what keeps the event off the shoulder of the
    turn (which is where the angle test fires first).
    """
    window = frames[max(0, position - span) : position + 1 + span]
    if not window:
        return frames[position]
    start, end = points[window[0]], points[window[-1]]
    dx, dy = end[0] - start[0], end[1] - start[1]
    if abs(dx) >= abs(dy):
        return max(window, key=lambda f: points[f][0] * (1 if dx >= 0 else -1))
    return max(window, key=lambda f: points[f][1] * (1 if dy >= 0 else -1))


def find_turnarounds(
    track: BallTrack,
    *,
    span: int = _DIRECTION_SPAN,
    threshold: float = MIN_TURN_DEGREES,
    min_gap: int = TURN_MIN_GAP,
) -> list[TrackEvent]:
    """Frames where the measured ball direction reverses sharply.

    Only local maxima of the turn angle qualify, so a run of frames inside one
    reversal yields a single event rather than one per frame; the event frame
    is then moved to the apex of that run (:func:`_vertex`). Frames are visited
    in order, and an event closer than ``min_gap`` to the previous one replaces
    it only when it is sharper — the same rule the near-view anchor search
    uses.
    """
    points = measured_points(track)
    frames = sorted(points)
    if len(frames) < 2 * _MIN_SIDE_POINTS:
        return []
    angles: list[float | None] = []
    for position in range(len(frames)):
        angles.append(_turn_angle(points, frames, position, span))
    events: list[TrackEvent] = []
    for position in range(len(frames)):
        degrees = angles[position]
        if degrees is None or degrees < threshold:
            continue
        neighbours = [
            a
            for a in (
                angles[position - 1] if position > 0 else None,
                angles[position + 1] if position + 1 < len(frames) else None,
            )
            if a is not None
        ]
        if neighbours and degrees < max(neighbours):
            continue
        event = TrackEvent(frame=_vertex(points, frames, position, span), degrees=degrees)
        if events and event.frame - events[-1].frame < min_gap:
            if degrees > events[-1].degrees:
                events[-1] = event
            continue
        events.append(event)
    return events


def _confidence(degrees: float) -> float:
    """Sharper reversal, stronger evidence; not a calibrated probability."""
    return max(0.0, min(1.0, (degrees - MIN_TURN_DEGREES) / (180.0 - MIN_TURN_DEGREES)))


def candidates_from_track(
    track: BallTrack, *, fps: float | None = None, ladder: tuple[int, ...] = (20, 40, 80)
) -> list[HitCandidate]:
    """One ``ball_track`` candidate per turnaround.

    ``window_level`` is the smallest ladder rung that contains the event, so a
    consumer that knows only the near-view ladder sees a comparable meaning:
    the first rung is a direct turnaround, later rungs a weaker one. The
    interval is always ``+-ladder[0]`` frames around the turnaround, whatever
    the rung, matching :func:`tennis_backend.analysis.hits.candidate_for`.
    """
    rate = fps if fps is not None else track.fps
    events = find_turnarounds(track)
    candidates: list[HitCandidate] = []
    for event in events:
        half = ladder[0]
        start = max(event.frame - half, 0)
        end = min(event.frame + half, max(track.frames - 1, event.frame))
        candidates.append(
            HitCandidate(
                start_frame=start,
                end_frame=end,
                anchor_frame=event.frame,
                basis="ball_track",
                window_level=half,
                confidence=_confidence(event.degrees),
            )
        )
    _ = rate  # the timeframe is already in frames; kept for symmetry with callers
    return candidates
