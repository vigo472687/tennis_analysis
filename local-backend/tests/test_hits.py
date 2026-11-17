"""Tests for the ball-first hit candidate module.

Synthetic detections only: no model, no media. The numbers mirror the real
detector's behaviour recorded in the B0 experiment (ball and racket boxes as
``[x1, y1, x2, y2, confidence]`` in frame pixels).
"""

from __future__ import annotations

import pytest

from tennis_backend.analysis.hits import (
    ANCHOR_DISTANCE,
    WIDENING_LADDER,
    Refusal,
    ball_on_racket,
    ball_positions,
    ball_racket_distance,
    candidate_for,
    distance_series,
    find_anchors,
    interval_seconds,
    refine_anchor,
    retained_frames,
)

RACKET = [100.0, 100.0, 160.0, 220.0, 0.8]  # 60x120 -> side 120


def ball(x: float, y: float, confidence: float = 0.8) -> list[float]:
    return [x - 5, y - 5, x + 5, y + 5, confidence]


def frame_entry(ball_box: list[float] | None, racket_box: list[float] | None) -> dict:
    return {
        "ball": [] if ball_box is None else [ball_box],
        "racket": [] if racket_box is None else [racket_box],
    }


HIT_FRAME = 100


def approaching(frame: int, close: bool = False) -> dict:
    """Ball approaching the racket, closest at :data:`HIT_FRAME`, then leaving.

    ``close`` pins the ball onto the racket centre at ``frame`` (a single
    unambiguous anchor regardless of the surrounding trajectory).
    """
    if close:
        return frame_entry(ball(130.0, 160.0), RACKET)
    distance = abs(frame - HIT_FRAME) * 3.0 + 10.0
    return frame_entry(ball(130.0 - distance, 160.0), RACKET)


# ----------------------------------------------------------------------
# geometry
# ----------------------------------------------------------------------


def test_distance_is_normalised_by_racket_side() -> None:
    closest = ball_racket_distance([ball(190, 160)], [RACKET])
    assert closest is not None
    distance, _, _ = closest
    # centre distance 60 px / racket side 120 px
    assert distance == pytest.approx(0.5)


def test_distance_needs_both_objects() -> None:
    assert ball_racket_distance([], [RACKET]) is None
    assert ball_racket_distance([ball(130, 160)], []) is None


def test_low_confidence_pairs_are_not_used() -> None:
    series = distance_series({1: frame_entry(ball(130, 160, 0.2), RACKET)})
    assert series[1] is None


def test_ball_on_racket_flags_a_fluorescent_head() -> None:
    on_head = ball(130, 160)
    assert ball_on_racket(on_head, RACKET) is True
    assert ball_on_racket(ball(30, 160), RACKET) is False


# ----------------------------------------------------------------------
# anchors
# ----------------------------------------------------------------------


def test_anchor_is_the_closest_frame() -> None:
    detections = {f: approaching(f, close=(f == 10)) for f in range(1, 20)}
    series = distance_series(detections)
    assert find_anchors(series) == [10]
    assert series[10] is not None and series[10] <= ANCHOR_DISTANCE


def test_anchors_merge_within_the_minimum_gap() -> None:
    detections = {f: approaching(f, close=(f in (10, 12))) for f in range(1, 20)}
    detections[12] = frame_entry(ball(132.0, 160.0), RACKET)  # slightly closer
    anchors = find_anchors(distance_series(detections))
    assert len(anchors) == 1


def test_no_pair_no_anchor() -> None:
    detections = {f: frame_entry(ball(10.0, 10.0), None) for f in range(1, 20)}
    assert find_anchors(distance_series(detections)) == []


# ----------------------------------------------------------------------
# refinement
# ----------------------------------------------------------------------


def test_refine_moves_to_the_largest_velocity_change() -> None:
    positions = {}
    for f in range(1, 21):
        if f <= 10:
            positions[f] = (10.0 * f, 100.0)  # moving right
        else:
            positions[f] = (100.0 - 10.0 * (f - 10), 100.0)  # reversing
    frame, change = refine_anchor(11, positions)
    # the reversal makes both frames next to it tie: the refinement must land
    # on the reversal, never on the far side of the window
    assert frame in (9, 10, 11)
    assert change > 0


def test_refine_keeps_the_anchor_without_enough_points() -> None:
    frame, change = refine_anchor(15, {15: (1.0, 2.0)})
    assert (frame, change) == (15, 0.0)


# ----------------------------------------------------------------------
# candidates
# ----------------------------------------------------------------------


def test_candidate_intervals_cover_approach_and_follow_through() -> None:
    detections = {f: approaching(f, close=(f == 40)) for f in range(1, 80)}
    result = candidate_for(detections, 40)
    assert not isinstance(result, Refusal)
    assert result.window_level == WIDENING_LADDER[0]
    assert result.start_frame <= 40 <= result.end_frame
    assert result.end_frame - result.start_frame >= 2 * WIDENING_LADDER[0]


def far(frame: int) -> dict:
    """Ball far from the racket on every frame: no anchor anywhere."""
    return frame_entry(ball(10.0, 10.0), RACKET)


def test_widening_finds_a_distant_anchor() -> None:
    # the only anchor sits at HIT_FRAME; a representative 25 frames later
    # misses it at +-20 and finds it at +-40
    detections = {f: approaching(f) for f in range(1, 141)}
    assert isinstance(candidate_for(detections, 125, ladder=(20,)), Refusal)
    result = candidate_for(detections, 125)
    assert not isinstance(result, Refusal)
    assert result.window_level == 40


def test_refusal_after_two_widenings() -> None:
    detections = {f: far(f) for f in range(1, 200)}
    result = candidate_for(detections, 100)
    assert isinstance(result, Refusal)
    assert result.widenings == 2
    assert result.as_dict()["message"] == "该区间未找到击球候选"


def test_candidate_confidence_follows_distance() -> None:
    detections = {f: approaching(f, close=(f == 30)) for f in range(1, 60)}
    result = candidate_for(detections, 30)
    assert not isinstance(result, Refusal)
    assert 0.0 < result.confidence <= 1.0


def test_interval_seconds() -> None:
    assert interval_seconds(30, 60, 30.0) == (1.0, 2.0)
    assert interval_seconds(0, 0, 0.0) == (0.0, 0.0)


def test_ball_positions_use_the_most_confident_ball() -> None:
    entry = {"ball": [ball(1, 1, 0.5), ball(9, 9, 0.9)], "racket": [RACKET]}
    assert ball_positions({3: entry})[3] == (9.0, 9.0)


# ----------------------------------------------------------------------
# retention
# ----------------------------------------------------------------------


def test_retained_ranges_keep_at_most_four_actions() -> None:
    detections = {f: approaching(f, close=(f in (20, 120, 220, 320, 420))) for f in range(1, 520)}
    candidates = []
    for rep in (20, 120, 220, 320, 420):
        result = candidate_for(detections, rep)
        assert not isinstance(result, Refusal)
        candidates.append(result)
    ranges = retained_frames(candidates, per_action_frames=80, max_actions=4)
    assert len(ranges) <= 4
    assert all(hi - lo <= 80 for lo, hi in ranges)
    assert ranges == sorted(ranges)
