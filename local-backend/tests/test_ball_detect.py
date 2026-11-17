"""Ball/racket video scan tests: injected detector, tiny synthetic video."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from tennis_backend.application.worker import CancellationToken, JobCancelled
from tennis_backend.domain.models import DomainViolation
from tennis_backend.media.ball_detect import detect_video


class FakeDetector:
    def __init__(self) -> None:
        self.calls = 0

    def detect(self, frame_rgb: Any) -> dict[str, list[list[float]]]:
        self.calls += 1
        n = self.calls
        return {"ball": [[n, n, n + 4.0, n + 4.0, 0.7]], "racket": []}


def write_video(path: Path, frames: int, fps: float = 10.0) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (64, 48))
    for i in range(frames):
        writer.write(np.full((48, 64, 3), i * 10 % 255, np.uint8))
    writer.release()


def test_every_frame_is_scanned_and_kept_as_observed(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    write_video(video, 12)
    detector = FakeDetector()
    scan = detect_video(video, detector)
    assert scan.frame_count == 12
    assert (scan.width, scan.height) == (64, 48)
    assert scan.fps == pytest.approx(10.0, abs=0.5)
    assert scan.frames[0]["ball"][0][:2] == [1.0, 1.0]
    assert scan.truncated is False


def test_stride_scans_fewer_frames(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    write_video(video, 12)
    scan = detect_video(video, FakeDetector(), stride=4)
    assert sorted(scan.frames) == [0, 4, 8]


def test_long_video_is_reported_truncated(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    write_video(video, 30, fps=10.0)
    scan = detect_video(video, FakeDetector(), max_seconds=1.0)
    assert scan.truncated is True
    assert scan.frame_count == 10


def test_missing_video_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DomainViolation) as raised:
        detect_video(tmp_path / "none.mp4", FakeDetector())
    assert raised.value.code.endswith("BALL_VIDEO_MISSING")


def test_bad_stride_is_refused(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    write_video(video, 4)
    with pytest.raises(DomainViolation):
        detect_video(video, FakeDetector(), stride=0)


def test_cancellation_stops_between_frames(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    write_video(video, 12)
    token = CancellationToken()
    token.cancel()
    with pytest.raises(JobCancelled):
        detect_video(video, FakeDetector(), cancel=token)
