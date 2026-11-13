"""Motion trajectory and joint-angle derivation from pose records (user request).

Derives, from ALREADY published per-frame pose records (Phase 3), without any
model inference:

- **Body trajectories** — per-keypoint frame-by-frame paths (wrist, shoulder,
  hip, ankle, ...) as timestamped 2D polylines. Valid only where the keypoint
  was valid in that frame; gaps are preserved as gaps (FR-POSE-003).
- **2D proxy angles** — shoulder-line angle, hip-line angle, shoulder-hip
  separation (2D rotational proxy), elbow angles, knee angles. Computed only
  from frames where all involved keypoints are valid; otherwise the value is
  omitted for that frame.

Honesty boundaries (all carried into outputs):

- everything is a 2D IMAGE-PLANE PROXY: camera-position dependent, not true
  3D biomechanics, no physical force claims;
- the wrist trajectory is labeled as WRIST — the racket is not a body
  keypoint and its path must not be implied (FR-POSE-003);
- angles are relative comparisons; no permanent ratings.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from tennis_backend.domain.models import DomainViolation

_RECORDS_EMPTY = "MOTION_NO_RECORDS"

#: Body keypoints used for the headline trajectories (COCO-WholeBody indices).
TRAJECTORY_KEYPOINTS: dict[str, int] = {
    "left_wrist": 9,
    "right_wrist": 10,
    "left_shoulder": 5,
    "right_shoulder": 6,
    "left_hip": 11,
    "right_hip": 12,
    "left_ankle": 15,
    "right_ankle": 16,
    "nose": 0,
}

#: Triangles for joint angles: vertex first, then the two endpoints.
ANGLE_TRIANGLES: dict[str, tuple[int, int, int]] = {
    "left_elbow_angle": (7, 5, 9),  # shoulder-elbow-wrist
    "right_elbow_angle": (8, 6, 10),
    "left_shoulder_angle": (5, 11, 7),  # hip-shoulder-elbow
    "right_shoulder_angle": (6, 12, 8),
    "left_hip_angle": (11, 5, 13),  # shoulder-hip-knee
    "right_hip_angle": (12, 6, 14),
    "left_knee_angle": (13, 11, 15),  # hip-knee-ankle
    "right_knee_angle": (14, 12, 16),
}


def _line_angle_deg(ax: float, ay: float, bx: float, by: float) -> float:
    """Angle of line a->b against the horizontal image axis, in [0, 180)."""
    deg = math.degrees(math.atan2(abs(by - ay), abs(bx - ax)))
    return round(deg, 1)


def _angle_at_deg(vx: float, vy: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Angle at vertex v between segments v->a and v->b, in [0, 180]."""
    v1 = (ax - vx, ay - vy)
    v2 = (bx - vx, by - vy)
    dot = v1[0] * v2[0] + v1[1] * v2[1]
    n1 = math.hypot(*v1)
    n2 = math.hypot(*v2)
    if n1 == 0 or n2 == 0:
        raise ValueError("zero-length segment")
    cos = max(-1.0, min(1.0, dot / (n1 * n2)))
    return round(math.degrees(math.acos(cos)), 1)


def derive_motion(pose_records: list[dict[str, Any]], kpt_threshold: float = 0.3) -> dict[str, Any]:
    """Derive body trajectories and 2D proxy angles from pose records.

    Returns the payload shape for ``motion-trajectories.jsonl`` (per-frame
    trajectories and angles) plus a summary block.
    """
    if not pose_records:
        raise DomainViolation(_RECORDS_EMPTY, "pose records are empty")

    # 1. per-keypoint trajectories: timestamped (x, y) where valid, None where
    #    not — gaps preserved as gaps (FR-POSE-003)
    trajectories: dict[str, list[dict[str, Any] | None]] = {
        name: [] for name in TRAJECTORY_KEYPOINTS
    }
    angles_per_frame: list[dict[str, Any]] = []

    for record in pose_records:
        # a frame where the subject is cut off by the frame border carries
        # extrapolated joints (detector guesses for out-of-image body parts):
        # it is treated exactly like a lost-subject frame — a preserved gap.
        # The raw record is untouched; records written before the ``clip``
        # field existed carry no flag and are used as before.
        clip = record.get("clip")
        clipped = bool(clip and clip.get("clipped"))
        kpts = None if clipped else record.get("keypoints")
        valid = [] if clipped else (record.get("valid") or [])
        frame_payload: dict[str, Any] = {"frame": record["frame"], "pts_s": record["pts_s"]}

        for name, index in TRAJECTORY_KEYPOINTS.items():
            if kpts is not None and index < len(kpts) and valid[index]:
                x, y, _s = kpts[index]
                trajectories[name].append(
                    {"frame": record["frame"], "pts_s": record["pts_s"], "x": x, "y": y}
                )
            else:
                trajectories[name].append(None)

        # 2. shoulder/hip line angles + separation (2D rotational proxy)
        def kpt(index: int, pts: list[Any] | None, mask: list[bool]) -> tuple[float, float, bool]:
            if pts is not None and index < len(pts) and mask[index]:
                return pts[index][0], pts[index][1], True
            return 0.0, 0.0, False

        ls_x, ls_y, ls_ok = kpt(5, kpts, valid)  # left shoulder
        rs_x, rs_y, rs_ok = kpt(6, kpts, valid)  # right shoulder
        lh_x, lh_y, lh_ok = kpt(11, kpts, valid)  # left hip
        rh_x, rh_y, rh_ok = kpt(12, kpts, valid)  # right hip
        angles: dict[str, float | None] = {}

        if ls_ok and rs_ok:
            angles["shoulder_line_deg"] = _line_angle_deg(ls_x, ls_y, rs_x, rs_y)
        else:
            angles["shoulder_line_deg"] = None
        if lh_ok and rh_ok:
            angles["hip_line_deg"] = _line_angle_deg(lh_x, lh_y, rh_x, rh_y)
        else:
            angles["hip_line_deg"] = None
        if angles["shoulder_line_deg"] is not None and angles["hip_line_deg"] is not None:
            sep = abs(angles["shoulder_line_deg"] - angles["hip_line_deg"])
            # rotational separation proxy: fold to [0, 90]
            angles["shoulder_hip_separation_deg"] = round(min(sep, 180 - sep), 1)
        else:
            angles["shoulder_hip_separation_deg"] = None

        # 3. joint angles (only when all triangle kpts valid)
        for angle_name, (vertex, end_a, end_b) in ANGLE_TRIANGLES.items():
            vx, vy, v_ok = kpt(vertex, kpts, valid)
            ax, ay, a_ok = kpt(end_a, kpts, valid)
            bx, by, b_ok = kpt(end_b, kpts, valid)
            if v_ok and a_ok and b_ok:
                try:
                    angles[angle_name] = _angle_at_deg(vx, vy, ax, ay, bx, by)
                except ValueError:
                    angles[angle_name] = None
            else:
                angles[angle_name] = None

        frame_payload["angles"] = angles
        angles_per_frame.append(frame_payload)

    limitations = [
        "all values are 2D image-plane proxies; camera-position dependent, "
        "not true 3D biomechanics or physical force",
        "wrist trajectories are WRIST paths; the racket is not a body keypoint "
        "and its path is not implied (FR-POSE-003)",
        "angles computed only from frames with all involved keypoints valid; "
        "gaps preserved as null",
        "relative values only; no permanent ratings or levels",
    ]
    summary = {
        "frames": len(pose_records),
        "trajectory_keypoints": sorted(TRAJECTORY_KEYPOINTS),
        "angle_names": sorted(ANGLE_TRIANGLES)
        + [
            "shoulder_line_deg",
            "hip_line_deg",
            "shoulder_hip_separation_deg",
        ],
        "limitations": limitations,
    }
    return {"trajectories": trajectories, "angles_per_frame": angles_per_frame, "summary": summary}


def publish_motion(derived: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    """Write the derived motion artifacts for the workbench."""
    out_dir.mkdir(parents=True, exist_ok=True)
    traj_path = out_dir / "motion-trajectories.json"
    angles_path = out_dir / "motion-angles.jsonl"
    traj_path.write_text(
        json.dumps(
            {
                "trajectories": derived["trajectories"],
                "summary": derived["summary"],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    with angles_path.open("w", encoding="utf-8") as handle:
        for frame in derived["angles_per_frame"]:
            handle.write(json.dumps(frame, ensure_ascii=False) + "\n")
    return traj_path, angles_path
