"""Per-frame pose extraction tests (Phase 3).

Uses an injected fake pose model so tests drive known keypoints
deterministically; the real RTMW-x integration has a smoke test against the
approved sample when rtmlib is installed. The tracker is NOT part of this
module — subject boxes come in as plain per-frame lists.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pytest

from tennis_backend.domain.models import DomainViolation
from tennis_backend.media.pose_extract import extract_pose, skeleton_spec


def test_model_initialization_diagnostics_preserve_protocol_stdout(
    monkeypatch: Any, capsys: Any
) -> None:
    from tennis_backend.media.pose_extract import RtmlibWholebodyPose

    base, file = SimpleNamespace(), SimpleNamespace()
    original_stdout = sys.stdout

    def wholebody(**_kwargs: Any) -> Any:
        base.print("load test model")
        file.print("load test checkpoint")
        assert sys.stdout is original_stdout
        return lambda _: (np.zeros((1, 133, 2)), np.ones((1, 133)))

    monkeypatch.setitem(sys.modules, "rtmlib", SimpleNamespace(Wholebody=wholebody))
    monkeypatch.setitem(sys.modules, "rtmlib.tools.base", base)
    monkeypatch.setitem(sys.modules, "rtmlib.tools.file", file)
    RtmlibWholebodyPose().infer(np.zeros((8, 8, 3)))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "load test model" in captured.err
    assert "load test checkpoint" in captured.err


class FakePoseModel:
    """Returns a deterministic keypoint grid; supports scripted failures."""

    def __init__(
        self,
        kpts: int = 3,
        score: float = 0.9,
        fail_frames: set[int] | None = None,
        offset: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        self._kpts = kpts
        self._score = score
        self._fail_frames = fail_frames or set()
        self._offset = offset
        self.frames_seen = 0

    def infer(self, frame_bgr: Any) -> tuple[np.ndarray, np.ndarray]:
        self.frames_seen += 1
        if self.frames_seen - 1 in self._fail_frames:
            raise RuntimeError("simulated inference fault")
        h, w = frame_bgr.shape[:2]
        pts = np.zeros((self._kpts, 2), dtype=np.float32)
        for i in range(self._kpts):
            pts[i, 0] = (w / 2) + self._offset[0] + i * 5
            pts[i, 1] = (h / 2) + self._offset[1] + i * 3
        scores = np.full((self._kpts,), self._score, dtype=np.float32)
        return pts, scores


@pytest.fixture
def video(tmp_path: Path) -> Path:
    writer = cv2.VideoWriter(
        str(tmp_path / "clip.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (320, 240)
    )
    for _ in range(10):
        writer.write(np.full((240, 320, 3), 128, dtype=np.uint8))
    writer.release()
    return tmp_path / "clip.mp4"


def boxes_all(frames: int, box: list[int]) -> list[list[int] | None]:
    return [list(box) for _ in range(frames)]


def test_per_frame_keypoints_recorded_raw_with_mask(video: Path, tmp_path: Path) -> None:
    outcome = extract_pose(
        video=video,
        subject_boxes=boxes_all(10, [80, 60, 240, 200]),
        out_dir=tmp_path / "out",
        model=FakePoseModel(kpts=3, score=0.9),
    )
    assert len(outcome.records) == 10
    assert outcome.frames_with_pose == 10
    for record in outcome.records:
        assert record["keypoints"] is not None
        assert len(record["keypoints"]) == 3
        assert record["valid"] == [True, True, True]
        assert record["subject_overlap"] is True
        assert record["failure"] is None
    # raw full-frame coordinates: crop [40,25,280,235] (25% margin),
    # size 240x210; first kpt at crop center + offset -> full-frame
    # (40+120, 25+105)
    first = outcome.records[0]["keypoints"][0]
    assert first[0] == pytest.approx(160, abs=1)
    assert first[1] == pytest.approx(130, abs=1)
    assert outcome.pose_jsonl.exists()
    assert outcome.skeleton_spec_path.exists()


def test_low_score_keypoints_marked_invalid(video: Path, tmp_path: Path) -> None:
    outcome = extract_pose(
        video=video,
        subject_boxes=boxes_all(10, [80, 60, 240, 200]),
        out_dir=tmp_path / "out",
        model=FakePoseModel(kpts=2, score=0.2),  # below threshold 0.3
    )
    # nothing valid -> no subject association -> no keypoints recorded
    # (low-confidence data of unclear ownership is not stored)
    assert outcome.frames_with_pose == 0
    for record in outcome.records:
        assert record["keypoints"] is None
        assert record["failure"] == "subject_not_matched"


def test_no_subject_box_records_failure_without_keypoints(video: Path, tmp_path: Path) -> None:
    outcome = extract_pose(
        video=video,
        subject_boxes=[None] * 10,
        out_dir=tmp_path / "out",
        model=FakePoseModel(),
    )
    assert outcome.frames_with_pose == 0
    assert outcome.frames_without_subject == 10
    for record in outcome.records:
        assert record["failure"] == "no_subject_box"
        assert record["keypoints"] is None


def test_inference_fault_recorded_not_crashed(video: Path, tmp_path: Path) -> None:
    outcome = extract_pose(
        video=video,
        subject_boxes=boxes_all(10, [80, 60, 240, 200]),
        out_dir=tmp_path / "out",
        model=FakePoseModel(kpts=3, fail_frames={3}),
    )
    failed = [r for r in outcome.records if r["failure"]]
    assert len(failed) == 1
    assert failed[0]["failure"] == "inference_error:RuntimeError"
    assert failed[0]["frame"] == 3
    assert outcome.frames_with_pose == 9  # the other 9 frames fine


def test_keypoints_outside_subject_box_recorded_as_not_matched(video: Path, tmp_path: Path) -> None:
    # fake model draws keypoints at the right edge; subject box on the left
    outcome = extract_pose(
        video=video,
        subject_boxes=boxes_all(10, [20, 20, 100, 100]),
        out_dir=tmp_path / "out",
        model=FakePoseModel(kpts=2, offset=(280.0, 0.0)),
    )
    assert outcome.frames_with_pose == 0
    for record in outcome.records:
        assert record["failure"] == "subject_not_matched"
        assert record["keypoints"] is None


def test_missing_video_refused(tmp_path: Path) -> None:
    with pytest.raises(DomainViolation) as error:
        extract_pose(
            video=tmp_path / "nope.mp4",
            subject_boxes=[[40, 40, 200, 200]],
            out_dir=tmp_path / "out",
            model=FakePoseModel(),
        )
    assert error.value.code == "DOMAIN_POSE_VIDEO_MISSING"


def test_skeleton_spec_is_coco_wholebody(tmp_path: Path) -> None:
    spec = skeleton_spec()
    assert spec["name"] == "coco133"
    assert len(spec["keypoints"]) == 133
    assert len(spec["skeleton"]) == 65
    names = {k["name"] for k in spec["keypoints"]}
    assert {"nose", "left_shoulder", "right_wrist", "left_ankle"} <= names


def test_selected_segments_preserve_original_frames_and_skip_gaps(
    video: Path, tmp_path: Path
) -> None:
    model = FakePoseModel()
    outcome = extract_pose(
        video=video,
        subject_boxes=boxes_all(10, [80, 60, 240, 200]),
        out_dir=tmp_path / "selected",
        model=model,
        segments=[(2, 3), (7, 9)],
    )
    assert [r["frame"] for r in outcome.records] == [2, 3, 7, 8, 9]
    assert [r["pts_s"] for r in outcome.records] == pytest.approx([0.2, 0.3, 0.7, 0.8, 0.9])
    assert model.frames_seen == 5


def test_empty_selection_never_loads_model_or_falls_back_to_full_video(
    video: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    def forbidden() -> Any:
        raise AssertionError("empty action selection must not load the model")

    monkeypatch.setattr("tennis_backend.media.pose_extract.RtmlibWholebodyPose", forbidden)
    outcome = extract_pose(
        video=video,
        subject_boxes=boxes_all(10, [80, 60, 240, 200]),
        out_dir=tmp_path / "empty",
        segments=[],
    )
    assert outcome.records == []
    assert outcome.frames_with_pose == 0
    assert outcome.pose_jsonl.read_text() == ""
    assert outcome.skeleton_spec_path.exists()


def test_real_rtmwx_smoke_on_real_sample(video: Path, tmp_path: Path) -> None:
    """Real RTMW-x smoke: 1 frame of the approved sample yields 133 kpts."""
    pytest.importorskip("rtmlib")
    outcome = extract_pose(
        video=video,
        subject_boxes=boxes_all(10, [80, 60, 240, 200]),
        out_dir=tmp_path / "out",
        model=None,  # real rtmlib Wholebody
    )
    with_poses = [r for r in outcome.records if r["keypoints"]]
    assert with_poses, "real model should produce keypoints on real frames"
    assert len(with_poses[0]["keypoints"]) == 133


# ---------------------------------------------------------------------------
# clip_state: a person cut off by the frame border has extrapolated joints
# ---------------------------------------------------------------------------


def _body(x_offset: float = 0.0) -> tuple[list[list[float]], list[bool]]:
    keypoints = [[100.0 + x_offset + i, 100.0 + i, 0.9] for i in range(17)]
    return keypoints, [True] * 17


def test_clip_state_inside_the_frame_is_not_clipped() -> None:
    from tennis_backend.media.pose_extract import clip_state

    keypoints, valid = _body()
    state = clip_state([60, 40, 180, 400], keypoints, valid, 544, 960)
    assert state["clipped"] is False and state["edges"] == []
    assert state["upper_body_outside"] == 0 and state["lower_body_outside"] == 0


def test_clip_state_box_touching_an_edge_alone_is_recorded_but_not_clipped() -> None:
    """Touching the border is recorded in ``edges`` (a fact about the box) but
    does not by itself discard the frame: a person standing tight against the
    border with every joint well inside is intact."""
    from tennis_backend.media.pose_extract import clip_state

    keypoints, valid = _body(x_offset=300)  # every joint far from the left border
    state = clip_state([0, 40, 330, 440], keypoints, valid, 544, 960)
    assert state["edges"] == ["left"]
    assert state["clipped"] is False and state["upper_body_on_edge"] == 0


def test_clip_state_flags_valid_keypoints_outside_the_image() -> None:
    """The real-data failure: the box looks fine but the detector extrapolated
    joints beyond the image — those 'valid' points are guesses."""
    from tennis_backend.media.pose_extract import clip_state

    keypoints, valid = _body()
    keypoints[5] = [-20.0, 150.0, 0.9]  # left shoulder left of the image
    keypoints[9] = [600.0, 200.0, 0.9]  # wrist right of a 544-wide image
    state = clip_state([60, 40, 300, 400], keypoints, valid, 544, 960)
    assert state["clipped"] is True and state["upper_body_outside"] == 2


def test_clip_state_ignores_invalid_keypoints_outside_the_image() -> None:
    from tennis_backend.media.pose_extract import clip_state

    keypoints, valid = _body()
    keypoints[5] = [-20.0, 150.0, 0.1]
    valid[5] = False  # already below the score threshold: not a claim, not counted
    state = clip_state([60, 40, 300, 400], keypoints, valid, 544, 960)
    assert state["clipped"] is False and state["upper_body_outside"] == 0


def test_clip_state_all_four_edges_and_missing_box() -> None:
    from tennis_backend.media.pose_extract import clip_state

    state = clip_state([0, 0, 544, 960], None, None, 544, 960)
    assert state["edges"] == ["left", "right", "top", "bottom"]
    assert state["clipped"] is False  # no keypoints to judge: edges alone never flag a frame
    assert clip_state(None, None, None, 544, 960)["clipped"] is False  # nothing to judge


def test_extract_pose_records_the_clip_state_without_touching_raw_keypoints(
    tmp_path: Path,
) -> None:
    """The flag is ADDED; the raw keypoints and valid mask are never edited."""
    import cv2

    from tennis_backend.media.pose_extract import extract_pose

    video = tmp_path / "v.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (320, 240))
    for _ in range(3):
        writer.write(np.full((240, 320, 3), 100, dtype=np.uint8))
    writer.release()

    class OutsideModel:
        """A person well inside the box, plus ONE joint extrapolated just past
        the left image edge (what a detector does for a cut-off body part).
        The bounding-box centre of all joints stays inside the person box, so
        the subject is still associated."""

        def infer(self, crop: Any) -> Any:
            h, w = crop.shape[:2]
            kpts = np.zeros((1, 133, 2))
            kpts[0, :, 0] = np.linspace(w * 0.3, w * 0.7, 133)
            kpts[0, :, 1] = np.linspace(h * 0.2, h * 0.8, 133)
            # full-frame x = crop-local x + the crop's left origin (≥ 0), so a
            # crop-local x of −60 lands left of the image edge only when the
            # crop starts near x=0 — the box below starts at x=0 on purpose
            kpts[0, 5] = [-60.0, h * 0.5]
            return kpts, np.ones((1, 133))

    outcome = extract_pose(
        video=video,
        subject_boxes=[[0, 40, 200, 220]] * 3,
        out_dir=tmp_path / "o",
        model=OutsideModel(),
    )
    for record in outcome.records:
        assert record["clip"]["clipped"] is True
        assert record["clip"]["upper_body_outside"] >= 1
        assert record["valid"][5] is True  # raw mask untouched
        assert record["keypoints"][5][0] < 0  # raw coordinate untouched


def test_ankles_below_the_frame_do_not_discard_an_intact_upper_body() -> None:
    """Calibrated on real data: 113 frames of a 291-frame clip had only the
    ANKLES a few pixels below the bottom edge. The person was intact and the
    swing metrics read the upper body, so these frames must NOT be flagged
    clipped (the overshoot is reported separately)."""
    from tennis_backend.media.pose_extract import clip_state

    keypoints, valid = _body()
    for ankle in (15, 16):
        keypoints[ankle] = [200.0, 970.0, 0.9]  # 10 px below a 960-high frame
    state = clip_state([60, 500, 300, 960], keypoints, valid, 544, 960)
    assert state["clipped"] is False
    assert state["lower_body_outside"] == 2 and state["upper_body_outside"] == 0
    assert state["edges"] == ["bottom"]


def test_a_few_pixels_of_overshoot_is_jitter_not_a_hidden_body_part() -> None:
    from tennis_backend.media.pose_extract import clip_state

    keypoints, valid = _body()
    keypoints[9] = [-3.0, 150.0, 0.9]  # wrist 3 px left of the image, box is 400 px tall
    state = clip_state([60, 40, 300, 440], keypoints, valid, 544, 960)
    assert state["upper_body_outside"] == 0 and state["clipped"] is False  # 3 px < 2% of 400


def test_box_on_the_side_border_with_a_joint_on_the_edge_is_clipped() -> None:
    """The visibly half-cut real frame: box touches the left border and the
    trunk joints sit on the picture edge, not beyond it."""
    from tennis_backend.media.pose_extract import EDGE_BAND_PX, clip_state

    keypoints, valid = _body(x_offset=300)
    keypoints[5] = [float(EDGE_BAND_PX - 1), 150.0, 0.9]  # left shoulder 9 px from the edge
    state = clip_state([0, 40, 330, 440], keypoints, valid, 544, 960)
    assert state["clipped"] is True and state["upper_body_on_edge"] == 1
    assert state["upper_body_outside"] == 0  # nothing is beyond the border: it is ON it

    far, far_valid = _body(x_offset=300)  # same box, every joint well inside
    assert clip_state([0, 40, 330, 440], far, far_valid, 544, 960)["clipped"] is False
