"""Swing segmentation and temporal summary tests.

Synthetic pose records with KNOWN geometry:
- the right wrist sweeps in three bursts (frames ~12, ~36, ~60) separated by
  stillness → exactly three swings, peaks at the known frames;
- the shoulder line starts turning BEFORE the hip line → the rotation order
  must say shoulder first;
- gaps (frames without keypoints) stay gaps.
"""

from __future__ import annotations

import math
from typing import Any

from tennis_backend.analysis.swings import (
    MAX_SWINGS_PER_SEGMENT,
    derive_swings,
    split_swings,
    wrist_speed_series,
)

FPS = 25.0


def frame_record(
    frame: int,
    *,
    wrist: tuple[float, float] | None,
    shoulder_turn_deg: float = 0.0,
    hip_turn_deg: float = 0.0,
    box_h: float = 200.0,
) -> dict[str, Any]:
    """A pose record whose shoulders/hips are foreshortened by a trunk turn.

    Turning the trunk from square-on (0°) to side-on (90°) shrinks the
    left→right x-extent by cos(turn) — exactly what a back-view camera sees.
    """
    keypoints = [[0.0, 0.0, 0.9] for _ in range(133)]
    valid = [False] * 133
    cx = 100.0

    def put(index: int, x: float, y: float) -> None:
        keypoints[index] = [x, y, 0.9]
        valid[index] = True

    half_shoulder = 30.0 * math.cos(math.radians(shoulder_turn_deg))
    half_hip = 20.0 * math.cos(math.radians(hip_turn_deg))
    put(5, cx - half_shoulder, 80.0)
    put(6, cx + half_shoulder, 80.0)
    put(11, cx - half_hip, 150.0)
    put(12, cx + half_hip, 150.0)
    if wrist is not None:
        put(10, *wrist)
    return {
        "frame": frame,
        "pts_s": round(frame / FPS, 4),
        "subject_box": [60, 20, 140, 20 + box_h],
        "keypoints": keypoints,
        "valid": valid,
        "subject_overlap": True,
        "failure": None,
    }


def three_swing_records(total: int = 80) -> list[dict[str, Any]]:
    """Wrist still except three fast bursts centred on frames 12, 36, 60.

    Every motion is a smooth hump (a real wrist never teleports): each burst
    swings forward, then a gentler recovery swings back, so the wrist stays in
    frame without any single-frame jump.
    """
    records = []
    x = 100.0
    for f in range(total):
        forward = sum(14.0 * math.exp(-((f - c) ** 2) / 6.0) for c in (12, 36, 60))
        # slow recovery hump (≈ 1/6 of the forward speed) a few frames later
        back = sum(-5.0 * math.exp(-((f - (c + 8)) ** 2) / 18.0) for c in (12, 36, 60))
        x += forward + back
        records.append(frame_record(f, wrist=(x, 100.0)))
    return records


def test_three_bursts_split_into_three_swings_at_known_peaks() -> None:
    records = three_swing_records()
    windows, wrist = split_swings(records, FPS)
    assert wrist == "right"
    assert len(windows) == 3
    peaks = [w.peak_frame for w in windows]
    for found, expected in zip(peaks, (12, 36, 60), strict=True):
        assert abs(found - expected) <= 2, f"peak {found} far from {expected}"
    # windows do not overlap and stay inside the segment
    for a, b in zip(windows, windows[1:], strict=False):
        assert a.end_frame < b.start_frame
    assert windows[0].start_frame == 0 and windows[-1].end_frame == 79


def test_drive_starts_before_peak() -> None:
    windows, _ = split_swings(three_swing_records(), FPS)
    for window in windows:
        assert window.start_frame <= window.drive_start_frame < window.peak_frame


def test_idle_jitter_is_not_promoted_to_a_swing() -> None:
    # a still wrist with tiny noise: no prominent peak → no swings
    records = [frame_record(f, wrist=(100.0 + (0.3 if f % 2 else 0.0), 100.0)) for f in range(60)]
    windows, _ = split_swings(records, FPS)
    assert windows == []


def test_too_short_segment_gives_no_swings() -> None:
    records = [frame_record(f, wrist=(100.0 + f * 20, 100.0)) for f in range(4)]
    windows, _ = split_swings(records, FPS)
    assert windows == []


def test_gap_breaks_the_speed_series_instead_of_bridging() -> None:
    records = three_swing_records(40)
    for gap in (10, 11, 12, 13):  # remove the first burst's keypoints entirely
        records[gap] = frame_record(gap, wrist=None)
    speeds, _ = wrist_speed_series(records, FPS)
    # no speed is computed INTO or OUT OF a gap: a jump across it is never invented
    assert all(speeds[i] is None for i in (10, 11, 12, 13, 14))


def test_swing_cap_per_segment() -> None:
    records = []
    x = 100.0
    for f in range(400):
        forward = sum(14.0 * math.exp(-((f - c) ** 2) / 6.0) for c in range(10, 390, 20))
        back = sum(-6.0 * math.exp(-((f - (c + 9)) ** 2) / 18.0) for c in range(10, 390, 20))
        x += forward + back
        records.append(frame_record(f, wrist=(x, 100.0)))
    windows, _ = split_swings(records, FPS)
    assert 0 < len(windows) <= MAX_SWINGS_PER_SEGMENT


def angles_for(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"frame": r["frame"], "pts_s": r["pts_s"], "angles": {}} for r in records]


def one_swing_records(shoulder_start: int = 18, hip_start: int = 28) -> list[dict[str, Any]]:
    """ONE smooth swing (a single wrist-speed hump on frame 30). The shoulder
    line turns square→side-on first (from frame 18), the hip line later."""
    records = []
    x = 100.0
    for f in range(60):
        x += 14.0 * math.exp(-((f - 30) ** 2) / 30.0)
        shoulder = max(0.0, min(80.0, (f - shoulder_start) * 5.0))
        hip = max(0.0, min(70.0, (f - hip_start) * 4.0))
        records.append(
            frame_record(f, wrist=(x, 100.0), shoulder_turn_deg=shoulder, hip_turn_deg=hip)
        )
    return records


def test_rotation_order_reports_shoulder_before_hip() -> None:
    records = one_swing_records()
    derived = derive_swings(
        records, angles_for(records), [{"start_frame": 0, "end_frame": 59}], fps=FPS
    )
    assert derived["swing_count"] == 1
    swing = derived["swings"][0]
    order = [item["line"] for item in swing["rotation_order"]]
    assert order == ["shoulder_turn_deg", "hip_turn_deg"]
    shoulder = swing["angles"]["shoulder_turn_deg"]
    hip = swing["angles"]["hip_turn_deg"]
    assert shoulder["max_rate_t_s"] <= hip["max_rate_t_s"]
    assert shoulder["max"] > 60 and hip["max"] > 50  # both really turned toward side-on


def test_hip_first_is_reported_as_hip_first() -> None:
    """The proxy must not always say shoulder-first: swap the timing."""
    records = one_swing_records(shoulder_start=30, hip_start=14)
    derived = derive_swings(
        records, angles_for(records), [{"start_frame": 0, "end_frame": 59}], fps=FPS
    )
    order = [item["line"] for item in derived["swings"][0]["rotation_order"]]
    assert order == ["hip_turn_deg", "shoulder_turn_deg"]


def test_a_line_that_never_turns_is_left_out_of_the_rotation_order() -> None:
    """A swing in which the hips stay square must NOT list the hips in the
    rotation order: 'no rotation observed' is not 'rotated first/last'."""
    records = []
    x = 100.0
    for f in range(60):
        x += 14.0 * math.exp(-((f - 30) ** 2) / 30.0)
        shoulder = max(0.0, min(80.0, (f - 18) * 5.0))
        records.append(
            frame_record(f, wrist=(x, 100.0), shoulder_turn_deg=shoulder, hip_turn_deg=0.0)
        )
    derived = derive_swings(
        records, angles_for(records), [{"start_frame": 0, "end_frame": 59}], fps=FPS
    )
    swing = derived["swings"][0]
    assert [item["line"] for item in swing["rotation_order"]] == ["shoulder_turn_deg"]


def test_turn_proxy_is_continuous_through_side_on() -> None:
    """The reason the width proxy replaced the tilt angle: a trunk passing
    through side-on must read as smooth rotation. (A folded tilt angle jumps
    ~180° there — measured on real back-view video.)"""
    from tennis_backend.analysis.swings import _turn_series

    records = [
        frame_record(f, wrist=(100.0, 100.0), shoulder_turn_deg=min(80.0, f * 4.0))
        for f in range(30)
    ]
    series = [v for v in _turn_series(records, 5, 6) if v is not None]
    steps = [abs(b - a) for a, b in zip(series, series[1:], strict=False)]
    assert series[0] < 5 and series[-1] > 60
    assert max(steps) < 12  # a 4°/frame turn never jumps


def test_turn_proxy_is_scale_invariant() -> None:
    """Same trunk turn, subject twice as big in frame → same proxy values."""
    from tennis_backend.analysis.swings import _turn_series

    def build(scale: float) -> list[dict[str, Any]]:
        out = []
        for f in range(20):
            record = frame_record(f, wrist=(100.0, 100.0), shoulder_turn_deg=f * 4.0)
            for index in (5, 6):
                record["keypoints"][index][0] = (
                    record["keypoints"][index][0] - 100.0
                ) * scale + 100.0
            record["subject_box"] = [60, 20, 140, 20 + 200.0 * scale]
            out.append(record)
        return out

    small = _turn_series(build(1.0), 5, 6)
    large = _turn_series(build(2.0), 5, 6)
    for a, b in zip(small, large, strict=True):
        assert a is not None and b is not None and abs(a - b) < 1.5


def test_too_few_valid_frames_makes_the_turn_series_a_gap() -> None:
    from tennis_backend.analysis.swings import _turn_series

    records = [frame_record(f, wrist=(100.0, 100.0), shoulder_turn_deg=f * 5.0) for f in range(6)]
    assert _turn_series(records, 5, 6) == [None] * 6  # a reference from 6 frames is meaningless


def test_keypoint_swap_becomes_a_gap_not_a_fake_rotation() -> None:
    """A real detector error (a left/right keypoint swap) shows as an impossible
    single-frame step in the turn series: those frames become gaps — never a
    huge 'rotation' with a huge velocity."""
    from tennis_backend.analysis.swings import MAX_LINE_STEP_DEG, _drop_jumps

    steady = [10.0, 12.0, 14.0, 16.0]
    cleaned, dropped = _drop_jumps(steady + [120.0, 118.0] + [18.0, 20.0])
    assert cleaned[:4] == steady
    assert cleaned[4] is None and cleaned[5] is None
    assert cleaned[6:] == [18.0, 20.0]  # recovers once consistent again
    assert dropped == 2
    assert max(abs(b - a) for a, b in zip(steady, steady[1:], strict=False)) < MAX_LINE_STEP_DEG


def test_series_with_too_few_valid_frames_is_null_not_interpolated() -> None:
    records = three_swing_records(40)
    # wipe the shoulders on most frames: the series falls under the valid-share bar
    for record in records:
        if record["frame"] % 4:
            record["valid"][5] = False
            record["valid"][6] = False
    derived = derive_swings(
        records, angles_for(records), [{"start_frame": 0, "end_frame": 39}], fps=FPS
    )
    assert derived["swing_count"] >= 1
    for swing in derived["swings"]:
        assert swing["angles"]["shoulder_turn_deg"] is None


def test_summary_is_compact_and_carries_its_limits() -> None:
    import json

    records = three_swing_records()
    derived = derive_swings(
        records, angles_for(records), [{"start_frame": 0, "end_frame": 79}], fps=FPS
    )
    for swing in derived["swings"]:
        size = len(json.dumps(swing, ensure_ascii=False))
        assert size <= 4096, f"{swing['swing_id']} is {size} bytes, over the 4 KB budget"
        assert any("not ball contact" in text for text in swing["limitations"])
        assert [p["name"] for p in swing["phases"]] == [
            "preparation",
            "drive",
            "follow_through",
        ]
        assert swing["phases"][0]["from_s"] == 0.0
        assert swing["phases"][-1]["to_s"] == swing["duration_s"]


def test_context_frames_outside_segments_are_never_split() -> None:
    records = three_swing_records(80)
    derived = derive_swings(
        records,
        angles_for(records),
        [{"start_frame": 30, "end_frame": 79}],  # only the 2nd and 3rd burst
        fps=FPS,
    )
    for swing in derived["swings"]:
        assert 30 <= swing["frames"][0] and swing["frames"][1] <= 79


def test_hitting_side_is_undetermined_when_wrists_move_alike() -> None:
    from tennis_backend.analysis.swings import wrist_speed_series

    records = []
    for f in range(40):
        record = frame_record(f, wrist=(100.0 + f * 3, 100.0))
        # the LEFT wrist moves almost as fast as the right (real data: 6.9 vs 5.5)
        record["keypoints"][9] = [100.0 + f * 2.8, 100.0, 0.9]
        record["valid"][9] = True
        records.append(record)
    _speeds, side = wrist_speed_series(records, FPS)
    assert side == "undetermined"


def test_hitting_side_named_only_with_a_clear_margin() -> None:
    from tennis_backend.analysis.swings import wrist_speed_series

    records = []
    for f in range(40):
        record = frame_record(f, wrist=(100.0 + f * 6, 100.0))
        record["keypoints"][9] = [100.0 + f * 1.0, 100.0, 0.9]  # guiding arm barely moves
        record["valid"][9] = True
        records.append(record)
    _speeds, side = wrist_speed_series(records, FPS)
    assert side == "right"


def _bursts(centres: list[int], strengths: list[float], total: int) -> list[dict[str, Any]]:
    records = []
    x = 100.0
    for f in range(total):
        forward = sum(
            s * math.exp(-((f - c) ** 2) / 6.0) for c, s in zip(centres, strengths, strict=True)
        )
        back = sum(
            -0.35 * s * math.exp(-((f - (c + 8)) ** 2) / 18.0)
            for c, s in zip(centres, strengths, strict=True)
        )
        x += forward + back
        records.append(frame_record(f, wrist=(x, 100.0)))
    return records


def test_cap_keeps_the_strongest_swings_not_the_earliest() -> None:
    """Regression (real clip): a time-ordered cap dropped the 2nd-strongest
    swing because it came late. With 7 bursts the cap of 5 must keep the 5
    STRONGEST, wherever they are in the segment."""
    from tennis_backend.analysis.swings import MAX_SWINGS_PER_SEGMENT, split_swings_full

    centres = [10, 30, 50, 70, 90, 110, 130]
    strengths = [8.0, 8.0, 8.0, 8.0, 8.0, 20.0, 20.0]  # the two strongest come LAST
    result = split_swings_full(_bursts(centres, strengths, 150), FPS)
    kept = [w.peak_frame for w in result.windows]
    assert len(kept) == MAX_SWINGS_PER_SEGMENT
    assert any(abs(p - 110) <= 2 for p in kept) and any(abs(p - 130) <= 2 for p in kept)


def test_peaks_beyond_the_cap_are_reported_not_hidden() -> None:
    from tennis_backend.analysis.swings import split_swings_full

    centres = [10, 30, 50, 70, 90, 110, 130]
    result = split_swings_full(_bursts(centres, [10.0] * 7, 150), FPS)
    assert len(result.windows) == 5
    assert len(result.dropped_peak_frames) == 2  # more movement exists than was summarised

    records = _bursts(centres, [10.0] * 7, 150)
    derived = derive_swings(
        records, angles_for(records), [{"start_frame": 0, "end_frame": 149}], fps=FPS
    )
    assert derived["swing_count"] == 5
    assert derived["peaks_beyond_cap"][0]["segment"] == 0
    assert len(derived["peaks_beyond_cap"][0]["dropped_peak_frames"]) == 2


def test_no_dropped_peaks_when_under_the_cap() -> None:
    records = three_swing_records()
    derived = derive_swings(
        records, angles_for(records), [{"start_frame": 0, "end_frame": 79}], fps=FPS
    )
    assert derived["peaks_beyond_cap"] == []


def test_sheet_frames_follow_the_summary_and_stay_in_time_order() -> None:
    from tennis_backend.analysis.swings import sheet_frames

    records = one_swing_records()
    derived = derive_swings(
        records, angles_for(records), [{"start_frame": 0, "end_frame": 59}], fps=FPS
    )
    swing = derived["swings"][0]
    cells = sheet_frames(swing)
    frames = [c["frame"] for c in cells]
    assert frames == sorted(frames) and len(set(frames)) == len(frames)
    assert cells[0]["role"] == "start" and cells[-1]["role"] == "end"
    peak = next(c for c in cells if c["role"] == "peak")
    # the peak cell sits where the summary says the wrist-speed peak is
    expected = swing["frames"][0] + round(swing["wrist_speed_peak_t_s"] * FPS)
    assert abs(peak["frame"] - expected) <= 1
    assert all(swing["frames"][0] <= f <= swing["frames"][1] for f in frames)


def test_sheet_frames_collapse_duplicates_for_a_very_short_swing() -> None:
    from tennis_backend.analysis.swings import sheet_frames

    swing = {
        "frames": [10, 14],
        "duration_s": 0.16,
        "wrist_speed_peak_t_s": 0.08,
        "phases": [
            {"name": "preparation", "from_s": 0.0, "to_s": 0.04},
            {"name": "drive", "from_s": 0.04, "to_s": 0.08},
            {"name": "follow_through", "from_s": 0.08, "to_s": 0.16},
        ],
    }
    cells = sheet_frames(swing)
    frames = [c["frame"] for c in cells]
    assert len(cells) < 6  # fewer cells, never the same frame twice
    assert len(set(frames)) == len(frames) and frames == sorted(frames)


def test_clipped_frames_are_excluded_and_reported_in_the_swing_summary() -> None:
    records = three_swing_records()
    for record in records[:30]:  # the first third of the clip is cut off by the frame
        record["clip"] = {"clipped": True, "edges": ["left"], "keypoints_outside_image": 1}
    derived = derive_swings(
        records, angles_for(records), [{"start_frame": 0, "end_frame": 79}], fps=FPS
    )
    assert derived["swings"], "later swings must still be found"
    first = derived["swings"][0]
    assert first["clip_flag_available"] is True
    assert first["clipped_frame_share"] > 0
    assert first["valid_frame_share"] < 1.0
    assert any("cut off by the frame border" in t for t in first["limitations"])


def test_records_without_a_clip_flag_report_that_it_was_unavailable() -> None:
    records = three_swing_records()
    derived = derive_swings(
        records, angles_for(records), [{"start_frame": 0, "end_frame": 79}], fps=FPS
    )
    for swing in derived["swings"]:
        assert swing["clip_flag_available"] is False  # absence is visible, not assumed fine
        assert swing["clipped_frame_share"] == 0.0


def test_a_clipped_wrist_frame_breaks_the_speed_series() -> None:
    records = three_swing_records(40)
    records[11]["clip"] = {"clipped": True, "edges": ["left"], "keypoints_outside_image": 3}
    speeds, _ = wrist_speed_series(records, FPS)
    assert speeds[11] is None and speeds[12] is None  # no speed into or out of it
