"""WASB-SBDT ball tracking for far / mid-distance video (HRNet heatmaps).

Near-camera video is served by COCO detection (:mod:`ball_detect`); a ball of
3-6 px in broadcast video is not, so this module runs the dedicated tennis
ball detector (WASB-SBDT HRNet, MIT) and the 4-stage post-processing the B0
experiment settled on. Every output point says how it was obtained:

``detected``   fixed 3-frame window, heatmap peak >= 0.5
``recovered``  sliding window, peak >= 0.5, speed-consistent with its neighbours
``lowthr``     peak 0.4-0.5 near a predicted position, <= 12 px/frame to anchors
``estimated``  hold fill between two close detections: NOT a detection

Honesty boundaries (measured):

- the ball is invisible to the detector while it is on the strings and while a
  leg hides it; a plausible-looking point can still be a miss (a ``lowthr``
  point in broadcast video was a false detection);
- speed continuity does not prove the track is the ball: in near-camera video
  it produced a smooth *wrong* track (the detector is blind there);
- pixel thresholds are tuned on an 886x498 crop and **must be carried in model
  input space** (letterbox scale): :func:`threshold_scale` derives the factor.

Memory: only heatmap pixels >= 0.4 can influence any stage, so each window
keeps just those (sparse), not the dense 3x288x512 map.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from tennis_backend.application.worker import CancellationToken
from tennis_backend.domain.models import DomainViolation

_WEIGHTS_MISSING = "BALL_WEIGHTS_MISSING"
_DEPS_MISSING = "REAL_POSE_DEPS_MISSING"

INPUT_W, INPUT_H = 512, 288
#: Frame size the pixel thresholds were tuned on (broadcast crop).
REFERENCE_SIZE = (886, 498)
#: Environment override for the weights location.
WEIGHTS_ENV = "TENNIS_BALL_WEIGHTS"
WEIGHTS_NAME = "wasb_tennis_best.pth.tar"

#: A heatmap pixel below this cannot affect any stage.
SPARSE_FLOOR = 0.4

_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)

#: HRNet configuration used by the published tennis weights (wasb.yaml).
_HRNET_CONFIG: dict[str, Any] = {
    "name": "hrnet",
    "frames_in": 3,
    "frames_out": 3,
    "inp_height": INPUT_H,
    "inp_width": INPUT_W,
    "out_height": INPUT_H,
    "out_width": INPUT_W,
    "rgb_diff": False,
    "out_scales": [0],
    "MODEL": {
        "EXTRA": {
            "FINAL_CONV_KERNEL": 1,
            "PRETRAINED_LAYERS": ["*"],
            "STEM": {"INPLANES": 64, "STRIDES": [1, 1]},
            "STAGE1": {
                "NUM_MODULES": 1,
                "NUM_BRANCHES": 1,
                "BLOCK": "BOTTLENECK",
                "NUM_BLOCKS": [1],
                "NUM_CHANNELS": [32],
                "FUSE_METHOD": "SUM",
            },
            "STAGE2": {
                "NUM_MODULES": 1,
                "NUM_BRANCHES": 2,
                "BLOCK": "BASIC",
                "NUM_BLOCKS": [2, 2],
                "NUM_CHANNELS": [16, 32],
                "FUSE_METHOD": "SUM",
            },
            "STAGE3": {
                "NUM_MODULES": 1,
                "NUM_BRANCHES": 3,
                "BLOCK": "BASIC",
                "NUM_BLOCKS": [2, 2, 2],
                "NUM_CHANNELS": [16, 32, 64],
                "FUSE_METHOD": "SUM",
            },
            "STAGE4": {
                "NUM_MODULES": 1,
                "NUM_BRANCHES": 4,
                "BLOCK": "BASIC",
                "NUM_BLOCKS": [2, 2, 2, 2],
                "NUM_CHANNELS": [16, 32, 64, 128],
                "FUSE_METHOD": "SUM",
            },
            "DECONV": {
                "NUM_DECONVS": 0,
                "KERNEL_SIZE": [],
                "NUM_BASIC_BLOCKS": 2,
            },
        },
        "INIT_WEIGHTS": True,
    },
}


class _AttrDict(dict[str, Any]):
    """HRNet reads its config both as ``cfg['x']`` and ``cfg.X``."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error


def _attr(value: Any) -> Any:
    if isinstance(value, dict):
        return _AttrDict({k: _attr(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_attr(v) for v in value]
    return value


def threshold_scale(width: int, height: int) -> float:
    """Factor turning reference-crop pixel thresholds into this video's pixels.

    Thresholds are tuned on 886x498 in original pixels; the same *model-input*
    distance is ``model_px / letterbox_scale`` original pixels, so the factor
    is ``reference_scale / video_scale``. For the reference size it is 1.0.
    """
    ref = min(INPUT_W / REFERENCE_SIZE[0], INPUT_H / REFERENCE_SIZE[1])
    own = min(INPUT_W / max(width, 1), INPUT_H / max(height, 1))
    return ref / own


def default_weights() -> Path:
    override = os.environ.get(WEIGHTS_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[4] / "models" / "ball" / WEIGHTS_NAME


@dataclass(frozen=True)
class WindowPeak:
    """Sparse result of one (window, slot): peak and pixels >= SPARSE_FLOOR."""

    peak: float
    ys: np.ndarray
    xs: np.ndarray
    values: np.ndarray

    def centroid(self, threshold: float = 0.5) -> tuple[float, float] | None:
        keep = self.values > threshold
        if not keep.any():
            return None
        w = self.values[keep].astype(np.float32)
        return (
            float((self.xs[keep] * w).sum() / w.sum()),
            float((self.ys[keep] * w).sum() / w.sum()),
        )


@dataclass
class TrackPoint:
    x: float
    y: float
    source: str
    peak: float | None


@dataclass
class BallTrack:
    """Per-frame ball track of one video; missing frames stay missing."""

    fps: float
    width: int
    height: int
    frames: int
    points: dict[int, TrackPoint] = field(default_factory=dict)
    spikes: list[int] = field(default_factory=list)


@dataclass
class Letterbox:
    """Original <-> model-input coordinate mapping (aspect ratio kept)."""

    scale: float
    ox: float
    oy: float

    @classmethod
    def for_size(cls, width: int, height: int) -> Letterbox:
        scale = min(INPUT_W / width, INPUT_H / height)
        return cls(scale, (INPUT_W - width * scale) / 2, (INPUT_H - height * scale) / 2)

    def to_original(self, point: tuple[float, float]) -> tuple[float, float]:
        return ((point[0] - self.ox) / self.scale, (point[1] - self.oy) / self.scale)

    def to_model(self, point: tuple[float, float]) -> tuple[float, float]:
        return (point[0] * self.scale + self.ox, point[1] * self.scale + self.oy)


# ----------------------------------------------------------------------
# inference
# ----------------------------------------------------------------------


class HeatmapModel:
    """Lazy-loaded HRNet; ``windows`` yields sparse peaks per window slot."""

    def __init__(self, weights: Path | None = None) -> None:
        self._weights = weights or default_weights()
        self._model: Any = None
        self._device = "cpu"

    def _load(self) -> Any:
        if not self._weights.is_file():
            raise DomainViolation(
                _WEIGHTS_MISSING,
                f"ball detector weights {self._weights.name} not found; "
                f"set {WEIGHTS_ENV} or place the file in models/ball/",
            )
        try:
            import torch

            from tennis_backend.vendor.wasb.hrnet import HRNet
        except ImportError as error:
            raise DomainViolation(
                _DEPS_MISSING,
                "real-pose dependencies are not installed; run uv sync --extra real-pose",
            ) from error
        self._device = "mps" if torch.backends.mps.is_available() else "cpu"
        model = HRNet(_attr(_HRNET_CONFIG))  # type: ignore[no-untyped-call]
        checkpoint = torch.load(self._weights, map_location="cpu", weights_only=True)
        model.load_state_dict(checkpoint["model_state_dict"])
        return model.to(self._device).eval()

    def windows(
        self,
        frames_rgb: Iterator[np.ndarray],
        size: tuple[int, int],
        *,
        cancel: CancellationToken | None,
    ) -> Iterator[tuple[int, list[WindowPeak]]]:
        """Yield ``(window_index, [slot0, slot1, slot2])`` for every 3-frame window."""
        import torch

        if self._model is None:
            self._model = self._load()
        box = Letterbox.for_size(*size)
        buffer: list[np.ndarray] = []
        index = 0
        for frame in frames_rgb:
            if cancel is not None:
                cancel.check_point()
            buffer.append(_prepare(frame, size, box))
            if len(buffer) < 3:
                continue
            batch = torch.from_numpy(np.concatenate(buffer[-3:]))[None].to(self._device)
            with torch.no_grad():
                out = self._model(batch)
            heat = (out[0] if isinstance(out, dict) else out).sigmoid().cpu().numpy()[0]
            yield index, [_sparse(heat[k]) for k in range(3)]
            index += 1
            buffer = buffer[-2:]


def _prepare(frame_rgb: np.ndarray, size: tuple[int, int], box: Letterbox) -> np.ndarray:
    width, height = size
    canvas = np.zeros((INPUT_H, INPUT_W, 3), np.uint8)
    new_w, new_h = round(width * box.scale), round(height * box.scale)
    oy, ox = round(box.oy), round(box.ox)
    canvas[oy : oy + new_h, ox : ox + new_w] = cv2.resize(frame_rgb, (new_w, new_h))
    normalised = (canvas.astype(np.float32) / 255 - _MEAN) / _STD
    out: np.ndarray = normalised.transpose(2, 0, 1)
    return out


def _sparse(heat: np.ndarray) -> WindowPeak:
    ys, xs = np.where(heat >= SPARSE_FLOOR)
    return WindowPeak(
        peak=float(heat.max()),
        ys=ys.astype(np.int32),
        xs=xs.astype(np.int32),
        values=heat[ys, xs].astype(np.float16),
    )


# ----------------------------------------------------------------------
# post-processing (pure; sparse heatmaps in, a track out)
# ----------------------------------------------------------------------


def _neighbour(frame: int, points: dict[int, Any], direction: int, max_gap: int = 4) -> int | None:
    for gap in range(1, max_gap + 1):
        if (frame + direction * gap) in points:
            return frame + direction * gap
    return None


def _speed(a: tuple[float, float], b: tuple[float, float], frames: int) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1]) / max(frames, 1)


def _recover(
    base: dict[int, tuple[float, float]], candidates: dict[int, tuple[float, float]], scale: float
) -> dict[int, tuple[float, float]]:
    current = dict(base)
    accepted: dict[int, tuple[float, float]] = {}
    for _ in range(3):
        for frame in sorted(set(candidates) - set(base)):
            if frame in accepted:
                continue
            speeds = []
            for direction in (-1, 1):
                other = _neighbour(frame, current, direction)
                if other is not None:
                    speeds.append(_speed(candidates[frame], current[other], abs(frame - other)))
            if not speeds:
                continue
            limit = (18 if len(speeds) == 2 else 15) * scale
            if all(s <= limit for s in speeds):
                accepted[frame] = candidates[frame]
                current[frame] = candidates[frame]
    return accepted


def postprocess(
    windows: list[list[WindowPeak]],
    frame_count: int,
    box: Letterbox,
    *,
    scale: float,
    fps: float,
    size: tuple[int, int],
) -> BallTrack:
    """The v4 pipeline: detect, recover, de-spike, low-threshold, hold fill."""
    track = BallTrack(fps=fps, width=size[0], height=size[1], frames=frame_count)

    original: dict[int, tuple[float, float]] = {}
    peaks: dict[int, float] = {}
    for i in range(0, len(windows), 3):
        for k in range(3):
            c = windows[i][k].centroid()
            if c is not None:
                original[i + k] = box.to_original(c)
                peaks[i + k] = windows[i][k].peak

    candidates: dict[int, tuple[float, float]] = {}
    cand_peak: dict[int, float] = {}
    for frame in range(frame_count):
        best: WindowPeak | None = None
        for k in range(3):
            i = frame - k
            if 0 <= i < len(windows) and (best is None or windows[i][k].peak > best.peak):
                best = windows[i][k]
        if best is not None:
            c = best.centroid()
            if c is not None:
                candidates[frame] = box.to_original(c)
                cand_peak[frame] = best.peak

    recovered_1 = _recover(original, candidates, scale)
    combined = {**original, **recovered_1}
    spikes: list[int] = []
    for frame in sorted(combined):
        p, q = _neighbour(frame, combined, -1), _neighbour(frame, combined, 1)
        if p is None or q is None:
            continue
        dp = _speed(combined[frame], combined[p], frame - p)
        dq = _speed(combined[frame], combined[q], q - frame)
        dpq = _speed(combined[q], combined[p], q - p)
        if dp > 30 * scale and dq > 30 * scale and dpq <= 15 * scale:
            spikes.append(frame)
    track.spikes = spikes
    kept = {f: v for f, v in original.items() if f not in spikes}
    recovered = _recover(kept, candidates, scale)
    positions = {**kept, **recovered}
    source = {f: "detected" for f in kept}
    source.update({f: "recovered" for f in recovered})
    peak_of = {f: peaks.get(f) for f in kept}
    peak_of.update({f: cand_peak.get(f) for f in recovered})

    low = _low_threshold(windows, frame_count, positions, box, scale)
    for frame, (xy, pk) in low.items():
        positions[frame] = xy
        source[frame] = "lowthr"
        peak_of[frame] = pk

    frames = sorted(positions)
    for a, b in zip(frames, frames[1:], strict=False):
        gap = b - a - 1
        if (
            1 <= gap <= 15
            and math.hypot(positions[a][0] - positions[b][0], positions[a][1] - positions[b][1])
            <= 8 * scale
        ):
            for frame in range(a + 1, b):
                t = (frame - a) / (b - a)
                positions[frame] = (
                    positions[a][0] + t * (positions[b][0] - positions[a][0]),
                    positions[a][1] + t * (positions[b][1] - positions[a][1]),
                )
                source[frame] = "estimated"
                peak_of[frame] = None

    track.points = {
        f: TrackPoint(x=positions[f][0], y=positions[f][1], source=source[f], peak=peak_of.get(f))
        for f in sorted(positions)
    }
    return track


def _low_threshold(
    windows: list[list[WindowPeak]],
    frame_count: int,
    positions: dict[int, tuple[float, float]],
    box: Letterbox,
    scale: float,
) -> dict[int, tuple[tuple[float, float], float]]:
    predicted: dict[int, tuple[tuple[float, float], float]] = {}
    for frame in range(frame_count):
        if frame in positions:
            continue
        a = next((frame - g for g in range(1, 5) if (frame - g) in positions), None)
        b = next((frame + g for g in range(1, 5) if (frame + g) in positions), None)
        if (
            a is not None
            and b is not None
            and math.hypot(positions[a][0] - positions[b][0], positions[a][1] - positions[b][1])
            <= 15 * scale * (b - a)
        ):
            t = (frame - a) / (b - a)
            at = (
                positions[a][0] + t * (positions[b][0] - positions[a][0]),
                positions[a][1] + t * (positions[b][1] - positions[a][1]),
            )
            predicted[frame] = (at, (10 + 4 * min(frame - a, b - frame)) * scale)
        elif a is not None:
            predicted[frame] = (positions[a], (10 + 4 * (frame - a)) * scale)
        elif b is not None:
            predicted[frame] = (positions[b], (10 + 4 * (b - frame)) * scale)

    found: dict[int, tuple[tuple[float, float], float]] = {}
    for frame, ((px, py), radius_px) in predicted.items():
        cx, cy = box.to_model((px, py))
        r = radius_px * box.scale
        x0, x1 = int(max(cx - r, 0)), int(min(cx + r + 1, INPUT_W))
        y0, y1 = int(max(cy - r, 0)), int(min(cy + r + 1, INPUT_H))
        if x1 <= x0 or y1 <= y0:
            continue
        best: tuple[float, np.ndarray, np.ndarray, np.ndarray] | None = None
        for k in range(3):
            i = frame - k
            if not 0 <= i < len(windows):
                continue
            w = windows[i][k]
            inside = (w.xs >= x0) & (w.xs < x1) & (w.ys >= y0) & (w.ys < y1)
            if not inside.any():
                continue
            values = w.values[inside].astype(np.float32)
            peak = float(values.max())
            if peak >= 0.4 and (best is None or peak > best[0]):
                best = (peak, w.xs[inside], w.ys[inside], values)
        if best is None:
            continue
        peak, xs, ys, values = best
        keep = values >= max(0.4, 0.5 * peak)
        weights = values[keep]
        mx = float((xs[keep] * weights).sum() / weights.sum())
        my = float((ys[keep] * weights).sum() / weights.sum())
        point = box.to_original((mx, my))
        speeds = []
        for direction in (-1, 1):
            other = _neighbour(frame, positions, direction)
            if other is not None:
                speeds.append(_speed(point, positions[other], abs(frame - other)))
        if speeds and all(s <= 12 * scale for s in speeds):
            found[frame] = (point, peak)
    return found


def track_video(
    video: Path,
    model: HeatmapModel,
    *,
    cancel: CancellationToken | None = None,
    max_seconds: float = 180.0,
) -> BallTrack:
    """Read ``video`` and return its ball track (whole clip held in sparse form)."""
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise DomainViolation("BALL_VIDEO_MISSING", f"video {video.name} cannot be opened")
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS)) or 30.0
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        limit = int(max_seconds * fps)
        count = 0

        def frames() -> Iterator[np.ndarray]:
            nonlocal count
            while count < limit:
                ok, frame = capture.read()
                if not ok:
                    return
                count += 1
                yield cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        windows = [slots for _, slots in model.windows(frames(), (width, height), cancel=cancel)]
    finally:
        capture.release()
    return postprocess(
        windows,
        count,
        Letterbox.for_size(width, height),
        scale=threshold_scale(width, height),
        fps=fps,
        size=(width, height),
    )
