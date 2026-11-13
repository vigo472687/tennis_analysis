"""Single-swing segmentation and temporal summary (专家 continuous-action data).

The expert layer used to receive only aggregated ``range`` values: no time
order, so it could not say whether trunk rotation led the arm, when each
angle peaked, or how many swings a clip holds. This module derives, from the
ALREADY published per-frame pose records (no model inference):

- **swing windows** — an action segment is a movement window, not one swing
  (2%/frame centre motion + 1 s merge gap can fuse several swings), so each
  segment is split at its wrist-speed peaks;
- **a compact temporal summary per swing** (target ≤ 4 KB): phase boundaries,
  and per angle the start / peak / peak time / end / max angular velocity;
- **a trunk-turn proxy for the shoulder line and the hip line** — the V1 line
  angles fold to 0–90° and lose the turning, and (checked on real back-view
  video) a signed *tilt* angle is the wrong quantity there: seen from behind,
  turning the trunk shows up as the left→right width shrinking toward the
  side-on pose, not as the line tilting. So rotation *order* uses a
  width-based turn proxy (0° = square to the camera, 90° = side-on). The
  accepted ``motion.py`` is left untouched.

Honesty boundaries (carried into every output):

- the wrist-speed peak is the instant the WRIST moved fastest — it is NOT
  ball contact, not racket-head speed and not "the hit" (FR-POSE-003/004,
  AC-POSE-004); no ball or racket is observed here;
- phases are a speed heuristic (preparation → drive → follow-through) and say
  so; they are not validated stroke phases;
- everything is a 2D image-plane proxy, camera dependent; the turn proxy is
  only meaningful *relative within this clip*: it cannot tell facing the
  camera from facing away, and cannot tell clockwise from anticlockwise
  (the rotation direction is unknown);
- gaps stay gaps: frames without a valid keypoint contribute nothing and the
  valid-frame share is reported, never interpolated.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

# COCO-WholeBody indices (published in skeleton-spec.json)
_L_SHOULDER, _R_SHOULDER = 5, 6
_L_HIP, _R_HIP = 11, 12
_L_WRIST, _R_WRIST = 9, 10

#: Angle series summarised per swing (names match motion-angles.jsonl).
ANGLE_NAMES: tuple[str, ...] = (
    "left_elbow_angle",
    "right_elbow_angle",
    "left_shoulder_angle",
    "right_shoulder_angle",
    "left_hip_angle",
    "right_hip_angle",
    "left_knee_angle",
    "right_knee_angle",
)
SIGNED_NAMES: tuple[str, ...] = ("shoulder_turn_deg", "hip_turn_deg")

MAX_SWINGS_PER_SEGMENT = 5
MIN_SWING_FRAMES = 6
#: A wrist-speed peak must exceed this multiple of the segment's median speed
#: (and the absolute floor below) to count as a swing, so idle jitter is not
#: promoted to a "swing".
PEAK_PROMINENCE = 2.0
SPEED_FLOOR = 0.05  # box-heights per second
SMOOTH_WINDOW = 5
MIN_VALID_SHARE = 0.5
#: After unwrapping, a body line cannot physically turn this much in ONE
#: frame (≈0.04 s): a bigger step is a detector error (keypoint swap or a
#: mislocated joint), not rotation. Such frames become gaps — never "corrected".
MAX_LINE_STEP_DEG = 60.0
#: The faster wrist is only called the hitting side when it clearly out-moves
#: the other; otherwise the side is reported as undetermined.
SIDE_MARGIN = 1.35


@dataclass(frozen=True)
class SwingWindow:
    """One candidate swing inside an action segment (inclusive frames)."""

    start_frame: int
    end_frame: int
    peak_frame: int
    drive_start_frame: int


def is_clipped(record: dict[str, Any]) -> bool:
    """Was the subject cut off by the frame border in this record?

    Records written before the ``clip`` field existed carry no flag: they are
    NOT treated as clipped (absence of evidence is not evidence), and the
    summary says how many frames were judged so that gap stays visible.
    """
    clip = record.get("clip")
    return bool(clip and clip.get("clipped"))


def _valid_point(record: dict[str, Any], index: int) -> tuple[float, float] | None:
    # a clipped frame's joints are the detector's extrapolation, not an
    # observation: derivations treat them as gaps (the raw record is untouched)
    if is_clipped(record):
        return None
    keypoints = record.get("keypoints")
    valid = record.get("valid") or []
    if keypoints is None or index >= len(keypoints) or index >= len(valid) or not valid[index]:
        return None
    return float(keypoints[index][0]), float(keypoints[index][1])


def _box_height(record: dict[str, Any]) -> float | None:
    box = record.get("subject_box")
    if not box:
        return None
    height = float(box[3]) - float(box[1])
    return height if height > 0 else None


def _smooth(values: list[float | None], window: int = SMOOTH_WINDOW) -> list[float | None]:
    """Centred moving average that ignores gaps and never bridges a None."""
    half = window // 2
    out: list[float | None] = []
    for i, value in enumerate(values):
        if value is None:
            out.append(None)
            continue
        neighbours = [v for v in values[max(0, i - half) : i + half + 1] if v is not None]
        out.append(sum(neighbours) / len(neighbours))
    return out


def wrist_speed_series(records: list[dict[str, Any]], fps: float) -> tuple[list[float | None], str]:
    """Per-frame wrist speed in box-heights/second, using the faster wrist.

    Speed is computed only between CONSECUTIVE frames that both carry a valid
    wrist keypoint — a gap breaks the series instead of fabricating a jump.
    Returns ``(speeds, side)``: the series follows the faster-moving wrist, and
    ``side`` names it only when it out-moves the other by SIDE_MARGIN — else
    ``"undetermined"`` (real back-view video: 6.9 vs 5.5, not a reliable call).
    """
    best: tuple[float, list[float | None], str] | None = None
    scores: dict[str, float] = {}
    for name, index in (("left", _L_WRIST), ("right", _R_WRIST)):
        speeds: list[float | None] = [None] * len(records)
        for i in range(1, len(records)):
            if records[i]["frame"] - records[i - 1]["frame"] != 1:
                continue
            a = _valid_point(records[i - 1], index)
            b = _valid_point(records[i], index)
            height = _box_height(records[i])
            if a is None or b is None or height is None:
                continue
            speeds[i] = math.hypot(b[0] - a[0], b[1] - a[1]) / height * fps
        known = [s for s in speeds if s is not None]
        score = max(known) if known else 0.0
        scores[name] = score
        if best is None or score > best[0]:
            best = (score, speeds, name)
    assert best is not None
    faster, slower = best[0], min(scores.values())
    # the series always follows the faster wrist (it carries the swing), but
    # calling it "the hitting side" needs a clear margin
    side = best[2] if slower <= 0 or faster / slower >= SIDE_MARGIN else "undetermined"
    return best[1], side


def _find_peaks(speeds: list[float | None]) -> list[int]:
    """Indices of local maxima that are prominent against the segment median."""
    known = [s for s in speeds if s is not None]
    if len(known) < MIN_SWING_FRAMES:
        return []
    median = statistics.median(known)
    threshold = max(SPEED_FLOOR, PEAK_PROMINENCE * median)
    peaks: list[int] = []
    for i in range(1, len(speeds) - 1):
        value = speeds[i]
        if value is None or value < threshold:
            continue
        left, right = speeds[i - 1], speeds[i + 1]
        if (left is None or value >= left) and (right is None or value > right):
            peaks.append(i)
    # keep the strongest peak within each neighbourhood of MIN_SWING_FRAMES
    peaks.sort(key=lambda i: -(speeds[i] or 0.0))
    kept: list[int] = []
    for i in peaks:
        if all(abs(i - j) >= MIN_SWING_FRAMES for j in kept):
            kept.append(i)
    return sorted(kept)


@dataclass(frozen=True)
class SplitResult:
    """Swing windows plus what the splitter chose NOT to report."""

    windows: list[SwingWindow]
    side: str
    dropped_peak_frames: list[int]  # prominent wrist-speed peaks beyond the cap


def split_swings(records: list[dict[str, Any]], fps: float) -> tuple[list[SwingWindow], str]:
    """Back-compatible wrapper: ``(windows, side)`` of :func:`split_swings_full`."""
    result = split_swings_full(records, fps)
    return result.windows, result.side


def split_swings_full(records: list[dict[str, Any]], fps: float) -> SplitResult:
    """Split one action segment's records into candidate swing windows.

    Boundaries sit at the speed minima between adjacent peaks (or the
    segment edges), so consecutive swings do not share frames. The drive
    start is the last frame before the peak where speed was still below half
    the peak — the heuristic "drive" phase begins there.
    """
    if len(records) < MIN_SWING_FRAMES:
        return SplitResult([], "undetermined", [])
    raw, wrist = wrist_speed_series(records, fps)
    speeds = _smooth(raw)
    all_peaks = _find_peaks(speeds)
    # the cap keeps the STRONGEST peaks (not the earliest): cutting by time
    # order silently dropped the second-strongest swing of a real clip (frame
    # 275, speed 2.61) because it came late. What the cap drops is reported.
    peaks = sorted(sorted(all_peaks, key=lambda i: -(speeds[i] or 0.0))[:MAX_SWINGS_PER_SEGMENT])
    dropped_peaks = [records[i]["frame"] for i in all_peaks if i not in peaks]
    if not peaks:
        return SplitResult([], wrist, dropped_peaks)

    # boundaries: lowest-speed frame between neighbouring peaks
    cuts: list[int] = []
    for a, b in zip(peaks, peaks[1:], strict=False):
        window = [(speeds[i] if speeds[i] is not None else math.inf, i) for i in range(a, b + 1)]
        cuts.append(min(window)[1])
    starts = [0, *[c + 1 for c in cuts]]
    ends = [*cuts, len(records) - 1]

    windows: list[SwingWindow] = []
    for peak, lo, hi in zip(peaks, starts, ends, strict=True):
        if hi - lo + 1 < MIN_SWING_FRAMES:
            continue
        half_peak = (speeds[peak] or 0.0) / 2
        drive = lo
        for i in range(peak, lo - 1, -1):
            value = speeds[i]
            if value is not None and value < half_peak:
                drive = i
                break
        windows.append(
            SwingWindow(
                start_frame=records[lo]["frame"],
                end_frame=records[hi]["frame"],
                peak_frame=records[peak]["frame"],
                drive_start_frame=records[drive]["frame"],
            )
        )
    return SplitResult(windows, wrist, dropped_peaks)


def _width(record: dict[str, Any], left_index: int, right_index: int) -> float | None:
    """Signed left→right x-extent of a body line, in subject-box heights.

    Normalised by the box height so subject size and camera distance cancel.
    The sign says which way round the two keypoints sit on the image x axis.
    """
    a = _valid_point(record, left_index)
    b = _valid_point(record, right_index)
    height = _box_height(record)
    if a is None or b is None or height is None:
        return None
    return (b[0] - a[0]) / height


def _turn_series(
    records: list[dict[str, Any]], left_index: int, right_index: int
) -> list[float | None]:
    """Trunk-turn proxy per frame: 0° square to camera, 90° side-on.

    ``turn = arccos(width / reference_width)`` where ``reference_width`` is
    the subject's own near-maximum width in this swing (90th percentile of
    |width|), i.e. what "square-on" looks like for THIS person from THIS
    camera. A crossing of the line through side-on (width ≈ 0) therefore
    reads as the smooth rotation it is — unlike a tilt angle, which jumps by
    ~180° there. Needs at least 8 valid frames, else the whole series is a
    gap (a reference from a handful of frames would be meaningless).
    """
    widths = [_width(r, left_index, right_index) for r in records]
    known = sorted(abs(w) for w in widths if w is not None)
    if len(known) < 8:
        return [None] * len(records)
    reference = known[int(0.9 * (len(known) - 1))]
    if reference < 0.02:  # the line never showed a measurable width
        return [None] * len(records)
    out: list[float | None] = []
    for width in widths:
        if width is None:
            out.append(None)
            continue
        ratio = max(-1.0, min(1.0, width / reference))
        out.append(round(math.degrees(math.acos(ratio)), 1))
    return out


def _drop_jumps(values: list[float | None]) -> tuple[list[float | None], int]:
    """Turn impossible single-frame steps of a turn series into gaps.

    A trunk cannot turn more than MAX_LINE_STEP_DEG in ≈0.04 s: a bigger
    step is a detector error (a left/right keypoint swap or a mislocated
    joint). The offending frame becomes a gap; nothing is flipped back or
    interpolated.
    """
    out: list[float | None] = []
    last: float | None = None
    dropped = 0
    for value in values:
        if value is None:
            out.append(None)
            continue
        if last is not None and abs(value - last) > MAX_LINE_STEP_DEG:
            out.append(None)
            dropped += 1
            continue
        out.append(value)
        last = value
    return out, dropped


def _series(
    records: list[dict[str, Any]],
    angles_by_frame: dict[int, dict[str, Any]],
    name: str,
) -> list[float | None]:
    if name == "shoulder_turn_deg":
        return _drop_jumps(_turn_series(records, _L_SHOULDER, _R_SHOULDER))[0]
    if name == "hip_turn_deg":
        return _drop_jumps(_turn_series(records, _L_HIP, _R_HIP))[0]
    out: list[float | None] = []
    for record in records:
        frame_angles = angles_by_frame.get(record["frame"], {})
        value = frame_angles.get(name)
        out.append(float(value) if value is not None else None)
    return out


def _summarise_series(
    values: list[float | None], times: list[float], origin_s: float
) -> dict[str, Any] | None:
    """Start / peak / end plus the max angular velocity of one series."""
    known = [(i, v) for i, v in enumerate(values) if v is not None]
    share = len(known) / len(values) if values else 0.0
    if len(known) < 3 or share < MIN_VALID_SHARE:
        return None
    peak_i, peak_v = max(known, key=lambda iv: iv[1])
    low_i, low_v = min(known, key=lambda iv: iv[1])
    # max angular velocity between CONSECUTIVE valid frames only
    best_rate = 0.0
    best_i: int | None = None
    for (i, v), (j, w) in zip(known, known[1:], strict=False):
        if j - i != 1:
            continue
        dt = times[j] - times[i]
        if dt <= 0:
            continue
        rate = abs(w - v) / dt
        if rate > best_rate:
            best_rate, best_i = rate, j
    return {
        "start": round(known[0][1], 1),
        "end": round(known[-1][1], 1),
        "max": round(peak_v, 1),
        "max_t_s": round(times[peak_i] - origin_s, 3),
        "min": round(low_v, 1),
        "min_t_s": round(times[low_i] - origin_s, 3),
        "max_rate_deg_s": round(best_rate, 1),
        "max_rate_t_s": round(times[best_i] - origin_s, 3) if best_i is not None else None,
        "valid_share": round(share, 2),
    }


def summarise_swing(
    window: SwingWindow,
    records: list[dict[str, Any]],
    angles_by_frame: dict[int, dict[str, Any]],
    *,
    swing_id: str,
    wrist: str,
) -> dict[str, Any]:
    """The compact temporal summary of ONE swing (times relative to its start)."""
    chunk = [r for r in records if window.start_frame <= r["frame"] <= window.end_frame]
    times = [float(r["pts_s"]) for r in chunk]
    origin = times[0]
    duration = round(times[-1] - origin, 3)

    def at(frame: int) -> float:
        for record in chunk:
            if record["frame"] == frame:
                return round(float(record["pts_s"]) - origin, 3)
        return 0.0

    angles: dict[str, Any] = {}
    for name in (*ANGLE_NAMES, *SIGNED_NAMES):
        summary = _summarise_series(_series(chunk, angles_by_frame, name), times, origin)
        angles[name] = summary  # None = not enough valid frames (honest gap)

    drive_t = at(window.drive_start_frame)
    peak_t = at(window.peak_frame)
    # which trunk line reached its fastest turn first — the rotation ORDER
    order: list[dict[str, Any]] = []
    for name in SIGNED_NAMES:
        entry = angles[name]
        if entry and entry["max_rate_t_s"] is not None:
            order.append({"line": name, "fastest_turn_t_s": entry["max_rate_t_s"]})
    order.sort(key=lambda item: item["fastest_turn_t_s"])

    return {
        "swing_id": swing_id,
        "frames": [window.start_frame, window.end_frame],
        "duration_s": duration,
        "valid_frame_share": round(
            sum(1 for r in chunk if r.get("keypoints") is not None and not is_clipped(r))
            / len(chunk),
            2,
        ),
        "clipped_frame_share": round(sum(1 for r in chunk if is_clipped(r)) / len(chunk), 2),
        "clip_flag_available": any(r.get("clip") is not None for r in chunk),
        "hitting_side_guess": wrist,
        "phases": [
            {"name": "preparation", "from_s": 0.0, "to_s": drive_t},
            {"name": "drive", "from_s": drive_t, "to_s": peak_t},
            {"name": "follow_through", "from_s": peak_t, "to_s": duration},
        ],
        "wrist_speed_peak_t_s": peak_t,
        "rotation_order": order,
        "angles": angles,
        "original_start_frame": window.start_frame,
        "limitations": [
            "wrist-speed peak is when the WRIST moved fastest, not ball contact or"
            " racket-head speed (no ball or racket is observed)",
            "phases are a wrist-speed heuristic, not validated stroke phases",
            "2D image-plane proxy: the trunk-turn angle is width-based (0° = square"
            " to the camera, 90° = side-on); it cannot tell facing-camera from"
            " facing-away nor clockwise from anticlockwise, and only changes"
            " within this clip are meaningful",
            "angles with fewer than half their frames valid are null (gaps are never interpolated)",
            "frames where the subject is cut off by the frame border are excluded"
            " (their joints are extrapolated); see clipped_frame_share",
        ],
    }


def derive_swings(
    pose_records: list[dict[str, Any]],
    angles_per_frame: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    *,
    fps: float,
) -> dict[str, Any]:
    """All swings of one subject, segment by segment.

    ``segments`` are the published ``action-selection.json`` windows. Records
    outside every segment (idle context frames) are never split into swings.
    """
    angles_by_frame = {int(a["frame"]): a.get("angles", {}) for a in angles_per_frame}
    swings: list[dict[str, Any]] = []
    dropped_by_cap: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        lo, hi = int(segment["start_frame"]), int(segment["end_frame"])
        records = [r for r in pose_records if lo <= r["frame"] <= hi]
        split = split_swings_full(records, fps)
        windows, wrist = split.windows, split.side
        if split.dropped_peak_frames:
            dropped_by_cap.append(
                {"segment": index, "dropped_peak_frames": split.dropped_peak_frames}
            )
        for number, window in enumerate(windows):
            swings.append(
                summarise_swing(
                    window,
                    records,
                    angles_by_frame,
                    swing_id=f"swing-{index}-{number}",
                    wrist=wrist,
                )
            )
    return {
        "fps": fps,
        "swing_count": len(swings),
        "swings": swings,
        # prominent wrist-speed peaks the per-segment cap chose not to report:
        # a "swing_count" without this would hide that more movement exists
        "peaks_beyond_cap": dropped_by_cap,
        "notes": [
            "an action segment can hold several swings: segments are movement"
            " windows, swings are split at wrist-speed peaks",
            "no swing found does NOT mean no movement: a segment without a"
            " prominent wrist-speed peak is reported with swing_count=0",
        ],
    }


#: Contact-sheet cell roles in time order, with their label.
SHEET_ROLES: tuple[tuple[str, str], ...] = (
    ("start", "swing start"),
    ("drive", "drive start"),
    ("pre_peak", "before peak"),
    ("peak", "wrist-speed peak"),
    ("post_peak", "after peak"),
    ("end", "swing end"),
)


def sheet_frames(swing: dict[str, Any]) -> list[dict[str, Any]]:
    """Which frames of ONE swing summary to show on a contact sheet.

    Pure and deterministic: positions come from the already-published summary
    (phase boundaries + the wrist-speed peak), so the sheet and the summary
    can never disagree. Duplicate frames collapse (a very short swing shows
    fewer cells rather than the same frame twice), and the order is time.

    Each entry: ``{"role", "label", "frame"}``.
    """
    first, last = int(swing["frames"][0]), int(swing["frames"][1])
    fps_guess = (last - first) / swing["duration_s"] if swing["duration_s"] > 0 else 25.0

    def frame_at(rel_s: float) -> int:
        return max(first, min(last, round(first + rel_s * fps_guess)))

    phases = {p["name"]: p for p in swing["phases"]}
    drive_start = frame_at(phases["drive"]["from_s"])
    peak = frame_at(swing["wrist_speed_peak_t_s"])
    chosen = {
        "start": first,
        "drive": drive_start,
        "pre_peak": (drive_start + peak) // 2,
        "peak": peak,
        "post_peak": (peak + last) // 2,
        "end": last,
    }
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for role, label in SHEET_ROLES:
        frame = chosen[role]
        if frame in seen:
            continue
        seen.add(frame)
        out.append({"role": role, "label": label, "frame": frame})
    return sorted(out, key=lambda item: item["frame"])
