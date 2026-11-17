"""Frame images: written for retained ranges only, deleted with retention.

The cases pin the storage promise the user approves before a run: only the
retained ranges reach disk, a missing video writes nothing instead of blank
images, and deletion is idempotent (a second sweep is not an error).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest

from tennis_backend.evidence.frame_images import (
    delete_expired_images,
    delete_version_images,
    frames_dir,
    retained_only,
    write_frames,
)
from tennis_backend.storage.evidence_store import EvidenceFrame

WIDTH, HEIGHT, FPS, COUNT = 160, 120, 10.0, 20


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (WIDTH, HEIGHT))
    for index in range(COUNT):
        frame = np.full((HEIGHT, WIDTH, 3), 40 + index * 5, np.uint8)
        writer.write(frame)
    writer.release()
    assert path.stat().st_size > 0
    return path


def frame_of(number: int, *, ball: bool = True) -> EvidenceFrame:
    return EvidenceFrame(
        frame=number,
        time_s=number / FPS,
        ball=[60.0, 40.0, 76.0, 56.0] if ball else None,
        ball_confidence=0.9 if ball else None,
        racket=[100.0, 30.0, 130.0, 90.0],
        racket_confidence=0.8,
        frames_to_anchor=0,
    )


def test_only_retained_frames_are_kept() -> None:
    frames = [frame_of(n) for n in range(COUNT)]
    kept = retained_only(frames, [(4, 8)])
    assert [item.frame for item in kept] == [4, 5, 6, 7, 8]


def test_frames_outside_every_range_are_dropped() -> None:
    frames = [frame_of(n) for n in range(COUNT)]
    kept = retained_only(frames, [(0, 3), (10, 12)])
    assert [item.frame for item in kept] == [0, 1, 2, 3, 10, 11, 12]


def test_writing_creates_one_image_and_one_zoom_per_frame(video: Path, tmp_path: Path) -> None:
    root = tmp_path / "public"
    images = write_frames(video, root, "projects/t/v1/evidence", [frame_of(3), frame_of(9)])
    assert [image.frame for image in images] == [3, 9]
    directory = root / frames_dir("projects/t/v1/evidence")
    assert (directory / "f000003.jpg").stat().st_size > 0
    assert (directory / "f000009.jpg").stat().st_size > 0
    assert images[0].zoom is not None
    assert (directory / "f000003-zoom.jpg").stat().st_size > 0


def test_a_frame_without_a_ball_has_no_zoom(video: Path, tmp_path: Path) -> None:
    root = tmp_path / "public"
    images = write_frames(video, root, "projects/t/v1/evidence", [frame_of(5, ball=False)])
    assert images[0].zoom is None
    assert not (root / frames_dir("projects/t/v1/evidence") / "f000005-zoom.jpg").exists()


def test_an_unreadable_video_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "public"
    missing = tmp_path / "nope.mp4"
    assert write_frames(missing, root, "projects/t/v1/evidence", [frame_of(1)]) == ()
    assert not (root / frames_dir("projects/t/v1/evidence") / "f000001.jpg").exists()


def test_deleting_a_version_removes_its_images(video: Path, tmp_path: Path) -> None:
    root = tmp_path / "public"
    write_frames(video, root, "projects/t/v1/evidence", [frame_of(1), frame_of(2)])
    removed = delete_version_images(root, "projects/t/v1/evidence")
    assert removed == 4  # two whole frames and two close-ups
    assert not (root / frames_dir("projects/t/v1/evidence")).exists()


def test_deleting_a_version_twice_is_not_an_error(tmp_path: Path) -> None:
    assert delete_version_images(tmp_path, "projects/none/v1/evidence") == 0


def test_expired_images_are_removed_and_fresh_ones_kept(video: Path, tmp_path: Path) -> None:
    import os

    root = tmp_path / "public"
    write_frames(video, root, "projects/old/v1/evidence", [frame_of(1)])
    write_frames(video, root, "projects/new/v1/evidence", [frame_of(2)])
    old = root / frames_dir("projects/old/v1/evidence")
    stale = (datetime.now() - timedelta(days=40)).timestamp()
    for path in old.iterdir():
        os.utime(path, (stale, stale))
    os.utime(old, (stale, stale))
    now = datetime.now()
    removed = delete_expired_images(root, "projects", now=now, retention=timedelta(days=30))
    assert removed == 2
    assert not old.exists()
    assert (root / frames_dir("projects/new/v1/evidence") / "f000002.jpg").exists()
