"""Which ball detector to run: the expert's choice, with a measured fallback.

The expert (a multimodal model looking at sample frames) names the detector:
``coco`` for near-camera video, ``wasb`` for far / mid-distance video. When it
does not, the backend asks again (the expert keeps reasoning, ReAct style);
after :data:`MAX_PROMPTS` unanswered prompts the backend picks one itself from
how large the subject is in the frame.

Fallback rule (user decision 2026-11-16): the median height of the subject's
person box divided by the frame height. At or above :data:`NEAR_HEIGHT_RATIO`
the video is near-camera -> ``coco``; below it -> ``wasb``.

Measured on the four B0 videos (median / p10 / p90 of that ratio):

====================  ======  ======  ======
video                 median  p10     p90
====================  ======  ======  ======
Flow (near)           0.727   0.390   0.851
400 (near)            0.416   0.331   0.548
broadcast (far)       0.189   0.147   0.215
395 (mid-distance)    0.157   0.149   0.163
====================  ======  ======  ======

The two groups are separated (far max 0.243, near min of p10 0.331). The
threshold sits in the gap. Limits, stated plainly: four videos, the threshold
was set from these same videos (not held out), the broadcast ratio takes the
largest person box per frame, and a close-up shot of a far-camera clip or a
very distant phone shot would break the rule. A backend fallback therefore
never counts as verified: the result is marked ``fallback`` so the expert
re-checks it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Literal

Detector = Literal["coco", "wasb"]

#: Subject box height / frame height at or above which a clip is "near camera".
NEAR_HEIGHT_RATIO = 0.28

#: Unanswered "which detector?" prompts before the backend chooses.
MAX_PROMPTS = 3

DETECTORS: tuple[Detector, ...] = ("coco", "wasb")


@dataclass(frozen=True)
class NeedDetector:
    """Ask the expert to name the detector (it keeps reasoning, then answers)."""

    prompts_used: int
    prompts_left: int
    options: tuple[Detector, ...] = DETECTORS
    guidance: str = (
        "指定 detector：近景（人物占画面高度约三分之一以上、球清晰可见）用 coco；"
        "远景或中景（人物很小、球只有几个像素）用 wasb。请查看抽样帧后填写。"
    )


@dataclass(frozen=True)
class DetectorChoice:
    """The detector to run and how it was chosen."""

    detector: Detector
    source: Literal["expert", "backend_fallback"]
    height_ratio: float | None = None
    note: str = ""

    @property
    def fallback(self) -> bool:
        return self.source == "backend_fallback"


def subject_height_ratio(heights_px: list[float], frame_height: int) -> float | None:
    """Median subject-box height over frame height; ``None`` without any box."""
    if not heights_px or frame_height <= 0:
        return None
    return statistics.median(heights_px) / frame_height


def fallback_detector(height_ratio: float | None) -> Detector:
    """The measured rule; with no subject box at all it picks ``wasb``.

    Without a subject there is no evidence the clip is near-camera, and the
    near-camera detector (COCO) was the one that produced false anchors on
    held balls and green rackets, so the more cautious choice is ``wasb``.
    """
    if height_ratio is not None and height_ratio >= NEAR_HEIGHT_RATIO:
        return "coco"
    return "wasb"


def resolve_detector(
    requested: str | None,
    *,
    prompts_used: int,
    height_ratio: float | None,
) -> DetectorChoice | NeedDetector:
    """The expert's choice, or another prompt, or (after three) the fallback."""
    if requested in DETECTORS:
        return DetectorChoice(detector=requested, source="expert")
    if requested not in (None, ""):
        # a value outside the slot is a prompt too, not a silent default
        prompts_used = max(prompts_used, 0)
    if prompts_used < MAX_PROMPTS:
        return NeedDetector(
            prompts_used=prompts_used + 1, prompts_left=MAX_PROMPTS - prompts_used - 1
        )
    chosen = fallback_detector(height_ratio)
    ratio_text = "无人体框" if height_ratio is None else f"人体框高占画面 {height_ratio:.2f}"
    return DetectorChoice(
        detector=chosen,
        source="backend_fallback",
        height_ratio=height_ratio,
        note=(
            f"专家 {MAX_PROMPTS} 次未指定检测器，后端按{ratio_text}"
            f"（阈值 {NEAR_HEIGHT_RATIO}）选择 {chosen}；该规则只在 4 个视频上测过，请复核"
        ),
    )
