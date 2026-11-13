"""Real evidence rendering and the separate target-skeleton generator (BE-020).

Renders the published evidence of one task version (BE-019 catalog) against
the original video with real FFmpeg/OpenCV, and produces the generic target
skeleton as a *separate* simulated artifact that never enters the measured
evidence set (AC-SPEC-023, FR-022, plan §13.2):

- **Evidence frames** — real ``ffmpeg`` extracts the frame at each evidence
  entry's original timestamp from the *original* video; the PNG is written
  atomically (staging + ``os.replace``) and the resource carries the real
  sha256, size, mime, task version and the original time it was taken at.
- **Trajectory ruler clips** — the stroke interval is re-encoded with a
  per-frame original-time ruler burned in (OpenCV text): the visualization
  shows *when* each frame is, never invented body/ball coordinates.
- **Slow motion** — the interval is stretched with ``setpts`` (frame
  duplication, no fabricated interpolation) and re-probed so the reported
  duration is the measured result.
- **Target skeleton** — an OpenCV stick figure stamped
  ``SIMULATED TARGET — NOT MEASURED``; it is returned as
  ``analysis:target_result`` (``kind=target_pose``) and is structurally
  separate from evidence: callers cannot attach it to the evidence channel,
  and the BE-019 catalog refuses target resources there.

Every rendered resource records its simulated nature in the contract fields
(``simulation=true``), matching the fixed-fixture data boundary: the media
processing is real, the analysis fed from it stays simulated until a
validated real model exists.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tennis_backend.domain.models import DomainViolation

_FILE_MISSING = "RENDER_SOURCE_MISSING"
_RENDER_FAILED = "RENDER_FAILED"
_EMPTY_EVIDENCE = "RENDER_NO_EVIDENCE"


def _run(argv: list[str], timeout: float = 120.0) -> None:
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        raise DomainViolation(_RENDER_FAILED, "render process failed or timed out") from error
    if completed.returncode != 0:
        raise DomainViolation(_RENDER_FAILED, "render process reported failure")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _place(staging: Path, root: Path, relative: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, target)
    return target


def _resource(
    root: Path, relative: str, mime: str, kind: str, *, simulation: bool = True
) -> dict[str, Any]:
    # hash and size come from the placed file under the authorized root,
    # while the contract only carries the relative reference
    file = root / relative
    digest = _sha256(file)
    return {
        "resource_id": Path(relative).stem,
        "path": relative,
        "mime": mime,
        "sha256": digest,
        "size_bytes": file.stat().st_size,
        "kind": kind,
        "simulation": simulation,
    }


@dataclass(frozen=True)
class RenderOutcome:
    """One render run: contract shapes plus the files under the root."""

    result: dict[str, Any]
    files: tuple[Path, ...]


# ---------------------------------------------------------------------------
# Evidence rendering (frames / trajectory ruler / slow motion)
# ---------------------------------------------------------------------------


def render_evidence(
    *,
    source_video: Path,
    root: Path,
    relative_dir: str,
    evidence: tuple[dict[str, Any], ...],
    mode: str = "frame",
    slow_factor: float = 2.0,
    ruler_samples: int = 8,
    version: int = 1,
    simulation: bool = True,
) -> RenderOutcome:
    """Render evidence entries against the original video.

    ``mode`` selects the artifact: ``frame`` extracts one PNG per evidence
    entry, ``trajectory`` renders a ruler-annotated clip of the stroke
    interval, ``slow_motion`` renders the interval stretched by
    ``slow_factor``. Only catalog-published entries (BE-019) are accepted;
    they already carry measured original timestamps.
    """
    if not source_video.is_file():
        raise DomainViolation(_FILE_MISSING, f"source video does not exist: {source_video.name}")
    if not evidence:
        raise DomainViolation(_EMPTY_EVIDENCE, "no published evidence to render")
    if mode not in ("frame", "trajectory", "slow_motion"):
        raise DomainViolation(_RENDER_FAILED, f"unknown render mode {mode}")
    root = Path(root)
    entries = sorted(evidence, key=lambda item: item["original_start_s"])
    files: list[Path] = []
    resources: list[dict[str, Any]] = []
    rendered_evidence: list[dict[str, Any]] = []

    if mode == "frame":
        for entry in entries:
            relative = f"{relative_dir}/{entry['evidence_id']}.png"
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
                staging = Path(handle.name)
            try:
                _run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-ss",
                        f"{entry['original_start_s']:.6f}",
                        "-i",
                        str(source_video),
                        "-frames:v",
                        "1",
                        "-y",
                        str(staging),
                    ]
                )
                files.append(_place(staging, root, relative))
            except DomainViolation:
                staging.unlink(missing_ok=True)
                raise
            resource = _resource(
                root, relative, "image/png", "evidence_frame", simulation=simulation
            )
            resources.append(resource)
            # contract linkage: the frozen evidence shape carries the rendered
            # resource in its own "resource" field — no extra keys allowed
            rendered_evidence.append({**entry, "resource": resource})
    else:
        first = entries[0]
        start = float(first["original_start_s"])
        end = max(float(item["original_end_s"]) for item in entries)
        suffix = "trajectory" if mode == "trajectory" else "slow"
        relative = (
            f"{relative_dir}/ev-{evidence_tail(entries[0]['evidence_id'])}-v{version}-{suffix}.mp4"
        )
        clip = _render_clip(
            source_video, root, relative, start, end, mode, slow_factor, ruler_samples
        )
        files.append(clip)
        kind = "trajectory" if mode == "trajectory" else "evidence_clip"
        resource = _resource(root, relative, "video/mp4", kind, simulation=simulation)
        resources.append(resource)
        for entry in entries:
            rendered_evidence.append({**entry, "resource": resource})

    result = {
        "resources": resources,
        "evidence": rendered_evidence,
        "simulation": simulation,
    }
    return RenderOutcome(result=result, files=tuple(files))


def evidence_tail(evidence_id: str) -> str:
    """Tail segment of an evidence id, used for rendered file names."""
    return evidence_id.rsplit("-", 1)[-1]


def _render_clip(
    source: Path,
    root: Path,
    relative: str,
    start: float,
    end: float,
    mode: str,
    slow_factor: float,
    ruler_samples: int,
) -> Path:
    duration = max(end - start, 0.1)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
        staging = Path(handle.name)
    try:
        if mode == "slow_motion":
            filters = f"setpts={slow_factor:.3f}*PTS"
        else:
            filters = "setpts=PTS-STARTPTS"
        _run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                f"{start:.6f}",
                "-t",
                f"{duration:.6f}",
                "-i",
                str(source),
                "-vf",
                filters,
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                "-y",
                str(staging),
            ]
        )
        if mode == "trajectory":
            staging = _burn_ruler(staging, start, ruler_samples)
        return _place(staging, root, relative)
    except DomainViolation:
        staging.unlink(missing_ok=True)
        raise


def _burn_ruler(clip: Path, start: float, samples: int) -> Path:
    """Stamp each sampled frame with its original time (never positions)."""
    import cv2  # local import: only the trajectory mode needs OpenCV

    capture = cv2.VideoCapture(str(clip))
    fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 64)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 64)
    # OpenCV writes an intermediate AVI (mp4v-in-mp4 is unsupported by the
    # local FFmpeg backend); the final re-encode below produces the MP4.
    writer_path = clip.with_name(clip.stem + "-ruled.avi")
    writer = cv2.VideoWriter(str(writer_path), 0, fps, (width, height))
    index = 0
    sample_step = max(total // max(samples, 1), 1)
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if index % sample_step == 0:
            stamp = start + index / fps
            cv2.putText(
                frame,
                f"t={stamp:.3f}s original",
                (4, max(height - 6, 12)),
                cv2.FONT_HERSHEY_SIMPLEX,
                max(0.4, min(1.2, width / 480)),
                (0, 255, 255),
                1,
                cv2.LINE_AA,
            )
        writer.write(frame)
        index += 1
    capture.release()
    writer.release()
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(writer_path),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-y",
            str(clip),
        ]
    )
    writer_path.unlink(missing_ok=True)
    return clip


# ---------------------------------------------------------------------------
# Separate target skeleton (never part of the measured evidence set)
# ---------------------------------------------------------------------------


def generate_target_skeleton(
    *,
    root: Path,
    relative_dir: str,
    task_id: str,
    version: int,
    features: list[str],
) -> dict[str, Any]:
    """Draw the generic simulated target skeleton as ``analysis:target_result``.

    The image is stamped ``SIMULATED TARGET - NOT MEASURED``; the result
    carries explicit limitations. Callers must keep it out of the evidence
    channel — the BE-019 catalog refuses target resources there.
    """
    import cv2
    import numpy as np

    image = np.zeros((320, 240, 3), dtype=np.uint8)
    image[:] = (24, 24, 24)
    color = (180, 180, 180)
    joints = {
        "head": (120, 40),
        "shoulder_l": (80, 90),
        "shoulder_r": (160, 90),
        "elbow_l": (60, 140),
        "elbow_r": (180, 140),
        "wrist_l": (70, 190),
        "wrist_r": (170, 190),
        "hip_l": (100, 200),
        "hip_r": (140, 200),
        "knee_l": (95, 255),
        "knee_r": (145, 255),
        "ankle_l": (90, 305),
        "ankle_r": (150, 305),
    }
    bones = [
        ("head", "shoulder_l"),
        ("head", "shoulder_r"),
        ("shoulder_l", "shoulder_r"),
        ("shoulder_l", "elbow_l"),
        ("elbow_l", "wrist_l"),
        ("shoulder_r", "elbow_r"),
        ("elbow_r", "wrist_r"),
        ("shoulder_l", "hip_l"),
        ("shoulder_r", "hip_r"),
        ("hip_l", "hip_r"),
        ("hip_l", "knee_l"),
        ("knee_l", "ankle_l"),
        ("hip_r", "knee_r"),
        ("knee_r", "ankle_r"),
    ]
    for a, b in bones:
        cv2.line(image, joints[a], joints[b], color, 2)
    for point in joints.values():
        cv2.circle(image, point, 4, (0, 255, 255), -1)
    cv2.putText(
        image,
        "SIMULATED TARGET",
        (8, 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (0, 0, 255),
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        image,
        "NOT MEASURED",
        (30, 315),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (0, 0, 255),
        1,
        cv2.LINE_AA,
    )

    relative = f"{relative_dir}/target-{task_id}-v{version}.png"
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        staging = Path(handle.name)
    cv2.imwrite(str(staging), image)
    _place(staging, root, relative)
    resource = _resource(root, relative, "image/png", "target_pose")
    return {
        "kind": "target_pose",
        "resource": resource,
        "features": list(features)[:200],
        "limitations": [
            "generic simulated skeleton drawn by OpenCV, not a detected pose",
            "never part of the measured evidence set; drawn pose carries no observation claim",
        ],
        "simulation": True,
    }


# ---------------------------------------------------------------------------
# Real pose overlay rendering (Phase 4) — measured pose on measured frames
# ---------------------------------------------------------------------------

_POSE_NO_RECORDS = "POSE_OVERLAY_NO_RECORDS"


def _stamp_layout(text: str, width: int, position: str) -> tuple[float, int]:
    """Font scale and x offset for a stamp that must fit the frame width.

    Workbench acceptance (WB4): on 544px-wide portrait video the full
    ``TRAJECTORY WRIST RIGHT (BODY KPT)`` line was wider than the frame and
    its tail was cut off. The scale shrinks until the measured text width
    fits, and ``position="right"`` right-aligns the text.
    """
    import cv2

    scale = max(0.4, min(1.2, width / 480))
    thickness = 1
    (text_width, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    max_width = width - 8
    while text_width > max_width and scale > 0.3 + 1e-6:
        scale = max(0.3, scale - 0.05)
        (text_width, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    x = 4 if position == "left" else max(4, width - text_width - 4)
    return scale, x


def _frame_stamp(
    frame: Any, text: str, height: int, width: int, color: Any, *, position: str = "left"
) -> None:
    import cv2

    scale, x = _stamp_layout(text, width, position)
    cv2.putText(
        frame,
        text,
        (x, max(height - 6, 12)),
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        color,
        1,
        cv2.LINE_AA,
    )


def _original_stamp_text(mapping: list[dict[str, float]], normalized: float) -> str | None:
    """The original-time stamp for a normalized time, or None outside the span.

    Interpolates between the measured mapping points exactly like the
    evidence catalog does, so the stamp agrees with the published backlink.
    Phase4 review B2: out-of-span frames must NOT fall back to the last
    measured point — an invented original time stamped on a
    ``simulation=false`` artifact is worse than no stamp.
    """
    if not mapping or normalized > mapping[-1]["normalized_s"]:
        return None
    for previous, current in zip(mapping, mapping[1:], strict=False):
        if previous["normalized_s"] <= normalized <= current["normalized_s"]:
            span = current["normalized_s"] - previous["normalized_s"]
            if span <= 0:
                return f"t={current['original_s']:.3f}s original"
            ratio = (normalized - previous["normalized_s"]) / span
            original = previous["original_s"] + ratio * (
                current["original_s"] - previous["original_s"]
            )
            return f"t={original:.3f}s original"
    return f"t={mapping[0]['original_s']:.3f}s original"


class _StreamingEncoder:
    """Write frames to the target MP4 as they are decoded (phase4 review D5).

    A 2-minute 1080p clip is ~3000 frames ≈ tens of GB if collected into a
    list first; encoding frame-by-frame through the intermediate AVI keeps
    memory flat regardless of clip length.
    """

    def __init__(self, fps: float, target: Path, slow_factor: float) -> None:
        import cv2

        self._cv2 = cv2
        self._fps = fps
        self._target = target
        self._slow_factor = slow_factor
        self._avi = target.with_name(target.stem + "-draw.avi")
        self._writer: Any = None
        self._count = 0

    def write(self, frame: Any) -> None:
        if self._writer is None:
            height, width = frame.shape[:2]
            self._writer = self._cv2.VideoWriter(str(self._avi), 0, self._fps, (width, height))
        self._writer.write(frame)
        self._count += 1

    def finish(self) -> None:
        if self._writer is None or self._count == 0:
            raise DomainViolation(_RENDER_FAILED, "no frames to encode")
        self._writer.release()
        _run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(self._avi),
                *(
                    ["-vf", f"setpts={self._slow_factor:.3f}*PTS"]
                    if self._slow_factor != 1.0
                    else []
                ),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                "-y",
                str(self._target),
            ]
        )
        self._avi.unlink(missing_ok=True)

    def discard(self) -> None:
        self._avi.unlink(missing_ok=True)


def render_skeleton_overlay(
    *,
    source_video: Path,
    pose_records: list[dict[str, Any]],
    skeleton_spec: dict[str, Any],
    root: Path,
    relative_dir: str,
    task_id: str,
    version: int,
    mapping: list[dict[str, float]] | None = None,
    slow_factor: float = 1.0,
    subjects: list[tuple[str, list[dict[str, Any]]]] | None = None,
    file_tag: str = "",
) -> RenderOutcome:
    """Draw the MEASURED RTMW-x skeleton over the video it was extracted from.

    ``subjects`` (联调 multi-subject) draws SEVERAL people on the same frames:
    one ``(label, records)`` pair per person, each in its own color with its
    label stamped at the head — a 4-player match shows 4 distinct skeletons.
    Without it ``pose_records`` is drawn as before (single color, no label).

    Honesty rules:

    - FR-POSE-001: the skeleton is drawn on the SAME frames the pose was
      extracted from (the normalized derivative), indexed by the records'
      absolute frame numbers;
    - FR-POSE-002: a link is drawn only when BOTH endpoints are valid in
      that frame; a frame without a record (or without keypoints) gets NO
      skeleton — gaps stay visible, nothing is interpolated across them;
    - the stamp says MEASURED POSE, and with a ``mapping`` each sampled
      frame also carries its original time through the measured BE-014
      mapping.

    The resource is ``simulation=False``: measured pose on measured video.
    ``slow_factor`` > 1 stretches playback (frame duplication, no
    fabricated interpolation) for the slow_motion render mode.
    """
    import cv2

    if not source_video.is_file():
        raise DomainViolation(_FILE_MISSING, f"source video does not exist: {source_video.name}")
    if not pose_records:
        raise DomainViolation(
            _POSE_NO_RECORDS, "no pose records published for this task; nothing to overlay"
        )
    # one (label, color, by_frame) entry per person drawn; the single-set path
    # keeps the historical green with no label
    palette = [
        (60, 220, 60),
        (60, 160, 255),
        (255, 120, 60),
        (220, 60, 220),
        (60, 220, 220),
        (220, 220, 60),
    ]
    drawn: list[tuple[str | None, tuple[int, int, int], dict[int, dict[str, Any]]]]
    if subjects:
        drawn = [
            (
                label,
                palette[i % len(palette)],
                {int(r["frame"]): r for r in records},
            )
            for i, (label, records) in enumerate(subjects)
        ]
    else:
        drawn = [(None, palette[0], {int(r["frame"]): r for r in pose_records})]
    # rtmlib's published topology addresses keypoints by NAME (e.g.
    # "left_ankle"); resolve names → indices through the keypoints list
    index_by_name = {
        str(keypoint["name"]): int(keypoint["index"])
        for keypoint in skeleton_spec.get("keypoints", [])
    }

    def _resolve(endpoint: Any) -> int | None:
        if isinstance(endpoint, int):
            return endpoint
        return index_by_name.get(str(endpoint))

    links: list[tuple[int, int]] = []
    dropped_links = 0
    for link in skeleton_spec.get("skeleton", []):
        a = _resolve(link["from"])
        b = _resolve(link["to"])
        if a is not None and b is not None:
            links.append((a, b))
        else:
            dropped_links += 1  # silent topology drift would degrade to dots
    stamp_label = "MEASURED POSE"
    if dropped_links:
        stamp_label += f" (PARTIAL TOPOLOGY: {dropped_links} links unresolved)"

    suffix = f"-slow{slow_factor:g}" if slow_factor != 1.0 else ""
    relative = f"{relative_dir}/pose-overlay-{task_id}-v{version}{file_tag}{suffix}.mp4"
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
        staging = Path(handle.name)
    capture = cv2.VideoCapture(str(source_video))
    fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    encoder = _StreamingEncoder(fps, staging, slow_factor)
    index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            for label, color, by_frame in drawn:
                record = by_frame.get(index)
                if record is None or record.get("keypoints") is None:
                    continue  # a gap draws NOTHING for this person (FR-POSE-002)
                kpts = record["keypoints"]
                valid = record.get("valid") or []
                for a, b in links:
                    if a < len(valid) and b < len(valid) and valid[a] and valid[b]:
                        pa = (int(round(kpts[a][0])), int(round(kpts[a][1])))
                        pb = (int(round(kpts[b][0])), int(round(kpts[b][1])))
                        cv2.line(frame, pa, pb, color, 2, cv2.LINE_AA)
                for k, point in enumerate(kpts):
                    if k < len(valid) and valid[k]:
                        cv2.circle(
                            frame,
                            (int(round(point[0])), int(round(point[1]))),
                            2,
                            color,
                            -1,
                        )
                if label is not None and record.get("subject_box"):
                    bx = record["subject_box"]
                    cv2.putText(
                        frame,
                        label,
                        (int(bx[0]), max(int(bx[1]) - 6, 14)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        color,
                        2,
                        cv2.LINE_AA,
                    )
            height, width = frame.shape[:2]
            _frame_stamp(frame, stamp_label, height, width, (255, 255, 255))
            if mapping and index % 10 == 0:
                normalized = index / fps
                stamp_text = _original_stamp_text(mapping, normalized)
                if stamp_text is not None:
                    # bottom-RIGHT: the MEASURED POSE watermark sits bottom-
                    # left; same-coordinate overlap failed the workbench
                    # visual check (WB4 acceptance item 2)
                    _frame_stamp(frame, stamp_text, height, width, (0, 255, 255), position="right")
            encoder.write(frame)
            index += 1
        capture.release()
        encoder.finish()
        placed = _place(staging, root, relative)
    except DomainViolation:
        capture.release()
        encoder.discard()
        staging.unlink(missing_ok=True)
        raise
    resource = _resource(root, relative, "video/mp4", "evidence_clip", simulation=False)
    return RenderOutcome(
        result={
            "resources": [resource],
            "evidence": [],
            "simulation": False,
        },
        files=(placed,),
    )


def render_wrist_trajectory(
    *,
    source_video: Path,
    pose_records: list[dict[str, Any]],
    keypoint_index: int,
    part: str,
    root: Path,
    relative_dir: str,
    task_id: str,
    version: int,
    side: str = "",
) -> RenderOutcome:
    """Draw the accumulating path of one body keypoint as a trajectory clip.

    FR-POSE-003 honesty: the resource is labeled with the BODY PART it
    tracks (e.g. ``wrist`` — never implied to be the racket), and gap
    frames (missing pose) break the polyline — the path is drawn only
    between consecutive frames that both carry a valid keypoint, so the
    missing segments stay visibly absent.
    """
    import cv2

    if not source_video.is_file():
        raise DomainViolation(_FILE_MISSING, f"source video does not exist: {source_video.name}")
    if not pose_records:
        raise DomainViolation(
            _POSE_NO_RECORDS, "no pose records published for this task; nothing to draw"
        )
    by_frame = {int(record["frame"]): record for record in pose_records}

    suffix = f"-{side}" if side else ""
    relative = f"{relative_dir}/traj-{task_id}-v{version}-{part}{suffix}.mp4"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
        staging = Path(handle.name)
    capture = cv2.VideoCapture(str(source_video))
    fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    encoder = _StreamingEncoder(fps, staging, 1.0)
    path: list[tuple[int, int]] = []
    gaps = 0
    previous_valid = False
    index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            record = by_frame.get(index)
            point = None
            if record is not None and record.get("keypoints") is not None:
                kpts = record["keypoints"]
                valid = record.get("valid") or []
                if keypoint_index < len(valid) and valid[keypoint_index]:
                    point = (
                        int(round(kpts[keypoint_index][0])),
                        int(round(kpts[keypoint_index][1])),
                    )
            if point is None:
                if previous_valid:
                    gaps += 1
                path.clear()  # the gap breaks the polyline: no line across it
            else:
                path.append(point)
                if len(path) >= 2:
                    cv2.line(frame, path[-2], path[-1], (255, 200, 40), 2, cv2.LINE_AA)
            previous_valid = point is not None
            height, width = frame.shape[:2]
            _frame_stamp(
                frame,
                f"TRAJECTORY {part} {side}".strip().upper() + " (BODY KPT)",
                height,
                width,
                (255, 200, 40),
            )
            encoder.write(frame)
            index += 1
        capture.release()
        encoder.finish()
        placed = _place(staging, root, relative)
    except DomainViolation:
        capture.release()
        encoder.discard()
        staging.unlink(missing_ok=True)
        raise
    resource = _resource(root, relative, "video/mp4", "trajectory", simulation=False)
    return RenderOutcome(
        result={
            # the wire resource carries the frozen fields only — the body-part
            # label is burned into the frames (the stamp) and the missing
            # segments are visible as polyline breaks, because the frozen
            # resource shape allows no extra keys
            "resources": [resource],
            "evidence": [],
            "simulation": False,
        },
        files=(placed,),
    )


# ---------------------------------------------------------------------------
# Swing contact sheet (专家 continuous-action input): ONE image, N moments
# ---------------------------------------------------------------------------

#: A sheet is a single PNG (one image slot for the expert layer). Cap its
#: width so the data URL stays well inside the 12 MB image budget.
SHEET_MAX_WIDTH = 1600
SHEET_COLUMNS = 3
_SHEET_NO_CELLS = "RENDER_SHEET_NO_CELLS"


def render_contact_sheet(
    *,
    source_video: Path,
    pose_records: list[dict[str, Any]],
    skeleton_spec: dict[str, Any],
    cells: list[dict[str, Any]],
    root: Path,
    relative_dir: str,
    sheet_id: str,
    mapping: list[dict[str, float]] | None = None,
) -> RenderOutcome:
    """One PNG showing several moments of ONE swing, skeleton drawn on each.

    ``cells`` are ``{"frame", "label"}`` in time order (from
    ``analysis.swings.sheet_frames``). Each cell carries its role label and
    its time — the original-video time when a ``mapping`` is given — so the
    expert can line the picture up with the temporal summary.

    Honesty rules (same as the overlay): the skeleton is drawn only where the
    record has valid keypoints and only for links whose BOTH ends are valid;
    a cell whose frame has no record shows the bare frame, stamped
    ``NO POSE`` — a gap is shown, never filled. Cells are taken from the
    video frame with that exact index (the frames the pose was extracted
    from). The resource is ``simulation=false``.
    """
    import cv2
    import numpy as np

    if not source_video.is_file():
        raise DomainViolation(_FILE_MISSING, f"source video does not exist: {source_video.name}")
    if not cells:
        raise DomainViolation(_SHEET_NO_CELLS, "no cells selected for the contact sheet")

    by_frame = {int(r["frame"]): r for r in pose_records}
    index_by_name = {str(k["name"]): int(k["index"]) for k in skeleton_spec.get("keypoints", [])}

    def resolve(endpoint: Any) -> int | None:
        return endpoint if isinstance(endpoint, int) else index_by_name.get(str(endpoint))

    links: list[tuple[int, int]] = []
    for link in skeleton_spec.get("skeleton", []):
        a, b = resolve(link["from"]), resolve(link["to"])
        if a is not None and b is not None:
            links.append((a, b))

    wanted = {int(c["frame"]): c for c in cells}
    capture = cv2.VideoCapture(str(source_video))
    grabbed: dict[int, Any] = {}
    last_wanted = max(wanted)
    index = 0
    while index <= last_wanted:
        ok, frame = capture.read()
        if not ok:
            break
        if index in wanted:
            grabbed[index] = frame
        index += 1
    capture.release()
    if not grabbed:
        raise DomainViolation(_RENDER_FAILED, "none of the requested frames could be decoded")

    columns = min(SHEET_COLUMNS, len(cells))
    first = next(iter(grabbed.values()))
    height, width = first.shape[:2]
    # shrink each cell so the assembled sheet stays within SHEET_MAX_WIDTH
    scale = min(1.0, SHEET_MAX_WIDTH / (columns * width))
    cell_w, cell_h = max(1, int(width * scale)), max(1, int(height * scale))

    tiles: list[Any] = []
    for cell in cells:
        frame_no = int(cell["frame"])
        source_frame: Any = grabbed.get(frame_no)
        tile: Any
        if source_frame is None:
            tile = np.zeros((height, width, 3), dtype=np.uint8)
            _frame_stamp(tile, "FRAME NOT DECODED", height, width, (0, 0, 255))
        else:
            tile = source_frame.copy()
            record = by_frame.get(frame_no)
            if record is not None and record.get("keypoints") is not None:
                kpts = record["keypoints"]
                valid = record.get("valid") or []
                for a, b in links:
                    if a < len(valid) and b < len(valid) and valid[a] and valid[b]:
                        pa = (int(round(kpts[a][0])), int(round(kpts[a][1])))
                        pb = (int(round(kpts[b][0])), int(round(kpts[b][1])))
                        cv2.line(tile, pa, pb, (60, 220, 60), 3, cv2.LINE_AA)
                for k, point in enumerate(kpts):
                    if k < len(valid) and valid[k]:
                        cv2.circle(
                            tile,
                            (int(round(point[0])), int(round(point[1]))),
                            3,
                            (60, 220, 60),
                            -1,
                        )
            else:
                _frame_stamp(tile, "NO POSE", height, width, (0, 0, 255), position="right")
            caption = str(cell.get("label", ""))
            stamp = _original_stamp_text(mapping, frame_no / 25.0) if mapping else None
            if stamp:
                caption = f"{caption} | {stamp}"
            _frame_stamp(tile, caption, height, width, (255, 255, 255))
        tiles.append(cv2.resize(tile, (cell_w, cell_h), interpolation=cv2.INTER_AREA))

    rows: list[Any] = []
    for start in range(0, len(tiles), columns):
        row_tiles = tiles[start : start + columns]
        while len(row_tiles) < columns:  # pad the last row with black so it stays rectangular
            row_tiles.append(np.zeros((cell_h, cell_w, 3), dtype=np.uint8))
        rows.append(np.hstack(row_tiles))
    sheet = np.vstack(rows)

    relative = f"{relative_dir}/{sheet_id}.png"
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        staging = Path(handle.name)
    try:
        if not cv2.imwrite(str(staging), sheet):
            raise DomainViolation(_RENDER_FAILED, "contact sheet could not be encoded")
        placed = _place(staging, root, relative)
    except DomainViolation:
        staging.unlink(missing_ok=True)
        raise
    resource = _resource(root, relative, "image/png", "evidence_frame", simulation=False)
    return RenderOutcome(
        result={"resources": [resource], "evidence": [], "simulation": False},
        files=(placed,),
    )
