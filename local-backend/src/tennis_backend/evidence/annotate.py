"""Annotated evidence images: boxes, a legend, and ball close-ups.

The expert (a multimodal model) receives whole frames, never crops, so a ball
cut off by a crop can not hide. Boxes are drawn on a *copy*; the ball close-up
is cut from the **unannotated** original, otherwise the box edge would sit on
the ball it is meant to show.

Drawing rules (user decisions 2026-11-14/16):

- ball = green box, racket = blue box;
- near camera (COCO): confidence >= 0.50 solid, below that dashed;
- far camera (WASB, no confidence): ``detected`` solid, every other source
  dashed, and the ball box is a fixed 32 px square around the ball centre
  (the ball itself is 3-6 px);
- every cell carries frame, time, both confidences and the distance to the
  nearest anchor; the legend states that no box does not mean no ball and that
  a confidence is not "this is a hit".

Text is drawn with OpenCV's built-in font, which has no CJK glyphs, so the
pixels carry an English legend; the same legend in Chinese is returned as text
(:data:`LEGEND_ZH`) alongside the images. No new dependency (Pillow is not a
declared dependency of this project).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

GREEN = (0, 200, 0)
BLUE = (255, 120, 0)  # BGR: blue-orange reads as "blue" against court colours
SOLID_CONFIDENCE = 0.50
FAR_BOX_PX = 32
ZOOM_HALF = 20
ZOOM_OUT = 160
IMAGE_HEIGHT = 720
JPEG_QUALITY = 80

LEGEND_EN = (
    "Green box = ball, blue box = racket. Solid: confidence >= 0.50 (far view: detected);",
    "dashed: lower confidence (far view: recovered / lowthr / estimated).",
    "No box does NOT mean there is no ball (missed detection).",
    "Confidence is NOT 'this is a hit'. Anchor = system suggestion, not truth.",
    "The boxes are system detections, not ground truth: please verify.",
)
LEGEND_ZH = (
    "绿框=球，蓝框=球拍。实线：置信度≥0.50（远景：detected）；虚线：置信度较低（远景：recovered/lowthr/estimated）。",
    "无框不等于画面里没有球（可能漏检）。",
    "置信度不代表这一帧是击球；锚点只是系统建议，不是真值。",
    "框是系统检出，不是真值，请复核。",
)


@dataclass(frozen=True)
class Overlay:
    """What to draw on one frame; boxes are ``[x1, y1, x2, y2]``."""

    frame: int
    time_s: float
    ball: list[float] | None = None
    ball_confidence: float | None = None
    ball_dashed: bool = False
    racket: list[float] | None = None
    racket_confidence: float | None = None
    frames_to_anchor: int | None = None


def _dashed_rect(
    image: np.ndarray,
    p1: tuple[int, int],
    p2: tuple[int, int],
    color: Any,
    thickness: int,
    dash: int = 8,
) -> None:
    x1, y1 = p1
    x2, y2 = p2
    # a small box must still show gaps: scale the dash to the shorter side
    dash = max(2, min(dash, min(abs(x2 - x1), abs(y2 - y1)) // 5))
    for (ax, ay), (bx, by) in (
        ((x1, y1), (x2, y1)),
        ((x2, y1), (x2, y2)),
        ((x2, y2), (x1, y2)),
        ((x1, y2), (x1, y1)),
    ):
        length = max(abs(bx - ax), abs(by - ay))
        steps = max(length // dash, 1)
        for i in range(0, steps, 2):
            start = (round(ax + (bx - ax) * i / steps), round(ay + (by - ay) * i / steps))
            end = (round(ax + (bx - ax) * (i + 1) / steps), round(ay + (by - ay) * (i + 1) / steps))
            cv2.line(image, start, end, color, thickness)


def _box(image: np.ndarray, box: list[float], color: Any, dashed: bool, thickness: int) -> None:
    p1, p2 = (round(box[0]), round(box[1])), (round(box[2]), round(box[3]))
    if dashed:
        _dashed_rect(image, p1, p2, color, thickness)
    else:
        cv2.rectangle(image, p1, p2, color, thickness)


def ball_box_for(overlay: Overlay, *, far: bool) -> list[float] | None:
    """Far view: a fixed square on the ball centre; near view: the detection."""
    if overlay.ball is None:
        return None
    if not far:
        return overlay.ball
    cx, cy = (overlay.ball[0] + overlay.ball[2]) / 2, (overlay.ball[1] + overlay.ball[3]) / 2
    half = FAR_BOX_PX / 2
    return [cx - half, cy - half, cx + half, cy + half]


def annotate_frame(image_bgr: np.ndarray, overlay: Overlay, *, far: bool) -> np.ndarray:
    """A copy of ``image_bgr`` with boxes and the per-cell label. Original untouched."""
    out = image_bgr.copy()
    thickness = max(2, round(min(out.shape[:2]) / 250))
    ball = ball_box_for(overlay, far=far)
    if overlay.racket is not None:
        dashed = (
            overlay.racket_confidence is not None and overlay.racket_confidence < SOLID_CONFIDENCE
        )
        _box(out, overlay.racket, BLUE, dashed, thickness)
    if ball is not None:
        dashed = overlay.ball_dashed or (
            not far
            and overlay.ball_confidence is not None
            and overlay.ball_confidence < SOLID_CONFIDENCE
        )
        _box(out, ball, GREEN, dashed, thickness)
    _label(out, overlay)
    return out


def _label(image: np.ndarray, overlay: Overlay) -> None:
    def conf(value: float | None) -> str:
        return "-" if value is None else f"{value:.2f}"

    anchor = "-" if overlay.frames_to_anchor is None else str(overlay.frames_to_anchor)
    text = (
        f"f{overlay.frame} {overlay.time_s:.2f}s ball {conf(overlay.ball_confidence)} "
        f"racket {conf(overlay.racket_confidence)} anchor {anchor}"
    )
    scale = max(0.5, image.shape[1] / 1000)
    thickness = max(1, round(scale * 2))
    (width, height), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    cv2.rectangle(
        image, (0, 0), (min(width + 12, image.shape[1]), height + baseline + 10), (0, 0, 0), -1
    )
    cv2.putText(
        image,
        text,
        (6, height + 5),
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        (0, 255, 255),
        thickness,
        cv2.LINE_AA,
    )


def zoom_on_ball(
    original_bgr: np.ndarray, overlay: Overlay, *, half: int = ZOOM_HALF, out_size: int = ZOOM_OUT
) -> np.ndarray | None:
    """Close-up around the ball centre, cut from the UNannotated frame.

    Pads with black at the border so the ball stays centred. ``None`` when no
    ball was detected in this frame (nothing to centre on).
    """
    if overlay.ball is None:
        return None
    cx = round((overlay.ball[0] + overlay.ball[2]) / 2)
    cy = round((overlay.ball[1] + overlay.ball[3]) / 2)
    padded = cv2.copyMakeBorder(original_bgr, half, half, half, half, cv2.BORDER_CONSTANT)
    crop = padded[cy : cy + 2 * half, cx : cx + 2 * half]
    return cv2.resize(crop, (out_size, out_size), interpolation=cv2.INTER_NEAREST)


def legend_panel(width: int) -> np.ndarray:
    """A dark strip carrying the English legend, ``width`` px wide."""
    scale = max(0.45, width / 1800)
    thickness = 1
    line_height = round(34 * scale / 0.45 * 0.55) + 8
    panel = np.full((line_height * len(LEGEND_EN) + 12, width, 3), 30, np.uint8)
    for i, line in enumerate(LEGEND_EN):
        cv2.putText(
            panel,
            line,
            (8, line_height * (i + 1)),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            (230, 230, 230),
            thickness,
            cv2.LINE_AA,
        )
    return panel


def scale_to_height(image: np.ndarray, height: int = IMAGE_HEIGHT) -> np.ndarray:
    if image.shape[0] == height:
        return image
    width = round(image.shape[1] * height / image.shape[0])
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def encode_jpeg(image: np.ndarray, quality: int = JPEG_QUALITY) -> bytes:
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("jpeg encoding failed")
    return bytes(buffer.tobytes())


def render_cell(
    original_bgr: np.ndarray, overlay: Overlay, *, far: bool
) -> tuple[bytes, bytes | None]:
    """One frame as the expert gets it: annotated whole frame + ball close-up (JPEG bytes).

    The close-up comes from the original, before any box is drawn, and is
    ``None`` when no ball was detected.
    """
    zoom = zoom_on_ball(original_bgr, overlay)
    annotated = scale_to_height(annotate_frame(original_bgr, overlay, far=far))
    return encode_jpeg(annotated), None if zoom is None else encode_jpeg(zoom)


def contact_sheet(cells: list[np.ndarray], columns: int = 4) -> np.ndarray:
    """Grid of annotated cells with the legend underneath (a human-readable preview)."""
    if not cells:
        raise ValueError("no cells")
    height, width = cells[0].shape[:2]
    rows = []
    for start in range(0, len(cells), columns):
        row = list(cells[start : start + columns])
        row += [np.zeros((height, width, 3), np.uint8)] * (columns - len(row))
        rows.append(np.hstack(row))
    grid = np.vstack(rows)
    return np.vstack([grid, legend_panel(grid.shape[1])])
