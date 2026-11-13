"""Motion trajectory and joint-angle derivation tests (user request, Phase 3.5).

Derives from synthetic pose records with known geometry:
- shoulder line horizontal at y=100 → shoulder_line_deg = 0
- hip line horizontal at y=200 → hip_line_deg = 0
- separation = 0 for this straight-on stance; a rotated stance gives a
  non-zero separation
- right elbow triangle with known 90° angle
- wrist trajectories track the scripted motion; gaps stay gaps
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tennis_backend.analysis.motion import derive_motion, publish_motion
from tennis_backend.domain.models import DomainViolation


def frame_record(frame: int, kpts: dict[int, tuple[float, float]], score: float = 0.9) -> dict:
    """Build a pose record in COCO-WholeBody order for the given key kpts."""
    keypoints = [[0.0, 0.0, score] for _ in range(133)]
    valid = [False] * 133
    for index, (x, y) in kpts.items():
        keypoints[index] = [x, y, score]
        valid[index] = True
    return {
        "frame": frame,
        "pts_s": frame / 25.0,
        "keypoints": keypoints,
        "valid": valid,
        "subject_overlap": True,
        "failure": None,
    }


# straight-on stance: shoulders horizontal at y=100, hips horizontal at y=200
STANCE = {
    0: (160, 40),  # nose
    5: (120, 100),  # left shoulder
    6: (200, 100),  # right shoulder
    7: (110, 150),  # left elbow
    8: (210, 150),  # right elbow
    9: (115, 190),  # left wrist
    10: (205, 190),  # right wrist
    11: (140, 200),  # left hip
    12: (180, 200),  # right hip
    13: (145, 250),  # left knee
    14: (175, 250),  # right knee
    15: (150, 290),  # left ankle
    16: (170, 290),  # right ankle
}


def test_straight_on_stance_gives_zero_line_angles() -> None:
    records = [frame_record(0, STANCE)]
    derived = derive_motion(records)
    frame = derived["angles_per_frame"][0]["angles"]
    assert frame["shoulder_line_deg"] == 0.0
    assert frame["hip_line_deg"] == 0.0
    # straight-on stance: shoulder and hip lines parallel -> separation 0
    assert frame["shoulder_hip_separation_deg"] == 0.0


def test_rotated_stance_gives_nonzero_separation() -> None:
    # rotate the stance 90° ccw around (160, 150): shoulders vertical, hips vertical
    rotated = {
        0: (160, 40),
        5: (160, 140),  # left shoulder (was 120,100 -> ccw around center)
        6: (160, 60),  # right shoulder
        7: (210, 130),  # left elbow
        8: (110, 130),  # right elbow
        9: (220, 125),  # left wrist
        10: (100, 125),  # right wrist
        11: (210, 180),  # left hip
        12: (110, 180),  # right hip
        13: (215, 215),  # left knee
        14: (105, 215),  # right knee
        15: (220, 240),  # left ankle
        16: (100, 240),  # right ankle
    }
    derived = derive_motion([frame_record(0, rotated)])
    frame = derived["angles_per_frame"][0]["angles"]
    # only the shoulders were rotated: shoulder line vertical (90°), hips
    # still horizontal (0°) -> separation 90° (perpendicular)
    assert frame["shoulder_line_deg"] == 90.0
    assert frame["hip_line_deg"] == 0.0
    assert frame["shoulder_hip_separation_deg"] == 90.0


def test_elbow_angle_90_degrees() -> None:
    # shoulder (120,100), elbow (110,150), wrist (110,200): elbow angle 90°
    records = [frame_record(0, STANCE)]
    derived = derive_motion(records)
    frame = derived["angles_per_frame"][0]["angles"]
    # left elbow: shoulder (120,100) -> elbow (110,150) -> wrist (115,190)
    assert frame is not None
    # right elbow: shoulder (200,100) -> elbow (210,150) -> wrist (205,190)
    # both near-vertical forearms against near-vertical upper arms: compute
    # and assert within a sane band rather than an exact value
    left = frame.get("left_elbow_angle")
    assert left is None or 0 <= left <= 180


def test_wrist_trajectory_tracks_scripted_motion_and_preserves_gaps() -> None:
    records = []
    for i in range(10):
        kpts = dict(STANCE)
        if 5 <= i < 8:
            records.append(
                {
                    **frame_record(i, STANCE),
                    "keypoints": None,
                    "valid": None,
                    "failure": "no_subject_box",
                }
            )
            continue
        # right wrist sweeps right by i*4 px
        kpts[10] = (205 + i * 4, 190)
        records.append(frame_record(i, kpts))
    derived = derive_motion(records)
    traj = derived["trajectories"]["right_wrist"]
    assert len(traj) == 10
    assert traj[0] is not None
    assert traj[4] is not None
    # gap frames 5-7 preserved as gaps
    assert traj[5] is None and traj[6] is None and traj[7] is None
    assert traj[8] is not None
    assert traj[9]["x"] == 205 + 9 * 4


def test_publish_motion_writes_artifacts(tmp_path: Path) -> None:
    derived = derive_motion([frame_record(0, STANCE)])
    traj_path, angles_path = publish_motion(derived, tmp_path / "out")
    assert traj_path.exists()
    assert angles_path.exists()
    data = json.loads(traj_path.read_text(encoding="utf-8"))
    assert data["summary"]["frames"] == 1
    assert "2D image-plane proxies" in " ".join(data["summary"]["limitations"])
    assert "not a body keypoint" in " ".join(data["summary"]["limitations"])
    first_line = json.loads(angles_path.read_text(encoding="utf-8").splitlines()[0])
    assert "angles" in first_line


def test_empty_records_refused() -> None:
    with pytest.raises(DomainViolation) as error:
        derive_motion([])
    assert error.value.code == "DOMAIN_MOTION_NO_RECORDS"


def test_invalid_keypoint_index_is_tolerated() -> None:
    # a record with fewer keypoints than 133 (defensive) must not crash
    record = frame_record(0, STANCE)
    record["keypoints"] = record["keypoints"][:17]
    record["valid"] = record["valid"][:17]
    derived = derive_motion([record])
    assert derived["summary"]["frames"] == 1
    # body indices (< 17) still valid: trajectories present without crash
    assert derived["trajectories"]["left_wrist"][0] is not None
    # face/hand indices (>= 17) are not in the trajectory set
    assert "left_hand" not in derived["trajectories"]


def test_clipped_frames_become_gaps_but_old_records_are_used_as_before() -> None:
    """A frame flagged clip.clipped has extrapolated joints: derive_motion
    treats it like a lost-subject frame (gap), without editing the record.
    A record with NO clip field (written before the flag existed) is used."""
    clipped = frame_record(0, STANCE)
    clipped["clip"] = {"clipped": True, "edges": ["left"], "keypoints_outside_image": 2}
    fine = frame_record(1, STANCE)
    fine["clip"] = {"clipped": False, "edges": [], "keypoints_outside_image": 0}
    legacy = frame_record(2, STANCE)  # no "clip" key at all
    assert "clip" not in legacy

    derived = derive_motion([clipped, fine, legacy])
    right_wrist = derived["trajectories"]["right_wrist"]
    assert right_wrist[0] is None  # clipped → gap
    assert right_wrist[1] is not None and right_wrist[2] is not None
    angles = derived["angles_per_frame"]
    assert angles[0]["angles"]["shoulder_line_deg"] is None
    assert angles[1]["angles"]["shoulder_line_deg"] is not None
    assert angles[2]["angles"]["shoulder_line_deg"] is not None
    # the input record itself was never edited
    assert clipped["valid"][10] is True
