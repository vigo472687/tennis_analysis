"""WASB ball tracking: pure post-processing on synthetic sparse heatmaps, plus
one real-weights smoke test that skips when the weights are not installed."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tennis_backend.domain.models import DomainViolation
from tennis_backend.media.wasb_track import (
    HeatmapModel,
    Letterbox,
    WindowPeak,
    default_weights,
    postprocess,
    threshold_scale,
    track_video,
)

SIZE = (886, 498)
BOX = Letterbox.for_size(*SIZE)


def blob(x: float, y: float, peak: float = 0.9) -> WindowPeak:
    """A 3x3 sparse blob around a model-space position."""
    cx, cy = int(round(x)), int(round(y))
    ys, xs = np.meshgrid(np.arange(cy - 1, cy + 2), np.arange(cx - 1, cx + 2), indexing="ij")
    values = np.full(xs.size, peak, np.float16)
    return WindowPeak(peak=peak, ys=ys.ravel(), xs=xs.ravel(), values=values)


def empty() -> WindowPeak:
    z = np.zeros(0, np.int32)
    return WindowPeak(peak=0.1, ys=z, xs=z, values=np.zeros(0, np.float16))


def windows_for(path: dict[int, tuple[float, float]], frames: int) -> list[list[WindowPeak]]:
    """Window i, slot k describes frame i+k; ball at ``path`` positions (model px)."""
    out = []
    for i in range(frames - 2):
        out.append([blob(*path[i + k]) if (i + k) in path else empty() for k in range(3)])
    return out


def test_threshold_scale_is_one_on_the_reference_size() -> None:
    assert threshold_scale(*SIZE) == pytest.approx(1.0)


def test_threshold_scale_follows_the_letterbox() -> None:
    # 544x960 portrait: measured 1.927 in the experiment
    assert threshold_scale(544, 960) == pytest.approx(1.927, abs=0.01)


def test_letterbox_round_trip() -> None:
    box = Letterbox.for_size(544, 960)
    point = (100.0, 200.0)
    back = box.to_original(box.to_model(point))
    assert back == pytest.approx(point)


def test_a_steady_ball_is_detected_everywhere() -> None:
    path = {f: (100.0 + 3.0 * f, 150.0) for f in range(20)}
    track = postprocess(windows_for(path, 20), 20, BOX, scale=1.0, fps=30.0, size=SIZE)
    assert len(track.points) >= 18
    assert {p.source for p in track.points.values()} <= {"detected", "recovered", "lowthr"}
    assert all(p.peak is not None and p.peak >= 0.4 for p in track.points.values())


def test_a_short_gap_is_recovered_not_invented() -> None:
    path = {f: (100.0 + 3.0 * f, 150.0) for f in range(30) if f not in (12, 13)}
    track = postprocess(windows_for(path, 30), 30, BOX, scale=1.0, fps=30.0, size=SIZE)
    assert 10 in track.points and 15 in track.points
    # a frame with no heatmap evidence keeps its source honest
    for frame in (12, 13):
        if frame in track.points:
            assert track.points[frame].source in {"recovered", "lowthr", "estimated"}


def test_a_stationary_gap_is_filled_as_estimated_only() -> None:
    path = {f: (200.0, 100.0) for f in range(30) if f not in (14, 15, 16)}
    track = postprocess(windows_for(path, 30), 30, BOX, scale=1.0, fps=30.0, size=SIZE)
    filled = [f for f in (14, 15, 16) if f in track.points]
    assert filled
    assert all(track.points[f].source == "estimated" for f in filled)
    assert all(track.points[f].peak is None for f in filled)


def test_a_far_jump_is_a_spike_and_is_dropped() -> None:
    path = {f: (100.0 + 2.0 * f, 150.0) for f in range(30)}
    path[15] = (400.0, 250.0)
    track = postprocess(windows_for(path, 30), 30, BOX, scale=1.0, fps=30.0, size=SIZE)
    assert 15 in track.spikes
    assert 15 not in track.points or track.points[15].source != "detected"


def test_no_ball_means_no_track() -> None:
    windows = [[empty(), empty(), empty()] for _ in range(20)]
    track = postprocess(windows, 22, BOX, scale=1.0, fps=30.0, size=SIZE)
    assert track.points == {}


def test_missing_weights_are_a_clear_refusal(tmp_path: Path) -> None:
    with pytest.raises(DomainViolation) as raised:
        list(HeatmapModel(tmp_path / "nope.pth.tar").windows(iter([]), SIZE, cancel=None))
    assert raised.value.code.endswith("BALL_WEIGHTS_MISSING")


@pytest.mark.skipif(not default_weights().is_file(), reason="ball weights not installed")
def test_real_weights_load_and_run(tmp_path: Path) -> None:
    import cv2

    video = tmp_path / "v.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (160, 90))
    for i in range(8):
        writer.write(np.full((90, 160, 3), 40 + i, np.uint8))
    writer.release()
    track = track_video(video, HeatmapModel())
    assert track.frames == 8
    assert track.fps == pytest.approx(30.0, abs=1.0)
