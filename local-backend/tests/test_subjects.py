"""Subject detection/tracking service tests (Phase 2).

Uses an injected fake detector so tests drive known boxes deterministically;
the real RF-DETR integration is covered by an optional test against the
approved sample when the real-pose extra is installed. The tracker is real
(boxmot ByteTrack) — the fake detector only replaces model inference.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pytest

from tennis_backend.domain.models import DomainViolation
from tennis_backend.media.subjects import RFDetrPersonDetector, extract_subjects


class FakeDetector:
    """Emits boxes per frame from a scripted list; None = no detection."""

    def __init__(self, script: list[list[dict[str, Any]] | None]) -> None:
        self._script = script
        self._i = 0

    def detect(self, frame_rgb: Any) -> list[dict[str, Any]]:
        boxes = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return boxes or []


def person_box(x: float) -> dict[str, Any]:
    return {"class": "person", "confidence": 0.9, "box": [int(x), 100, int(x) + 50, 300]}


def chair_box() -> dict[str, Any]:
    return {"class": "chair", "confidence": 0.6, "box": [10, 200, 60, 260]}


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    d = tmp_path / "out"
    d.mkdir()
    return d


def make_video(tmp_path: Path, seconds: float = 2.0, fps: float = 10.0) -> Path:

    video = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), fps, (320, 240))
    for _ in range(int(seconds * fps)):
        writer.write(np.full((240, 320, 3), 128, dtype=np.uint8))
    writer.release()
    return video


def test_single_stable_track_is_detected_and_tracked(tmp_path: Path) -> None:
    video = make_video(tmp_path)
    script = [[person_box(50 + i * 2), chair_box()] for i in range(20)]
    outcome = extract_subjects(video=video, out_dir=tmp_path / "out", detector=FakeDetector(script))
    assert outcome.subject_stability == "single"
    assert outcome.requires_selection is False
    assert outcome.retention == pytest.approx(1.0)
    # chair never counted as subject
    assert all(d["class"] == "person" for r in outcome.records for d in r.boxes)
    assert outcome.annotated_video is not None and outcome.annotated_video.exists()
    assert outcome.per_frame_jsonl is not None and outcome.per_frame_jsonl.exists()


def test_gap_frames_are_recorded_lost_not_filled(tmp_path: Path) -> None:
    video = make_video(tmp_path)
    script: list[list[dict[str, Any]] | None] = []
    for i in range(20):
        script.append([person_box(50 + i)] if 5 <= i < 15 else None)  # frames 15-19 lost
    outcome = extract_subjects(video=video, out_dir=tmp_path / "out", detector=FakeDetector(script))
    lost = [r for r in outcome.records if r.lost]
    # ByteTrack confirms a track only after its first detections: the
    # pre-detection warm-up frames (0-4) are honestly lost too
    assert [r.frame for r in lost] == list(range(0, 5)) + list(range(15, 20))
    assert all(r.track_id is None for r in lost)  # no invented ids on lost frames
    assert outcome.subject_stability == "single"  # 15 tracked frames dominate


def test_two_persistent_subjects_require_selection(tmp_path: Path) -> None:
    video = make_video(tmp_path)

    class TwoPersonDetector:
        def __init__(self) -> None:
            self._i = 0

        def detect(self, frame_rgb: Any) -> list[dict[str, Any]]:
            self._i += 1
            return [person_box(30), person_box(200)]  # two stable boxes

    outcome = extract_subjects(video=video, out_dir=tmp_path / "out", detector=TwoPersonDetector())
    assert outcome.subject_stability == "multi"
    assert outcome.requires_selection is True
    assert any("select one person" in n for n in outcome.notes)


def test_no_person_at_all_reports_none(tmp_path: Path) -> None:
    video = make_video(tmp_path)
    outcome = extract_subjects(
        video=video, out_dir=tmp_path / "out", detector=FakeDetector([[]] * 20)
    )
    assert outcome.subject_stability == "none"
    assert outcome.annotated_video is not None  # video still produced, honestly empty
    assert all(r.lost for r in outcome.records)


def _rec(frame: int, box: list[int]) -> Any:
    return SimpleNamespace(
        frame=frame,
        boxes=[{"class": "person", "confidence": 0.9, "box": list(box)}],
        track_id=1,
        lost=False,
    )


def test_action_segments_split_by_movement_and_idle() -> None:
    from tennis_backend.media.subjects import derive_action_segments

    fps = 10.0
    records = []
    for i in range(50):
        if i < 10:  # moving: box sweeps right by 8px/frame
            records.append(_rec(i, [40 + i * 8, 100, 90 + i * 8, 300]))
        elif i < 30:  # idle 2s: same box every frame
            records.append(_rec(i, [160, 100, 210, 300]))
        else:  # moving again
            records.append(_rec(i, [160 + (i - 30) * 8, 100, 210 + (i - 30) * 8, 300]))
    segs = derive_action_segments(records, fps=fps)
    assert len(segs) >= 2  # two action runs split by the idle block
    assert segs[0].start_frame == 0 and segs[0].end_frame >= 8
    # idle frames are excluded from segment coverage
    for seg in segs:
        assert seg.frames <= seg.end_frame - seg.start_frame + 1


def test_action_segments_cap_keeps_first_ten_chronologically() -> None:
    from tennis_backend.media.subjects import (
        ActionSegment,
        select_action_segments,
    )

    segs = [ActionSegment(i * 100, i * 100 + 50, i * 4.0, i * 4.0 + 2.0, 50) for i in range(12)]
    selected = select_action_segments(segs, max_actions=10)
    assert len(selected) == 10
    assert [s.start_frame for s in selected] == [i * 100 for i in range(10)]


def test_action_segments_short_blips_filtered() -> None:
    from tennis_backend.media.subjects import derive_action_segments

    fps = 10.0
    records = []
    # three 2-frame movement blips separated by 40-frame idle stretches:
    # gaps far exceed the 1s merge window, each blip is under the 1s
    # minimum duration -> all filtered
    frame = 0
    for _blip in range(3):
        for k in range(2):
            records.append(_rec(frame, [40 + k * 30, 100, 90 + k * 30, 300]))
            frame += 1
        frame += 40
    for r in records:
        assert r.frame < 130
    segs = derive_action_segments(records, fps=fps)
    assert segs == []


def test_missing_video_refused(tmp_path: Path) -> None:
    with pytest.raises(DomainViolation) as error:
        extract_subjects(
            video=tmp_path / "nope.mp4",
            out_dir=tmp_path / "out",
            detector=FakeDetector([]),
        )
    assert error.value.code == "DOMAIN_SUBJECT_VIDEO_MISSING"


def test_rfdetr_detector_refused_without_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "rfdetr":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    detector = RFDetrPersonDetector()
    with pytest.raises(DomainViolation) as error:
        detector.detect(np.zeros((10, 10, 3), dtype=np.uint8))
    assert error.value.code == "DOMAIN_REAL_POSE_DEPS_MISSING"
