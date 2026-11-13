"""Phase-4 render tests: measured pose overlay + wrist trajectory clips.

Synthetic 4-frame video + hand-placed keypoints with known geometry:
- frames with valid keypoints draw the skeleton links (FR-POSE-001);
- the gap frame (no pose record) draws NOTHING — no interpolation across
  missing frames (FR-POSE-002), verified at pixel level;
- the trajectory clip is labeled with the BODY PART and reports its
  missing segments (FR-POSE-003);
- every resource is simulation=False (measured pose on measured video).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from tennis_backend.domain.models import DomainViolation
from tennis_backend.evidence.render import (
    _original_stamp_text,
    _stamp_layout,
    render_skeleton_overlay,
    render_wrist_trajectory,
)

GREEN = (60, 220, 60)


def make_video(tmp_path: Path, name: str = "clip.mp4", frames: int = 4) -> Path:
    video = tmp_path / name
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (160, 120))
    for _ in range(frames):
        writer.write(np.zeros((120, 160, 3), dtype=np.uint8))
    writer.release()
    return video


def spec() -> dict[str, Any]:
    return {
        "name": "coco133",
        "keypoints": [],
        "skeleton": [
            {"index": 0, "from": 5, "to": 6},  # shoulders
            {"index": 1, "from": 6, "to": 8},  # right arm
        ],
    }


def record(frame: int, kpts: dict[int, tuple[int, int]] | None) -> dict[str, Any]:
    if kpts is None:
        return {
            "frame": frame,
            "pts_s": frame / 10.0,
            "subject_box": None,
            "keypoints": None,
            "valid": None,
            "subject_overlap": False,
            "failure": "no_subject_box",
        }
    keypoints = [[0.0, 0.0, 0.0] for _ in range(11)]
    valid = [False] * 11
    for index, (x, y) in kpts.items():
        keypoints[index] = [float(x), float(y), 0.9]
        valid[index] = True
    return {
        "frame": frame,
        "pts_s": frame / 10.0,
        "subject_box": [20, 20, 120, 110],
        "keypoints": keypoints,
        "valid": valid,
        "subject_overlap": True,
        "failure": None,
    }


POSED = {
    0: {5: (40, 30), 6: (120, 30), 8: (140, 80), 10: (150, 95)},
    1: {5: (50, 30), 6: (130, 30), 8: (150, 80), 10: (152, 95)},
    2: None,  # the gap frame: nothing drawn
    3: {5: (70, 30), 6: (150, 30), 8: (150, 90), 10: (154, 95)},
}


def test_overlay_draws_links_only_where_both_endpoints_valid(
    tmp_path: Path,
) -> None:
    video = make_video(tmp_path)
    outcome = render_skeleton_overlay(
        source_video=video,
        pose_records=[record(i, POSED[i]) for i in range(4)],
        skeleton_spec=spec(),
        root=tmp_path,
        relative_dir="out",
        task_id="t1",
        version=1,
    )
    resource = outcome.result["resources"][0]
    assert resource["simulation"] is False
    assert resource["kind"] == "evidence_clip"
    assert outcome.result["simulation"] is False
    rendered = cv2.VideoCapture(str(tmp_path / "out" / "pose-overlay-t1-v1.mp4"))
    ok, frame = rendered.read()
    assert ok
    # frame 0: skeleton pixels are drawn (green lines on black)
    green = (frame[:, :, 1].astype(int) - frame[:, :, 0] > 100) & (
        frame[:, :, 1].astype(int) - frame[:, :, 2] > 100
    )
    assert green.any()
    rendered.release()


def test_gap_frame_draws_nothing(tmp_path: Path) -> None:
    """FR-POSE-002: the frame without a pose record stays clean — the
    overlay must not carry the previous skeleton over the gap."""
    video = make_video(tmp_path)
    render_skeleton_overlay(
        source_video=video,
        pose_records=[record(i, POSED[i]) for i in range(4)],
        skeleton_spec=spec(),
        root=tmp_path,
        relative_dir="out",
        task_id="t1",
        version=1,
    )
    capture = cv2.VideoCapture(str(tmp_path / "out" / "pose-overlay-t1-v1.mp4"))
    frames: list[np.ndarray] = []
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        frames.append(frame)
    capture.release()
    assert len(frames) == 4
    # frame 2 (gap): no green skeleton pixels anywhere except the stamp
    gap = frames[2]
    green_mask = (gap[:, :, 1].astype(int) - gap[:, :, 0] > 100) & (
        gap[:, :, 1].astype(int) - gap[:, :, 2] > 100
    )
    assert not green_mask.any()
    # frames 0 and 3 DO carry green skeleton pixels
    for index in (0, 3):
        frame = frames[index]
        green = (frame[:, :, 1].astype(int) - frame[:, :, 0] > 100) & (
            frame[:, :, 1].astype(int) - frame[:, :, 2] > 100
        )
        assert green.any(), f"frame {index} should draw the skeleton"


def test_slow_motion_stretches_and_renames(tmp_path: Path) -> None:
    video = make_video(tmp_path)
    outcome = render_skeleton_overlay(
        source_video=video,
        pose_records=[record(i, POSED[i]) for i in range(4)],
        skeleton_spec=spec(),
        root=tmp_path,
        relative_dir="out",
        task_id="t1",
        version=1,
        slow_factor=2.0,
    )
    assert outcome.result["resources"][0]["path"].endswith("pose-overlay-t1-v1-slow2.mp4")
    capture = cv2.VideoCapture(str(tmp_path / "out" / "pose-overlay-t1-v1-slow2.mp4"))
    count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = capture.get(cv2.CAP_PROP_FPS)
    capture.release()
    # setpts stretches TIMESTAMPS (same 4 frames, ~half the fps): playback is
    # 2x slower with no fabricated interpolation
    assert count == 4
    assert fps == pytest.approx(5.0, rel=0.2)


def test_wrist_trajectory_labels_part_and_reports_gaps(tmp_path: Path) -> None:
    """FR-POSE-003: the body-part label and the gap break are burned into
    the FRAMES (the wire resource carries only the frozen fields)."""
    video = make_video(tmp_path)
    outcome = render_wrist_trajectory(
        source_video=video,
        pose_records=[record(i, POSED[i]) for i in range(4)],
        keypoint_index=10,
        part="wrist",
        root=tmp_path,
        relative_dir="out",
        task_id="t1",
        version=1,
        side="right",
    )
    resource = outcome.result["resources"][0]
    # the wire resource carries the frozen fields only
    assert resource["simulation"] is False
    assert resource["kind"] == "trajectory"
    assert (tmp_path / "out" / "traj-t1-v1-wrist-right.mp4").exists()
    # the stamp labels the body part on every frame; the polyline carries
    # the same color — verify the label is actually present on frame 0
    capture = cv2.VideoCapture(str(tmp_path / "out" / "traj-t1-v1-wrist-right.mp4"))
    ok, frame = capture.read()
    capture.release()
    assert ok
    stamp_band = frame[100:, :]  # bottom band where the stamp is drawn
    stamped = (
        (stamp_band[:, :, 0].astype(int) > 180)  # BGR blue
        & (stamp_band[:, :, 1].astype(int) > 140)
        & (stamp_band[:, :, 2].astype(int) < 120)
    )
    assert stamped.any()


def test_missing_video_refused(tmp_path: Path) -> None:
    with pytest.raises(DomainViolation) as error:
        render_skeleton_overlay(
            source_video=tmp_path / "nope.mp4",
            pose_records=[record(0, POSED[0])],
            skeleton_spec=spec(),
            root=tmp_path,
            relative_dir="out",
            task_id="t1",
            version=1,
        )
    assert error.value.code == "DOMAIN_RENDER_SOURCE_MISSING"


def test_no_records_refused_with_stable_code(tmp_path: Path) -> None:
    video = make_video(tmp_path)
    for function in (render_skeleton_overlay, render_wrist_trajectory):
        kwargs: dict[str, Any] = {
            "source_video": video,
            "pose_records": [],
            "root": tmp_path,
            "relative_dir": "out",
            "task_id": "t1",
            "version": 1,
        }
        if function is render_skeleton_overlay:
            kwargs["skeleton_spec"] = spec()
        else:
            kwargs |= {"keypoint_index": 10, "part": "wrist"}
        with pytest.raises(DomainViolation) as error:
            function(**kwargs)
        assert error.value.code == "DOMAIN_POSE_OVERLAY_NO_RECORDS"


def test_out_of_span_frames_carry_no_original_time_stamp() -> None:
    """Phase4 review B2 regression: frames beyond the mapping span must NOT
    fall back to the last measured point's timestamp — an invented original
    time on a simulation=false artifact is worse than no stamp. The stamp
    decision is a pure function (mp4 compression smears thin stamp text, so
    it is asserted directly, not via pixels)."""
    mapping = [
        {"normalized_s": 0.0, "original_s": 1.0},
        {"normalized_s": 4.0, "original_s": 5.0},
    ]
    # inside the span: the mapped original time
    assert _original_stamp_text(mapping, 0.0) == "t=1.000s original"
    assert _original_stamp_text(mapping, 2.0) == "t=3.000s original"
    assert _original_stamp_text(mapping, 4.0) == "t=5.000s original"
    # beyond the span: no stamp, never the last point's time
    assert _original_stamp_text(mapping, 4.5) is None
    assert _original_stamp_text(mapping, 100.0) is None
    assert _original_stamp_text([], 0.0) is None


def test_stamp_layout_adapts_to_narrow_frames() -> None:
    """Workbench acceptance WB4 item 1: long stamps must fit the frame —
    on a 544px portrait frame the full trajectory label was cut off."""
    import cv2

    long_text = "TRAJECTORY WRIST RIGHT (BODY KPT)"
    scale, x = _stamp_layout(long_text, 544, "left")
    (text_width, _), _ = cv2.getTextSize(long_text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    assert text_width <= 544 - 8, f"text width {text_width} exceeds the frame"
    assert x == 4
    # right position right-aligns within the frame
    scale_r, x_r = _stamp_layout("t=1.234s original", 544, "right")
    (width_r, _), _ = cv2.getTextSize("t=1.234s original", cv2.FONT_HERSHEY_SIMPLEX, scale_r, 1)
    assert x_r + width_r <= 544 - 4
    assert x_r > 4  # actually offset from the left edge
    # extreme case: a very narrow frame still shrinks to the floor scale
    scale_n, _ = _stamp_layout(long_text, 120, "left")
    assert scale_n == pytest.approx(0.3, abs=0.03)


# ---------------------------------------------------------------------------
# Swing contact sheet
# ---------------------------------------------------------------------------


def _sheet_cells(frames: list[int]) -> list[dict[str, Any]]:
    return [{"frame": f, "label": f"cell {i}"} for i, f in enumerate(frames)]


def _make_long_video(tmp_path: Path, frames: int = 12) -> Path:
    return make_video(tmp_path, "sheet.mp4", frames=frames)


def test_contact_sheet_is_one_png_with_every_requested_cell(tmp_path: Path) -> None:
    import cv2

    from tennis_backend.evidence.render import render_contact_sheet

    video = _make_long_video(tmp_path)
    records = [record(i, POSED[i % 4] if i % 4 != 2 else POSED[0]) for i in range(12)]
    outcome = render_contact_sheet(
        source_video=video,
        pose_records=records,
        skeleton_spec=spec(),
        cells=_sheet_cells([0, 3, 5, 7, 9, 11]),
        root=tmp_path,
        relative_dir="out",
        sheet_id="sheet-t1-v1-swing-0",
    )
    resource = outcome.result["resources"][0]
    assert resource["mime"] == "image/png" and resource["kind"] == "evidence_frame"
    assert resource["simulation"] is False and outcome.result["simulation"] is False
    image = cv2.imread(str(tmp_path / "out" / "sheet-t1-v1-swing-0.png"))
    assert image is not None
    # 6 cells, 3 columns → 2 rows of a 160x120 source
    assert image.shape[1] == 3 * 160 and image.shape[0] == 2 * 120


def test_contact_sheet_draws_skeleton_only_where_pose_exists(tmp_path: Path) -> None:
    """A cell whose frame has a pose shows green skeleton pixels; a cell
    without a record shows none (a gap is shown, never filled)."""
    import cv2

    from tennis_backend.evidence.render import render_contact_sheet

    video = _make_long_video(tmp_path)
    records = [record(0, POSED[0]), record(1, None)]  # frame 1 = gap
    render_contact_sheet(
        source_video=video,
        pose_records=records,
        skeleton_spec=spec(),
        cells=_sheet_cells([0, 1]),
        root=tmp_path,
        relative_dir="out",
        sheet_id="sheet-gap",
    )
    image = cv2.imread(str(tmp_path / "out" / "sheet-gap.png"))
    left, right = image[:, :160], image[:, 160:]

    def green(tile: Any) -> int:
        return int(
            (
                (tile[:, :, 1].astype(int) - tile[:, :, 0] > 100)
                & (tile[:, :, 1].astype(int) - tile[:, :, 2] > 100)
            ).sum()
        )

    assert green(left) > 0  # frame 0 drew its skeleton
    assert green(right) == 0  # the gap frame drew nothing


def test_contact_sheet_width_is_capped(tmp_path: Path) -> None:
    import cv2

    from tennis_backend.evidence.render import SHEET_MAX_WIDTH, render_contact_sheet

    wide = tmp_path / "wide.mp4"
    writer = cv2.VideoWriter(str(wide), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (1080, 1920))
    for _ in range(4):
        writer.write(np.zeros((1920, 1080, 3), dtype=np.uint8))
    writer.release()
    render_contact_sheet(
        source_video=wide,
        pose_records=[],
        skeleton_spec=spec(),
        cells=_sheet_cells([0, 1, 2]),
        root=tmp_path,
        relative_dir="out",
        sheet_id="sheet-wide",
    )
    image = cv2.imread(str(tmp_path / "out" / "sheet-wide.png"))
    assert image.shape[1] <= SHEET_MAX_WIDTH  # 3 × 1080 would be 3240 px otherwise


def test_contact_sheet_refuses_empty_input(tmp_path: Path) -> None:
    from tennis_backend.evidence.render import render_contact_sheet

    with pytest.raises(DomainViolation) as error:
        render_contact_sheet(
            source_video=_make_long_video(tmp_path),
            pose_records=[],
            skeleton_spec=spec(),
            cells=[],
            root=tmp_path,
            relative_dir="out",
            sheet_id="sheet-empty",
        )
    assert error.value.code == "DOMAIN_RENDER_SHEET_NO_CELLS"
    with pytest.raises(DomainViolation) as error:
        render_contact_sheet(
            source_video=tmp_path / "nope.mp4",
            pose_records=[],
            skeleton_spec=spec(),
            cells=_sheet_cells([0]),
            root=tmp_path,
            relative_dir="out",
            sheet_id="sheet-nosrc",
        )
    assert error.value.code == "DOMAIN_RENDER_SOURCE_MISSING"
