from __future__ import annotations

import math

import pytest

from tennis_backend.experience.measure import joint_angle, summarize_frames


def landmarks(visible: float = 1.0) -> list[list[float]]:
    points = [[0.5, 0.5, 0.0, visible] for _ in range(33)]
    points[11] = [0.4, 0.3, 0.0, visible]
    points[12] = [0.6, 0.3, 0.0, visible]
    points[13] = [0.3, 0.4, 0.0, visible]
    points[14] = [0.7, 0.4, 0.0, visible]
    points[15] = [0.2, 0.5, 0.0, visible]
    points[16] = [0.8, 0.5, 0.0, visible]
    points[23] = [0.4, 0.5, 0.0, visible]
    points[24] = [0.6, 0.5, 0.0, visible]
    points[25] = [0.4, 0.7, 0.0, visible]
    points[26] = [0.6, 0.7, 0.0, visible]
    points[27] = [0.4, 0.9, 0.0, visible]
    points[28] = [0.6, 0.9, 0.0, visible]
    return points


def test_projection_angles_use_image_aspect_and_refuse_low_visibility() -> None:
    assert joint_angle([0, 0, 0, 1], [0, 1, 0, 1], [1, 1, 0, 1], 2) == 90
    assert joint_angle([0, 0, 0, 0.2], [0, 1, 0, 1], [1, 1, 0, 1], 1) is None
    assert joint_angle([0, 0, 0, 1], [0, 0, 0, 1], [1, 1, 0, 1], 1) is None


@pytest.mark.parametrize("people", [[], [landmarks(), landmarks()]])
def test_empty_and_multiple_subjects_never_get_individual_metrics(
    people: list[list[list[float]]],
) -> None:
    result = summarize_frames(
        [{"t": i / 12, "people": people} for i in range(20)], 640, 480, "right"
    )
    assert result["individual_review_available"] is False
    assert result["angle_ranges"] == {}
    assert result["segments"] == []
    assert result["motion"] == []


def test_actual_landmark_motion_gets_review_anchors_but_no_contact_claims() -> None:
    frames = []
    for i in range(60):
        points = landmarks()
        points[16][0] = 0.65 + 0.18 * math.sin(i / 3)
        frames.append({"t": i / 12, "people": [points]})
    result = summarize_frames(frames, 640, 480, "right")
    assert result["individual_review_available"] is True
    assert result["detection_ratio"] == 1
    assert result["feet_visible_ratio"] == 1
    assert result["segments"]
    assert result["angle_ranges"]["right_elbow"]["samples"] == 60
    assert not any("contact" in key or "force" in key for key in result)


def test_keypoints_with_hidden_feet_do_not_claim_visible_footwork() -> None:
    points = landmarks()
    points[27][3] = 0.1
    points[28][3] = 0.1
    result = summarize_frames(
        [{"t": i / 12, "people": [points]} for i in range(20)], 640, 480, "right"
    )
    assert result["feet_visible_ratio"] == 0
    assert "left_knee" not in result["angle_ranges"]
    assert "right_knee" not in result["angle_ranges"]


def test_small_object_false_positives_do_not_pass_body_quality_gate() -> None:
    points = landmarks()
    for point in points:
        point[0] = 0.5 + (point[0] - 0.5) * 0.1
        point[1] = 0.7 + (point[1] - 0.5) * 0.1
    result = summarize_frames(
        [{"t": i / 12, "people": [points]} for i in range(20)], 640, 480, "right"
    )
    assert result["detection_ratio"] == 0
    assert result["individual_review_available"] is False
    assert result["angle_ranges"] == {}
