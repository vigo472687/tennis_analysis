"""Detector choice: expert slot, three prompts, measured fallback."""

from __future__ import annotations

import pytest

from tennis_backend.analysis.detector_choice import (
    MAX_PROMPTS,
    NEAR_HEIGHT_RATIO,
    DetectorChoice,
    NeedDetector,
    fallback_detector,
    resolve_detector,
    subject_height_ratio,
)

# Measured medians from the four B0 videos
MEASURED = {"flow": 0.727, "v400": 0.416, "broadcast": 0.189, "v395": 0.157}


def test_expert_choice_is_used_as_is() -> None:
    for name in ("coco", "wasb"):
        result = resolve_detector(name, prompts_used=0, height_ratio=0.9)
        assert isinstance(result, DetectorChoice)
        assert (result.detector, result.source, result.fallback) == (name, "expert", False)


def test_no_choice_asks_again_up_to_three_times() -> None:
    seen = []
    for used in range(MAX_PROMPTS):
        result = resolve_detector(None, prompts_used=used, height_ratio=0.5)
        assert isinstance(result, NeedDetector)
        seen.append((result.prompts_used, result.prompts_left))
    assert seen == [(1, 2), (2, 1), (3, 0)]


def test_a_value_outside_the_slot_is_asked_again() -> None:
    result = resolve_detector("yolo", prompts_used=0, height_ratio=0.5)
    assert isinstance(result, NeedDetector)


def test_after_three_prompts_the_backend_picks_and_says_so() -> None:
    result = resolve_detector(None, prompts_used=MAX_PROMPTS, height_ratio=0.5)
    assert isinstance(result, DetectorChoice)
    assert result.fallback is True
    assert result.source == "backend_fallback"
    assert "复核" in result.note


@pytest.mark.parametrize(
    ("video", "expected"),
    [("flow", "coco"), ("v400", "coco"), ("broadcast", "wasb"), ("v395", "wasb")],
)
def test_fallback_separates_the_four_measured_videos(video: str, expected: str) -> None:
    assert fallback_detector(MEASURED[video]) == expected


def test_threshold_lies_between_the_two_groups() -> None:
    assert (
        max(MEASURED["broadcast"], MEASURED["v395"])
        < NEAR_HEIGHT_RATIO
        < min(MEASURED["flow"], MEASURED["v400"])
    )


def test_no_subject_picks_the_cautious_detector() -> None:
    assert fallback_detector(None) == "wasb"


def test_height_ratio_uses_the_median() -> None:
    assert subject_height_ratio([100, 200, 900], 1000) == pytest.approx(0.2)
    assert subject_height_ratio([], 1000) is None
    assert subject_height_ratio([100], 0) is None
