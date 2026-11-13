"""Real ffprobe-based media content probing (BE-013).

Runs the local ``ffprobe`` binary (fixed argv, no shell, timeout) and maps
its JSON output onto the domain :class:`MediaProbe` (BE-004) plus the frozen
``media:summary`` wire shape, with ``observation_kind="measured"`` — probe
metadata is genuinely measured, and nothing here derives a pose, support of
an object or quality verdict from it.

Honesty rules (AC-SPEC-010, FR-003):

- ``duration_s``/``fps``/``width``/``height``/``codec`` come only from
  ffprobe output; a missing or unparseable essential field fails loudly
  (``MEDIA_PROBE_INVALID``) instead of inventing a value.
- ``device_source`` is copied from container/stream tags when present and is
  ``"unknown"`` otherwise — a device source is never fabricated.
- ``slow_motion`` is ``yes`` only when the file itself carries an explicit
  slow-motion marker tag; otherwise ``unknown`` (fps alone proves nothing).
- ``hdr`` is ``yes`` only for HDR transfer characteristics, ``no`` when a
  non-HDR transfer characteristic is present, ``unknown`` when absent.
- ``panorama`` is ``native`` only when spherical side data exists, ``flat``
  when a video stream exists without it, ``unknown`` without a video stream.
- ``support`` blocks before analysis (EX-040): longer than 180 s, a
  container other than MP4/MOV, or native panorama is ``unsupported``; a
  file ffprobe cannot read at all is ``unable``. Exactly 180.0 s is inside
  the limit.

Threading note (AG-B02): ``probe_video`` spawns the runner on the calling
thread; the transport worker (BE-012) may call it directly inside a job.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tennis_backend.domain.models import DomainViolation, MediaProbe

#: FR-002: a single video is at most 180 seconds; exactly 180.0 is allowed.
MAX_DURATION_S = 180.0

DEFAULT_TIMEOUT_S = 60.0

#: Containers the prototype accepts (RQ-002: standard MP4/MOV).
SUPPORTED_CONTAINERS = frozenset({"mp4", "mov"})

#: Transfer characteristics that mean HDR.
_HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})

#: Marker substrings treated as an explicit slow-motion tag.
_SLOW_MOTION_MARKERS = ("slofmo", "slow")

Runner = Callable[[list[str], float], tuple[int, str]]

_FILE_MISSING = "MEDIA_FILE_MISSING"
_PROBE_FAILED = "MEDIA_PROBE_FAILED"
_PROBE_INVALID = "MEDIA_PROBE_INVALID"


def _system_runner(argv: list[str], timeout: float) -> tuple[int, str]:
    """Run ffprobe with a fixed argv, no shell, and a hard timeout."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise DomainViolation(_PROBE_FAILED, f"ffprobe timed out after {timeout}s") from error
    except OSError as error:
        raise DomainViolation(_PROBE_FAILED, "ffprobe is not available") from error
    return completed.returncode, completed.stdout


def _resolve_runner(runner: Runner | None) -> Runner:
    return runner if runner is not None else _system_runner


@dataclass(frozen=True)
class ProbeOutcome:
    """One measured probe: the wire summary plus the domain entity."""

    summary: dict[str, Any]
    media: MediaProbe
    #: True when the file must be blocked before analysis (support != supported).
    is_blocked: bool
    #: Stable, sanitized notes (no paths, no raw ffprobe output).
    notes: tuple[str, ...]


def probe_video(
    path: str | Path,
    *,
    runner: Runner | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> ProbeOutcome:
    """Probe one local video file with real ffprobe; never fabricates values."""
    location = Path(path)
    if not location.is_file():
        raise DomainViolation(_FILE_MISSING, f"media file does not exist: {location.name}")
    argv = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(location),
    ]
    code, stdout = _resolve_runner(runner)(argv, timeout)
    if code != 0:
        # unreadable/corrupt: reported as unable, never guessed around
        summary = _blocked_summary(
            container="unknown",
            codec="unknown",
            duration_s=0.0,
            fps=0.0,
            width=0,
            height=0,
            rotation="unknown",
            slow_motion="unknown",
            hdr="unknown",
            device_source="unknown",
            panorama="unknown",
            support="unable",
            observation_kind="measured",
        )
        return ProbeOutcome(
            summary=summary,
            media=_to_media_probe(summary),
            is_blocked=True,
            notes=("probe_failed",),
        )
    try:
        report = json.loads(stdout)
    except ValueError as error:
        raise DomainViolation(_PROBE_INVALID, "ffprobe output is not valid JSON") from error
    media_summary = _summarize(report)
    return ProbeOutcome(
        summary=media_summary,
        media=_to_media_probe(media_summary),
        is_blocked=media_summary["support"] != "supported",
        notes=_notes(media_summary),
    )


# ---------------------------------------------------------------------------
# Mapping ffprobe JSON onto the measured shapes
# ---------------------------------------------------------------------------


def _summarize(report: dict[str, Any]) -> dict[str, Any]:
    video = _video_stream(report)
    fmt = report.get("format") or {}
    container = _container(str(fmt.get("format_name", "")))
    duration = _essential_float(fmt.get("duration"), "format.duration")
    fps = _fps(video)
    rotation = _rotation(video)
    hdr = _hdr(video)
    slow_motion = _slow_motion(fmt, video)
    panorama = _panorama(video)
    device_source = _device_source(fmt, video)
    support = _support(container, duration, panorama, bool(video))
    codec = str((video or {}).get("codec_name") or "unknown")
    width = int((video or {}).get("width") or 0)
    height = int((video or {}).get("height") or 0)
    return _blocked_summary(
        container=container,
        codec=codec[:128] or "unknown",
        duration_s=duration,
        fps=fps,
        width=width,
        height=height,
        rotation=rotation,
        slow_motion=slow_motion,
        hdr=hdr,
        device_source=device_source,
        panorama=panorama,
        support=support,
        observation_kind="measured",
    )


def _video_stream(report: dict[str, Any]) -> dict[str, Any] | None:
    streams: Any = report.get("streams") or []
    for stream in streams:
        if isinstance(stream, dict) and str(stream.get("codec_type")) == "video":
            return stream
    return None


def _container(format_name: str) -> str:
    names = format_name.lower()
    if "mp4" in names:
        return "mp4"
    if "mov" in names:
        return "mov"
    return "unknown"


def _essential_float(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise DomainViolation(_PROBE_INVALID, f"ffprobe did not report {field}") from error
    if parsed < 0:
        raise DomainViolation(_PROBE_INVALID, f"ffprobe reported a negative {field}")
    return parsed


def _fps(video: dict[str, Any] | None) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = str((video or {}).get(key) or "")
        num, _, den = rate.partition("/")
        try:
            numerator = float(num)
            denominator = float(den) if den else 1.0
        except ValueError:
            continue
        if denominator > 0 and numerator >= 0:
            return numerator / denominator
    return 0.0


def _rotation(video: dict[str, Any] | None) -> str:
    raw: Any = None
    tags = (video or {}).get("tags") or {}
    if "rotate" in tags:
        raw = tags["rotate"]
    for side in (video or {}).get("side_data_list") or []:
        if "rotation" in side:
            raw = side["rotation"]
    if raw is None:
        return "unknown"
    try:
        degrees = int(float(raw)) % 360
    except (TypeError, ValueError):
        return "unknown"
    if degrees % 90 != 0:
        return "unknown"
    return str(degrees)


def _hdr(video: dict[str, Any] | None) -> str:
    transfer = (video or {}).get("color_transfer")
    if transfer is None:
        return "unknown"
    return "yes" if str(transfer) in _HDR_TRANSFERS else "no"


def _slow_motion(fmt: dict[str, Any], video: dict[str, Any] | None) -> str:
    for tags in (fmt.get("tags"), (video or {}).get("tags")):
        for key, value in (tags or {}).items():
            key_lower = str(key).lower()
            if any(marker in key_lower for marker in _SLOW_MOTION_MARKERS):
                return "yes" if str(value).lower() not in ("0", "false") else "no"
    return "unknown"


def _panorama(video: dict[str, Any] | None) -> str:
    if video is None:
        return "unknown"
    for side in video.get("side_data_list") or []:
        if str(side.get("side_data_type", "")).lower().startswith("spherical"):
            return "native"
    return "flat"


def _device_source(fmt: dict[str, Any], video: dict[str, Any] | None) -> str:
    # Only copy what the file itself declares; never invent a source.
    keys = (
        "com.apple.quicktime.model",
        "com.apple.quicktime.make",
        "manufacturer",
        "device_name",
        "model",
    )
    for tags in (fmt.get("tags"), (video or {}).get("tags")):
        for key in keys:
            value = (tags or {}).get(key)
            if value:
                return str(value)[:128]
    return "unknown"


def _support(container: str, duration: float, panorama: str, has_video: bool) -> str:
    if not has_video or duration <= 0:
        return "unable"
    if duration > MAX_DURATION_S:
        return "unsupported"
    if panorama == "native":
        return "unsupported"
    if container not in SUPPORTED_CONTAINERS:
        return "unsupported"
    return "supported"


def _blocked_summary(**fields: Any) -> dict[str, Any]:
    return {
        "container": fields["container"],
        "codec": fields["codec"],
        "duration_s": fields["duration_s"],
        "fps": fields["fps"],
        "width": fields["width"],
        "height": fields["height"],
        "rotation": fields["rotation"],
        "slow_motion": fields["slow_motion"],
        "hdr": fields["hdr"],
        "device_source": fields["device_source"],
        "panorama": fields["panorama"],
        "support": fields["support"],
        "observation_kind": fields["observation_kind"],
    }


def _to_media_probe(summary: dict[str, Any]) -> MediaProbe:
    # summary values are produced by this module's measured mapping and match
    # the domain Literal vocabularies by construction.
    return MediaProbe(
        container=summary["container"],
        codec=summary["codec"],
        duration_s=summary["duration_s"],
        fps=summary["fps"],
        width=summary["width"],
        height=summary["height"],
        rotation=summary["rotation"],
        slow_motion=summary["slow_motion"],
        hdr=summary["hdr"],
        device_source=summary["device_source"],
        panorama=summary["panorama"],
        observation_kind=summary["observation_kind"],
    )


def _notes(summary: dict[str, Any]) -> tuple[str, ...]:
    notes: list[str] = []
    if summary["support"] != "supported":
        notes.append(f"blocked_{summary['support']}")
    if summary["duration_s"] > MAX_DURATION_S:
        notes.append("duration_over_limit")
    if summary["panorama"] == "native":
        notes.append("native_panorama")
    if summary["container"] not in SUPPORTED_CONTAINERS:
        notes.append("container_not_standard")
    return tuple(notes)
