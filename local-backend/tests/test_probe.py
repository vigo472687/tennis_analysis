"""Real ffprobe media content probing tests (BE-013).

Real behavior is proven against the local ffprobe/ffmpeg binaries: a small
synthetic clip is generated with ffmpeg and probed with real ffprobe, and a
corrupt file is probed for the ``unable`` path. Boundary, rotation, HDR,
panorama, device-source and container rules are covered with an injected
runner so no large or exotic media has to be rendered. No user content, no
network.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.domain.models import DomainViolation
from tennis_backend.media.probe import MAX_DURATION_S, probe_video

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "v1"
FFMPEG = shutil.which("ffmpeg")


def format_only(format_payload: dict[str, Any], stream: dict[str, Any] | None = None) -> str:
    report: dict[str, Any] = {"format": format_payload}
    if stream is not None:
        report["streams"] = [stream]
    return json.dumps(report)


def standard_stream(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "codec_type": "video",
        "codec_name": "h264",
        "width": 1920,
        "height": 1080,
        "avg_frame_rate": "30/1",
        "r_frame_rate": "30/1",
        "color_transfer": "bt709",
    }
    payload.update(overrides)
    return payload


def good_format(duration: float = 12.0) -> dict[str, Any]:
    return {"duration": str(duration), "format_name": "mov,mp4,m4a,3gp,3g2,mj2", "tags": {}}


def injected_runner(payload: str, code: int = 0) -> Any:
    def run(argv: list[str], timeout: float) -> tuple[int, str]:
        assert argv[0] == "ffprobe"
        assert "-print_format" in argv and "json" in argv
        return code, payload

    return run


def base_summary() -> dict[str, Any]:
    return {
        "container": "mp4",
        "codec": "h264",
        "duration_s": 12.0,
        "fps": 30.0,
        "width": 1920,
        "height": 1080,
        "rotation": "unknown",
        "slow_motion": "unknown",
        "hdr": "unknown",
        "device_source": "unknown",
        "panorama": "unknown",
        "support": "supported",
        "observation_kind": "measured",
    }


# ---------------------------------------------------------------------------
# Real ffprobe round-trip (synthetic clip generated with ffmpeg)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_clip(tmp_path_factory: pytest.TempPathFactory) -> Path | None:
    """A 2-second synthetic mp4; None when ffmpeg is unavailable."""
    if FFMPEG is None:
        return None
    directory = tmp_path_factory.mktemp("probe-media")
    target = directory / "flat-forehand.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=2:r=25",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    return target


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
def test_real_probe_measures_a_standard_clip(real_clip: Path | None) -> None:
    assert real_clip is not None
    outcome = probe_video(real_clip)
    summary = outcome.summary
    assert summary["container"] == "mp4"
    assert summary["codec"] == "h264"
    assert 1.5 <= summary["duration_s"] <= 3.0
    assert 20.0 <= summary["fps"] <= 30.0
    assert (summary["width"], summary["height"]) == (64, 64)
    assert summary["observation_kind"] == "measured"
    assert summary["support"] == "supported"
    assert outcome.is_blocked is False
    # honesty: the synthetic clip carries no device metadata
    assert summary["device_source"] == "unknown"
    assert summary["slow_motion"] == "unknown"
    # the summary satisfies the frozen media:summary contract
    registry = ContractRegistry.load(CONTRACTS_DIR)
    assert registry.validate(registry.definition_ref("media", "summary"), summary) == ()
    # and the domain entity mirrors the measured fields
    assert outcome.media.duration_s == summary["duration_s"]
    assert outcome.media.observation_kind == "measured"


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
def test_real_probe_corrupt_file_is_blocked_as_unable(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"\x00\x01\x02not a video at all" * 16)
    outcome = probe_video(corrupt)
    assert outcome.summary["support"] == "unable"
    assert outcome.is_blocked is True
    assert outcome.summary["container"] == "unknown"
    assert "probe_failed" in outcome.notes


def test_missing_file_refused(tmp_path: Path) -> None:
    with pytest.raises(DomainViolation) as error:
        probe_video(tmp_path / "nope.mp4")
    assert error.value.code == "DOMAIN_MEDIA_FILE_MISSING"


def test_ffprobe_failure_is_reported_not_guessed(tmp_path: Path) -> None:
    target = tmp_path / "x.mp4"
    target.write_bytes(b"x")

    def failing(argv: list[str], timeout: float) -> tuple[int, str]:
        return 1, ""

    outcome = probe_video(target, runner=failing)
    assert outcome.summary["support"] == "unable"
    assert outcome.is_blocked is True


# ---------------------------------------------------------------------------
# Injected-runner rules: boundary, rotation, HDR, panorama, device source
# ---------------------------------------------------------------------------


def probe_with(payload: str, tmp_path: Path) -> Any:
    target = tmp_path / "v.mp4"
    target.write_bytes(b"x")
    return probe_video(target, runner=injected_runner(payload))


def test_duration_boundary_exactly_180_is_inside(tmp_path: Path) -> None:
    outcome = probe_with(format_only(good_format(MAX_DURATION_S), standard_stream()), tmp_path)
    assert outcome.summary["support"] == "supported"
    assert outcome.is_blocked is False


def test_duration_over_180_is_blocked_before_analysis(tmp_path: Path) -> None:
    outcome = probe_with(format_only(good_format(180.5), standard_stream()), tmp_path)
    assert outcome.summary["support"] == "unsupported"
    assert outcome.is_blocked is True
    assert "duration_over_limit" in outcome.notes


def test_non_standard_container_is_unsupported(tmp_path: Path) -> None:
    payload = format_only(
        {"duration": "5", "format_name": "matroska,webm", "tags": {}},
        standard_stream(),
    )
    outcome = probe_with(payload, tmp_path)
    assert outcome.summary["container"] == "unknown"
    assert outcome.summary["support"] == "unsupported"
    assert "container_not_standard" in outcome.notes


def test_rotation_from_side_data_and_tags(tmp_path: Path) -> None:
    side = format_only(
        good_format(),
        standard_stream(side_data_list=[{"side_data_type": "Display Matrix", "rotation": -90}]),
    )
    assert probe_with(side, tmp_path).summary["rotation"] == "270"
    tagged = format_only(good_format(), standard_stream(tags={"rotate": "90"}))
    assert probe_with(tagged, tmp_path).summary["rotation"] == "90"
    odd = format_only(good_format(), standard_stream(tags={"rotate": "45"}))
    assert probe_with(odd, tmp_path).summary["rotation"] == "unknown"


def test_hdr_transfer_characteristics(tmp_path: Path) -> None:
    hdr = format_only(good_format(), standard_stream(color_transfer="smpte2084"))
    assert probe_with(hdr, tmp_path).summary["hdr"] == "yes"
    sdr = format_only(good_format(), standard_stream(color_transfer="bt709"))
    assert probe_with(sdr, tmp_path).summary["hdr"] == "no"
    absent = format_only(good_format(), standard_stream(color_transfer=None))
    assert probe_with(absent, tmp_path).summary["hdr"] == "unknown"


def test_native_panorama_is_blocked_flat_is_not(tmp_path: Path) -> None:
    spherical = format_only(
        good_format(),
        standard_stream(side_data_list=[{"side_data_type": "Spherical"}]),
    )
    outcome = probe_with(spherical, tmp_path)
    assert outcome.summary["panorama"] == "native"
    assert outcome.summary["support"] == "unsupported"
    assert "native_panorama" in outcome.notes
    flat = format_only(good_format(), standard_stream())
    assert probe_with(flat, tmp_path).summary["panorama"] == "flat"
    assert probe_with(flat, tmp_path).summary["support"] == "supported"


def test_device_source_only_from_declared_tags(tmp_path: Path) -> None:
    declared = format_only(
        {**good_format(), "tags": {"com.apple.quicktime.model": "iPhone 15 Pro"}},
        standard_stream(),
    )
    assert probe_with(declared, tmp_path).summary["device_source"] == "iPhone 15 Pro"
    silent = format_only(good_format(), standard_stream())
    assert probe_with(silent, tmp_path).summary["device_source"] == "unknown"


def test_slow_motion_requires_explicit_marker(tmp_path: Path) -> None:
    high_fps_only = format_only(good_format(), standard_stream(avg_frame_rate="240/1"))
    assert probe_with(high_fps_only, tmp_path).summary["slow_motion"] == "unknown"
    marker = format_only(
        {**good_format(), "tags": {"com.apple.quicktime.slofmo": "1"}},
        standard_stream(),
    )
    assert probe_with(marker, tmp_path).summary["slow_motion"] == "yes"


def test_invalid_json_and_missing_duration_fail_loudly(tmp_path: Path) -> None:
    target = tmp_path / "v.mp4"
    target.write_bytes(b"x")
    with pytest.raises(DomainViolation) as error:
        probe_video(target, runner=injected_runner("{not json"))
    assert error.value.code == "DOMAIN_MEDIA_PROBE_INVALID"
    no_duration = format_only({"format_name": "mp4"}, standard_stream())
    with pytest.raises(DomainViolation) as error:
        probe_video(target, runner=injected_runner(no_duration))
    assert error.value.code == "DOMAIN_MEDIA_PROBE_INVALID"


def test_no_video_stream_is_unable(tmp_path: Path) -> None:
    audio_only = json.dumps(
        {
            "format": good_format(),
            "streams": [{"codec_type": "audio", "codec_name": "aac"}],
        }
    )
    outcome = probe_with(audio_only, tmp_path)
    assert outcome.summary["support"] == "unable"
    assert outcome.is_blocked is True
