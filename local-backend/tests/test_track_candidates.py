"""Far-view candidates from a WASB ball track (``ball_track`` basis).

The cases pin the honesty rules, not accuracy: ``estimated`` hold-fill points
never create or feed a turnaround, a smooth track yields nothing, a sharp
reversal yields one candidate, and two reversals closer than the minimum gap
collapse to the sharper one.
"""

from __future__ import annotations

from tennis_backend.analysis.track_candidates import (
    MIN_TURN_DEGREES,
    candidates_from_track,
    find_turnarounds,
    measured_points,
)
from tennis_backend.media.wasb_track import BallTrack, TrackPoint

FPS = 60.0


def track_of(points: dict[int, tuple[float, float]], *, source: str = "detected") -> BallTrack:
    track = BallTrack(fps=FPS, width=886, height=498, frames=max(points) + 10)
    track.points = {
        frame: TrackPoint(x=x, y=y, source=source, peak=0.8) for frame, (x, y) in points.items()
    }
    return track


def straight(
    frames: range, *, x0: float = 100.0, step: float = 4.0
) -> dict[int, tuple[float, float]]:
    return {f: (x0 + step * (f - frames.start), 200.0) for f in frames}


def reversal(around: int = 60, span: int = 20) -> dict[int, tuple[float, float]]:
    """Ball moving right into ``around``, then leaving to the left (a bounce)."""
    points: dict[int, tuple[float, float]] = {}
    for offset in range(span, 0, -1):
        points[around - offset] = (300.0 - 5.0 * offset, 200.0)
    points[around] = (300.0, 200.0)
    for offset in range(1, span + 1):
        points[around + offset] = (300.0 - 5.0 * offset, 200.0)
    return points


def test_a_straight_track_has_no_turnaround() -> None:
    track = track_of(straight(range(0, 120)))
    assert find_turnarounds(track) == []


def test_a_reversal_is_found_once() -> None:
    events = find_turnarounds(track_of(reversal()))
    assert len(events) == 1
    assert events[0].frame == 60
    assert events[0].degrees > MIN_TURN_DEGREES


def test_estimated_points_never_create_a_turnaround() -> None:
    """A reversal made only of hold-fill is not a measurement."""
    track = track_of(reversal(), source="estimated")
    assert measured_points(track) == {}
    assert find_turnarounds(track) == []


def test_estimated_points_on_one_side_suppress_the_event() -> None:
    points = reversal()
    track = BallTrack(fps=FPS, width=886, height=498, frames=120)
    track.points = {
        frame: TrackPoint(x=x, y=y, source="estimated" if frame > 60 else "detected", peak=0.8)
        for frame, (x, y) in points.items()
    }
    assert find_turnarounds(track) == []


def test_candidates_use_the_ball_track_basis_and_the_first_rung() -> None:
    candidates = candidates_from_track(track_of(reversal()))
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.basis == "ball_track"
    assert candidate.window_level == 20
    assert candidate.anchor_frame == 60
    assert (candidate.start_frame, candidate.end_frame) == (40, 80)
    assert 0.0 <= candidate.confidence <= 1.0


def test_two_close_reversals_collapse_to_the_sharper_one() -> None:
    points = reversal(around=60, span=12)
    # a second, gentler reversal 6 frames later: inside the 15-frame gap
    for offset in range(1, 6):
        points[66 + offset] = (300.0 - 2.0 * offset, 200.0)
    events = find_turnarounds(track_of(points))
    assert len(events) == 1
    assert events[0].frame == 60


def test_candidates_never_start_before_frame_zero() -> None:
    points = {f: (5.0 * f, 200.0) for f in range(0, 12)}
    for offset in range(1, 12):
        points[11 + offset] = (55.0 - 5.0 * offset, 200.0)
    candidates = candidates_from_track(track_of(points))
    assert candidates
    assert candidates[0].start_frame >= 0
