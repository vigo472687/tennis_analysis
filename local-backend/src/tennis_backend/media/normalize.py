"""FFmpeg standardization with measured original-time mapping (BE-014).

Turns one supported original video into a standardized derivative (upright
pixels, constant frame rate, no rotation metadata) and measures the mapping
from normalized time back to the original video's frame timestamps
(FR-022, AC-SPEC-012).

Honesty and failure rules:

- Every value in the outcome is measured: rotation/fps/slow-motion come from
  a real probe of the source (BE-013), the mapping points come from real
  ``ffprobe`` frame timestamps of the original, and the derivative is
  re-probed to verify the standardization actually happened.
- A timeout, an ffmpeg failure, a verification mismatch or an unusable
  mapping produces a **blocked** outcome: ``time_mapping_valid=False`` and
  ``media=None`` — no formal evidence can be published from it (EX-043).
  The source file is never modified; the derivative is written atomically
  (staging + ``os.replace``).
- Inputs that are not supported media (per the BE-013 probe) are refused
  with ``MEDIA_NOT_SUPPORTED`` before any work starts.

Threading note (AG-B02): like the probe, this runs its subprocesses on the
calling thread and may be invoked inside a BE-012 worker job.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tennis_backend.domain.models import DomainViolation, MediaAsset, MediaProbe, TimePoint
from tennis_backend.media.probe import probe_video

DEFAULT_TARGET_FPS = 25.0
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_MAPPING_POINTS = 100
DEFAULT_MAX_PROBE_FRAMES = 2000

_NOT_SUPPORTED = "MEDIA_NOT_SUPPORTED"

#: Runner protocol: fixed argv, no shell; returns the exit code.
ProcessRunner = Callable[[list[str], float], int]

#: Frame-times reader protocol: measured original pts timestamps; () unusable.
FrameTimesReader = Callable[[Path, float, int], tuple[float, ...]]


def _system_runner(argv: list[str], timeout: float) -> int:
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise _RunnerTimeout(f"process timed out after {timeout}s") from error
    except OSError as error:
        raise _RunnerTimeout(f"process is not available: {error.__class__.__name__}") from error
    return completed.returncode


class _RunnerTimeout(Exception):
    """Internal signal that a subprocess failed or timed out."""


# ---------------------------------------------------------------------------
# Outcome shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceReport:
    """Measured facts about the original video before standardization."""

    duration_s: float
    fps: float
    rotation: str
    slow_motion: str
    variable_frame_rate: bool


@dataclass(frozen=True)
class NormalizeOutcome:
    """Either a standardized derivative with a valid mapping, or a block."""

    status: str  # "normalized" | "blocked"
    derivative_path: Path | None
    media: MediaAsset | None
    mapping: tuple[TimePoint, ...]
    source: SourceReport
    #: Stable sanitized notes; no paths, no raw process output.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_blocked(self) -> bool:
        return self.status != "normalized"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def normalize_video(
    path: str | Path,
    *,
    output_dir: str | Path,
    original_media_id: str,
    original_ref: str,
    target_fps: float = DEFAULT_TARGET_FPS,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_mapping_points: int = DEFAULT_MAX_MAPPING_POINTS,
    max_probe_frames: int = DEFAULT_MAX_PROBE_FRAMES,
    runner: ProcessRunner | None = None,
    frame_reader: FrameTimesReader | None = None,
    simulation: bool = True,
) -> NormalizeOutcome:
    """Standardize one supported original video and measure its time mapping.

    ``simulation`` reflects the prototype's data boundary: the standardization
    is real FFmpeg work, but the analysis fed from it stays simulated until a
    validated real model exists, so the derivative asset is registered with
    this flag exactly as received.
    """
    location = Path(path)
    if not location.is_file():
        raise DomainViolation("MEDIA_FILE_MISSING", f"media file does not exist: {location.name}")
    process = runner if runner is not None else _system_runner

    # 1. measure the source with a real probe; unsupported media never starts
    source_probe = probe_video(location)
    if source_probe.summary["support"] != "supported":
        raise DomainViolation(
            _NOT_SUPPORTED,
            "media is not supported for standardization"
            f" (support={source_probe.summary['support']})",
        )
    # 2. measure original frame timestamps (the mapping backbone)
    reader = frame_reader if frame_reader is not None else _SystemFrameTimesReader()
    frame_times = _measure_frame_times(location, reader, timeout, max_probe_frames)

    # 3. run real FFmpeg standardization into a staging file, then place it
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    staging: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="normalize-", suffix=".mp4", dir=out_dir, delete=False
        ) as handle:
            staging = Path(handle.name)
        _run_ffmpeg(location, staging, target_fps, process, timeout)
    except _RunnerTimeout:
        _discard(staging)
        return NormalizeOutcome(
            status="blocked",
            derivative_path=None,
            media=None,
            mapping=(),
            source=_source_report(source_probe, frame_times),
            notes=("ffmpeg_failed_or_timeout",),
        )

    # 4. verify the derivative with a real probe; mismatch blocks publication
    derivative_probe = probe_video(staging)
    failures = _verify(source_probe, derivative_probe, target_fps)
    mapping = _mapping_points(frame_times, target_fps, max_mapping_points)
    if failures or mapping is None or derivative_probe.summary["support"] != "supported":
        notes = tuple(failures) or ("mapping_failed",)
        _discard(staging)
        return NormalizeOutcome(
            status="blocked",
            derivative_path=None,
            media=None,
            mapping=(),
            source=_source_report(source_probe, frame_times),
            notes=notes,
        )

    # 5. place the derivative atomically and register the measured asset
    derivative_id = f"{original_media_id}-std-{uuid.uuid4().hex[:8]}"
    final_path = out_dir / f"{derivative_id}.mp4"
    os.replace(staging, final_path)
    staging = None
    media = MediaAsset(
        media_id=derivative_id,
        role="standardized",
        original_ref=original_ref,
        derived_from=original_media_id,
        probe=derivative_probe.media,
        time_mapping=list(mapping),
        time_mapping_valid=True,
        simulation=simulation,
    )
    return NormalizeOutcome(
        status="normalized",
        derivative_path=final_path,
        media=media,
        mapping=mapping,
        source=_source_report(source_probe, frame_times),
        notes=(),
    )


# ---------------------------------------------------------------------------
# Measured helpers
# ---------------------------------------------------------------------------


def _measure_frame_times(
    location: Path, reader: FrameTimesReader, timeout: float, max_frames: int
) -> tuple[float, ...]:
    """Measure original frame timestamps with real ffprobe; () means unusable."""
    return reader(location, timeout, max_frames)


class _SystemFrameTimesReader:
    """Reads real frame pts timestamps via ffprobe (no shell, hard timeout)."""

    def __call__(self, location: Path, timeout: float, max_frames: int) -> tuple[float, ...]:
        argv = [
            "ffprobe",
            "-v",
            "quiet",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            str(location),
        ]
        try:
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (subprocess.TimeoutExpired, OSError):
            return ()
        if completed.returncode != 0:
            return ()
        times: list[float] = []
        for line in completed.stdout.splitlines():
            token = line.strip().split(",")[0]
            if not token or token == "N/A":
                continue
            try:
                times.append(float(token))
            except ValueError:
                return ()
            if len(times) >= max_frames:
                break
        return tuple(times)


def _run_ffmpeg(
    source: Path,
    target: Path,
    target_fps: float,
    process: ProcessRunner,
    timeout: float,
) -> None:
    # Rotation correction is delegated entirely to ffmpeg autorotate (the
    # display-matrix authority): it applies the container's rotation exactly
    # once, for any angle, on every platform. The probe-driven transpose that
    # used to live here DOUBLE-ROTATED videos whose display matrix was -90
    # (phase2-rotation diagnosis on a real phone MOV): ffmpeg auto-rotated
    # AND our transpose fired again. The probe rotation stays recorded in the
    # source report as observed metadata; it no longer drives filters.
    video_filter = f"fps={target_fps}"
    argv = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(source),
        "-vf",
        video_filter,
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-an",
        "-movflags",
        "+faststart",
        "-y",
        str(target),
    ]
    if process(argv, timeout) != 0:
        raise _RunnerTimeout("ffmpeg failed")


def _mapping_points(
    frame_times: tuple[float, ...], target_fps: float, max_points: int
) -> tuple[TimePoint, ...] | None:
    if not frame_times or target_fps <= 0:
        return None
    # sample EVENLY ACROSS THE WHOLE clip (not the first max_points frames):
    # a mapping that stops at 4s while the derivative runs 30s would be a
    # partial mapping marked valid, and out-of-span lookups would silently
    # clamp to the last measured point (phase4 review B1)
    step = max(1, -(-len(frame_times) // max(max_points, 1)))
    sampled = list(enumerate(frame_times))[::step]
    for previous, current in zip(sampled, sampled[1:], strict=False):
        if current[1] <= previous[1]:
            return None  # non-monotonic source timestamps cannot map safely
    points = tuple(
        TimePoint(normalized_s=index / target_fps, original_s=stamp) for index, stamp in sampled
    )
    return points or None


def _verify(source_probe: Any, derivative_probe: Any, target_fps: float) -> tuple[str, ...]:
    failures: list[str] = []
    source = source_probe.summary
    derivative = derivative_probe.summary
    expected_duration = float(source["duration_s"])
    actual_duration = float(derivative["duration_s"])
    tolerance = max(1.0, expected_duration * 0.05)
    if abs(actual_duration - expected_duration) > tolerance:
        failures.append("duration_mismatch")
    measured_fps = float(derivative["fps"])
    if measured_fps <= 0 or abs(measured_fps - target_fps) > target_fps * 0.2:
        failures.append("fps_mismatch")
    if derivative["rotation"] in ("90", "180", "270"):
        failures.append("rotation_not_upright")
    return tuple(failures)


def _source_report(source_probe: Any, frame_times: tuple[float, ...]) -> SourceReport:
    probe: MediaProbe = source_probe.media
    return SourceReport(
        duration_s=probe.duration_s,
        fps=probe.fps,
        rotation=probe.rotation,
        slow_motion=probe.slow_motion,
        variable_frame_rate=_is_variable(frame_times),
    )


def _is_variable(frame_times: tuple[float, ...]) -> bool:
    if len(frame_times) < 10:
        return False
    deltas = [b - a for a, b in zip(frame_times, frame_times[1:], strict=False) if b > a]
    if not deltas:
        return False
    return (max(deltas) / min(deltas)) > 1.5


def _discard(staging: Path | None) -> None:
    if staging is not None:
        try:
            staging.unlink()
        except OSError:
            pass
