"""Ball and racket detection over an approved video (COCO classes).

RF-DETR nano (the model the backend already uses for people) also knows the
COCO classes ``sports ball`` and ``tennis racket``. This module runs it per
frame and records the detections exactly as seen: no tracking, no filling of
missed frames, no judgement about whether a pair is a hit (that is
:mod:`tennis_backend.analysis.hits`, and ultimately the expert).

Measured limits carried with the output (B0 experiment record):

- near-camera phone video: usable; far-camera or broadcast video: the ball is
  3-6 px and this detector finds almost none of it (a different detector,
  WASB, is needed there and is not part of this module);
- a fluorescent-green racket head is sometimes detected as a ball;
- the ball held in the hand and background discs are detected as balls.

The detector is injected so tests drive known boxes; the default wraps
RF-DETR nano and needs the ``real-pose`` extra. Inference runs on the
caller's thread (a worker job under the NDJSON transport) and honours the
cancellation token between frames.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import cv2

from tennis_backend.application.worker import CancellationToken
from tennis_backend.domain.models import DomainViolation
from tennis_backend.media.weights import DETECTOR_ENV, detector_weights, require

_VIDEO_MISSING = "BALL_VIDEO_MISSING"
_WEIGHTS_MISSING = "BALL_WEIGHTS_MISSING"
_DEPS_MISSING = "REAL_POSE_DEPS_MISSING"

#: Seconds of footage analysed (FR-002: full video up to 180 s).
DEFAULT_MAX_SECONDS = 180.0

#: Detector floor; the anchor logic applies its own higher thresholds
#: (ball 0.4, racket 0.3) on top of this.
DETECTION_FLOOR = 0.15

BALL_LABEL = "sports ball"
RACKET_LABEL = "tennis racket"


class BallRacketDetector(Protocol):
    """One-frame ball/racket detector, pixel coordinates."""

    def detect(self, frame_rgb: Any) -> dict[str, list[list[float]]]:
        """Return ``{"ball": [[x1,y1,x2,y2,conf]...], "racket": [...]}``."""


class RFDetrBallRacketDetector:
    """RF-DETR nano restricted to the ball and racket classes (lazy load)."""

    def __init__(self, threshold: float = DETECTION_FLOOR) -> None:
        self._threshold = threshold
        self._model: Any = None

    def _load(self) -> Any:
        try:
            import rfdetr
        except ImportError as error:
            raise DomainViolation(
                _DEPS_MISSING,
                "real-pose dependencies are not installed; run uv sync --extra real-pose",
            ) from error
        # RF-DETR's INFO handler targets stdout, reserved for NDJSON.
        logger = logging.getLogger("rf-detr")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.addHandler(logging.StreamHandler(sys.stderr))
        logger.propagate = False
        return rfdetr.RFDETRNano(
            pretrain_weights=str(
                require(
                    detector_weights(),
                    code=_WEIGHTS_MISSING,
                    what="RF-DETR nano",
                    env=DETECTOR_ENV,
                )
            )
        )

    def detect(self, frame_rgb: Any) -> dict[str, list[list[float]]]:
        if self._model is None:
            self._model = self._load()
        result = self._model.predict(frame_rgb, threshold=self._threshold)
        names = result.data.get("class_name") if hasattr(result, "data") else None
        out: dict[str, list[list[float]]] = {"ball": [], "racket": []}
        for i in range(len(result.xyxy)):
            label = str(names[i]) if names is not None else str(int(result.class_id[i]))
            if label not in (BALL_LABEL, RACKET_LABEL):
                continue
            box = [float(v) for v in result.xyxy[i]] + [float(result.confidence[i])]
            out["ball" if label == BALL_LABEL else "racket"].append(box)
        return out


@dataclass
class BallRacketScan:
    """Per-frame detections of one video, kept exactly as observed."""

    fps: float
    width: int
    height: int
    frames: dict[int, dict[str, list[list[float]]]] = field(default_factory=dict)
    truncated: bool = False

    @property
    def frame_count(self) -> int:
        return len(self.frames)


def detect_video(
    video: Path,
    detector: BallRacketDetector,
    *,
    cancel: CancellationToken | None = None,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    stride: int = 1,
) -> BallRacketScan:
    """Run ``detector`` on every ``stride``-th frame of ``video``.

    ``stride`` stays 1 for evidence work (the expert gets whole intervals);
    a larger value exists only for cheap storage estimates. A video longer
    than ``max_seconds`` is cut off and reported ``truncated`` rather than
    silently shortened.
    """
    if stride < 1:
        raise DomainViolation("BALL_STRIDE", "stride must be at least 1")
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise DomainViolation(_VIDEO_MISSING, f"video {video.name} cannot be opened")
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS)) or 30.0
        scan = BallRacketScan(
            fps=fps,
            width=int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
            height=int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )
        limit = int(max_seconds * fps)
        index = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if index >= limit:
                scan.truncated = True
                break
            if cancel is not None:
                cancel.check_point()
            if index % stride == 0:
                scan.frames[index] = detector.detect(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            index += 1
        return scan
    finally:
        capture.release()
