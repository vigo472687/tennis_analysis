"""FFmpeg standardization and original-time mapping tests (BE-014).

Real behavior is proven against the local ffmpeg/ffprobe binaries: synthetic
clips (plain, rotated, corrupt) are standardized and re-probed for real.
Failure paths — ffmpeg failure/timeout, unreadable frame timestamps, mapping
breaks, verification mismatch — are covered with injected runners so no exotic
media has to be rendered. EX-043: a blocked outcome never yields a publishable
media asset.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.media.normalize import NormalizeOutcome, normalize_video
from tennis_backend.media.probe import probe_video

FFMPEG = shutil.which("ffmpeg")


def make_clip(
    directory: Path, name: str, *, seconds: float = 1.0, rotation: int | None = None
) -> Path:
    """Generate a tiny synthetic mp4 with real ffmpeg; skip-guarded caller.

    Rotation is applied as an input option on a remux pass (matches how
    phone recordings carry a Display Matrix).
    """
    assert FFMPEG is not None
    target = directory / name
    plain = directory / f"{name}.plain.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=64x64:d={seconds}:r=25",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(plain) if rotation is not None else str(target),
        ],
        check=True,
        capture_output=True,
    )
    if rotation is not None:
        subprocess.run(
            [
                FFMPEG,
                "-loglevel",
                "error",
                "-display_rotation",
                str(rotation),
                "-i",
                str(plain),
                "-c",
                "copy",
                "-y",
                str(target),
            ],
            check=True,
            capture_output=True,
        )
        plain.unlink()
    return target


@pytest.fixture(scope="module")
def plain_clip(tmp_path_factory: pytest.TempPathFactory) -> Path | None:
    if FFMPEG is None:
        return None
    return make_clip(tmp_path_factory.mktemp("norm-media"), "plain.mp4")


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    return out


def normalize(
    path: Path | None,
    workdir: Path,
    **overrides: Any,
) -> NormalizeOutcome:
    assert path is not None
    kwargs: dict[str, Any] = {
        "output_dir": workdir,
        "original_media_id": "media-src-1",
        "original_ref": "projects/task-demo/v1/media/source.mp4",
    }
    kwargs.update(overrides)
    return normalize_video(path, **kwargs)


# ---------------------------------------------------------------------------
# Real round-trips
# ---------------------------------------------------------------------------


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
def test_real_standardization_produces_valid_mapping(
    plain_clip: Path | None, workdir: Path
) -> None:
    outcome = normalize(plain_clip, workdir)
    assert outcome.status == "normalized"
    assert outcome.derivative_path is not None and outcome.derivative_path.exists()
    assert outcome.media is not None
    assert outcome.media.role == "standardized"
    assert outcome.media.derived_from == "media-src-1"
    assert outcome.media.time_mapping_valid is True
    assert len(outcome.mapping) > 0
    # mapping is measured and monotonic in both axes
    points = outcome.mapping
    assert all(p.original_s >= 0 for p in points)
    assert all(b.normalized_s > a.normalized_s for a, b in zip(points, points[1:], strict=False))
    assert all(b.original_s >= a.original_s for a, b in zip(points, points[1:], strict=False))
    # source report is measured
    assert outcome.source.duration_s == pytest.approx(1.0, abs=0.3)
    assert outcome.source.rotation in ("0", "unknown")
    # the derivative is a genuinely supported, upright, constant-fps file
    derivative = probe_video(outcome.derivative_path)
    assert derivative.summary["support"] == "supported"
    assert derivative.summary["rotation"] not in ("90", "180", "270")
    assert derivative.summary["fps"] == pytest.approx(25.0, rel=0.2)


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
def test_real_rotated_clip_is_upright_after_standardization(
    tmp_path_factory: pytest.TempPathFactory, workdir: Path
) -> None:
    rotated = make_clip(tmp_path_factory.mktemp("norm-rot"), "rotated.mp4", rotation=90)
    outcome = normalize(rotated, workdir)
    assert outcome.status == "normalized"
    assert outcome.source.rotation == "90"
    assert outcome.derivative_path is not None
    derivative = probe_video(outcome.derivative_path)
    # pixels are upright now: no rotation metadata remains on the derivative
    assert derivative.summary["rotation"] in ("0", "unknown")


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def test_missing_file_refused(workdir: Path) -> None:
    with pytest.raises(Exception) as error:
        normalize(workdir.parent / "nope.mp4", workdir)
    assert "DOMAIN_MEDIA_FILE_MISSING" in str(getattr(error.value, "code", error.value))


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
def test_corrupt_source_refused_before_any_work(tmp_path: Path, workdir: Path) -> None:
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"\x00\x01junk" * 64)
    with pytest.raises(Exception) as error:
        normalize(corrupt, workdir)
    assert "DOMAIN_MEDIA_NOT_SUPPORTED" in str(getattr(error.value, "code", error.value))
    assert list(workdir.iterdir()) == []  # nothing was produced


# ---------------------------------------------------------------------------
# Failure paths: blocked outcomes never produce formal evidence (EX-043)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
def test_ffmpeg_failure_blocks_without_derivative(plain_clip: Path | None, workdir: Path) -> None:
    def failing_runner(argv: list[str], timeout: float) -> int:
        return 1 if argv[0] == "ffmpeg" else 0

    outcome = normalize(plain_clip, workdir, runner=failing_runner)
    assert outcome.is_blocked is True
    assert outcome.derivative_path is None
    assert outcome.media is None
    assert outcome.media is None and outcome.mapping == ()
    assert "ffmpeg_failed_or_timeout" in outcome.notes


def test_frame_times_unreadable_blocks(tmp_path: Path, workdir: Path) -> None:
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    clip = make_clip(tmp_path, "src.mp4")
    # without real frame timestamps no mapping can be claimed valid
    outcome = normalize(
        clip,
        workdir,
        frame_reader=lambda path, timeout, max_frames: (),
    )
    assert outcome.is_blocked is True
    assert outcome.media is None
    assert outcome.mapping == ()
    assert "mapping_failed" in outcome.notes


def test_non_monotonic_frame_times_block_the_mapping(tmp_path: Path, workdir: Path) -> None:
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    clip = make_clip(tmp_path, "src2.mp4")
    jumbled = (0.0, 0.5, 0.2, 0.9, 1.1, 1.3, 1.5, 1.7, 1.9, 2.1, 2.3)
    outcome = normalize(clip, workdir, frame_reader=lambda path, timeout, max_frames: jumbled)
    assert outcome.is_blocked is True
    assert outcome.media is None


def test_verification_mismatch_blocks(tmp_path: Path, workdir: Path) -> None:
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    clip = make_clip(tmp_path, "src3.mp4")

    def lying_runner(argv: list[str], timeout: float) -> int:
        # ffmpeg "succeeds" but writes an empty file, so re-probe sees garbage
        if argv[0] == "ffmpeg":
            Path(argv[-1]).write_bytes(b"")
            return 0
        return 0

    outcome = normalize(clip, workdir, runner=lying_runner)
    assert outcome.is_blocked is True
    assert outcome.media is None
    assert outcome.derivative_path is None


def test_mapping_points_capped(tmp_path: Path, workdir: Path) -> None:
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    clip = make_clip(tmp_path, "src4.mp4")
    many = tuple(round(i * 0.04, 3) for i in range(400))
    outcome = normalize(
        clip,
        workdir,
        frame_reader=lambda path, timeout, max_frames: many,
        max_mapping_points=10,
    )
    assert outcome.status == "normalized"
    assert len(outcome.mapping) == 10
    assert outcome.media is not None


def test_variable_frame_rate_is_measured_not_assumed(tmp_path: Path, workdir: Path) -> None:
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    clip = make_clip(tmp_path, "src5.mp4")
    uniform = tuple(round(i * 0.04, 3) for i in range(50))
    even = normalize(clip, workdir, frame_reader=lambda p, t, m: uniform)
    assert even.source.variable_frame_rate is False
    uneven = tuple(
        sorted(
            [0.0, 0.04, 0.08, 0.20, 0.24, 0.28, 0.60, 0.64, 0.68, 0.72, 1.00],
        )
    )
    juddery = normalize(clip, workdir, frame_reader=lambda p, t, m: uneven)
    assert juddery.source.variable_frame_rate is True


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
def test_mapping_spans_the_whole_clip_beyond_first_100_frames(
    tmp_path: Path, workdir: Path
) -> None:
    """Phase4 review B1 regression: the mapping must sample the WHOLE clip.

    A 6s @ 25fps clip has 150 frames; a head-truncated mapping would cover
    only the first 100 frames (4s) while marking the mapping valid, so any
    out-of-span lookup silently clamped to the last measured point.
    """
    long_clip = make_clip(tmp_path, "long.mp4", seconds=6.0)
    outcome = normalize(long_clip, workdir)
    assert outcome.status == "normalized"
    assert outcome.media is not None and outcome.media.time_mapping_valid
    span = outcome.media.time_mapping[-1].normalized_s
    assert span >= 5.5, f"mapping covers only {span}s of a 6s clip"
    assert len(outcome.media.time_mapping) <= 100  # point budget kept
