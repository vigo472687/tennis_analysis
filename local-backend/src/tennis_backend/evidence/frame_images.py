"""Write evidence frame images to disk, and delete them when retention ends.

The database (:mod:`tennis_backend.storage.evidence_store`) holds *what was
detected* per frame. The images the expert actually looks at live here, one
JPEG per frame plus the ball close-up, under::

    <public root>/projects/<task>/v<n>/evidence/frames/f000123.jpg
    <public root>/projects/<task>/v<n>/evidence/frames/f000123-zoom.jpg

Only retained ranges are written: a technical-diagnosis run keeps 3-4 actions,
so writing every analysed frame would multiply the storage the user was asked
to approve. Everything outside the retained ranges is dropped on disk too,
which keeps the promise "the storage estimate you approved is what lands".

Deletion is by whole task version (:func:`delete_version_images`) or by age
(:func:`delete_expired_images`), and is called by the same periodic job that
purges the database rows; a missing file is not an error (a manual clean-up
must not break the job).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import cv2

from tennis_backend.evidence.annotate import Overlay, render_cell
from tennis_backend.storage.evidence_store import EvidenceFrame

#: Sub-directory of a task version's evidence folder holding frame images.
FRAMES_DIR = "frames"

#: Suffix of the ball close-up next to each whole frame.
ZOOM_SUFFIX = "-zoom"


@dataclass(frozen=True)
class FrameImage:
    """One written frame: relative paths under the public root."""

    frame: int
    image: str
    zoom: str | None


def frames_dir(relative_evidence_dir: str) -> str:
    """Relative directory of the frame images of one task version."""
    return f"{relative_evidence_dir.rstrip('/')}/{FRAMES_DIR}"


def _name(frame: int, *, zoom: bool = False) -> str:
    return f"f{frame:06d}{ZOOM_SUFFIX if zoom else ''}.jpg"


def overlay_of(item: EvidenceFrame, *, far: bool = False, ball_dashed: bool = False) -> Overlay:
    """The drawing instruction for one stored frame (no new detection here)."""
    return Overlay(
        frame=item.frame,
        time_s=item.time_s,
        ball=item.ball,
        ball_confidence=item.ball_confidence,
        ball_dashed=ball_dashed,
        racket=item.racket,
        racket_confidence=item.racket_confidence,
        frames_to_anchor=item.frames_to_anchor,
    )


def write_frames(
    video: Path,
    root: Path,
    relative_evidence_dir: str,
    frames: list[EvidenceFrame],
    *,
    far: bool = False,
    cancel: Any = None,
) -> tuple[FrameImage, ...]:
    """Decode ``video`` once and write the images of ``frames`` (whole + close-up).

    Reads sequentially and renders only the requested frame numbers, so a
    long clip costs one pass. A frame that cannot be decoded is skipped and
    left out of the result rather than written blank.
    """
    wanted = {item.frame: item for item in frames}
    if not wanted:
        return ()
    directory = root / frames_dir(relative_evidence_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[FrameImage] = []
    last = max(wanted)
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        return ()
    try:
        index = 0
        while index <= last:
            ok, image = capture.read()
            if not ok:
                break
            item = wanted.get(index)
            index += 1
            if item is None:
                continue
            if cancel is not None:
                cancel.check_point()
            image_bytes, zoom_bytes = render_cell(image, overlay_of(item, far=far), far=far)
            (directory / _name(item.frame)).write_bytes(image_bytes)
            zoom_relative: str | None = None
            if zoom_bytes is not None:
                (directory / _name(item.frame, zoom=True)).write_bytes(zoom_bytes)
                zoom_relative = (
                    f"{frames_dir(relative_evidence_dir)}/{_name(item.frame, zoom=True)}"
                )
            written.append(
                FrameImage(
                    frame=item.frame,
                    image=f"{frames_dir(relative_evidence_dir)}/{_name(item.frame)}",
                    zoom=zoom_relative,
                )
            )
    finally:
        capture.release()
    return tuple(written)


def retained_only(
    frames: list[EvidenceFrame], ranges: list[tuple[int, int]]
) -> list[EvidenceFrame]:
    """The frames inside any retained range (a kept range is always inclusive)."""
    kept = [item for item in frames if any(lo <= item.frame <= hi for lo, hi in ranges)]
    return kept


def delete_version_images(root: Path, relative_evidence_dir: str) -> int:
    """Remove every image of one task version; returns how many files went."""
    directory = root / frames_dir(relative_evidence_dir)
    if not directory.is_dir():
        return 0
    count = sum(1 for path in directory.iterdir() if path.is_file())
    shutil.rmtree(directory, ignore_errors=True)
    return count


def delete_expired_images(root: Path, projects: str, *, now: datetime, retention: timedelta) -> int:
    """Remove frame-image folders older than ``retention`` (mtime based).

    Used as a backstop for folders whose database rows are already gone; the
    authoritative expiry is the upload time recorded with the frames, so this
    only cleans up orphans. Measured against the folder's mtime because each
    write touches it.
    """
    base = root / projects
    if not base.is_dir():
        return 0
    cutoff = now - retention
    removed = 0
    for candidate in base.glob("*/v*/evidence/frames"):
        if not candidate.is_dir():
            continue
        stamp = datetime.fromtimestamp(candidate.stat().st_mtime)
        if stamp >= cutoff:
            continue
        removed += sum(1 for path in candidate.iterdir() if path.is_file())
        shutil.rmtree(candidate, ignore_errors=True)
    return removed


__all__ = [
    "FRAMES_DIR",
    "ZOOM_SUFFIX",
    "FrameImage",
    "delete_expired_images",
    "delete_version_images",
    "frames_dir",
    "overlay_of",
    "retained_only",
    "write_frames",
]
