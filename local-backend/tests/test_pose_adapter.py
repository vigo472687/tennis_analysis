"""RealPoseAdapter tests (Phase 4): honest stages over published records.

All results are validated against the FROZEN contracts through run_stage,
so a shape drift fails here before it can reach a caller. The honesty
assertions mirror the spec rules: strokes carry no events, timing invents
no times, metrics below MIN_VALID_FRAMES stay insufficient_evidence, and
nothing ever claims simulation=true.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tennis_backend.analysis.adapters import run_stage
from tennis_backend.analysis.pose import (
    EVENT_REQUIRES,
    MIN_VALID_FRAMES,
    RealPoseAdapter,
    real_evidence_entries,
)
from tennis_backend.application.worker import CancellationToken
from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.domain.models import DomainViolation, MediaAsset, MediaProbe

REPO_ROOT = Path(__file__).resolve().parents[2]


def neutral_probe() -> MediaProbe:
    return MediaProbe(
        container="mp4",
        codec="h264",
        duration_s=3.0,
        fps=25.0,
        width=640,
        height=360,
        rotation="0",
        slow_motion="unknown",
        hdr="no",
        device_source="unknown",
        panorama="flat",
        observation_kind="measured",
    )


def angle_frame(frame: int, angles: dict[str, float | None]) -> dict[str, Any]:
    return {"frame": frame, "pts_s": frame / 25.0, "angles": angles}


def motion_payload(
    angles: list[dict[str, Any]] | None = None,
    trajectories: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    return {
        "trajectories": trajectories or {},
        "angles_per_frame": angles or [],
        "summary": {
            "angle_names": [
                "left_elbow_angle",
                "shoulder_line_deg",
                "hip_line_deg",
                "shoulder_hip_separation_deg",
            ],
        },
    }


def pose_record(frame: int, box: list[int] | None = None) -> dict[str, Any]:
    return {
        "frame": frame,
        "pts_s": frame / 25.0,
        "subject_box": box or [100, 50, 160, 250],  # height 200
        "keypoints": None,
        "valid": None,
        "subject_overlap": True,
        "failure": None,
    }


def make_adapter(
    angles: list[dict[str, Any]] | None = None,
    trajectories: dict[str, list[Any]] | None = None,
    pose_records: list[dict[str, Any]] | None = None,
    selection: dict[str, Any] | None = None,
) -> RealPoseAdapter:
    return RealPoseAdapter(
        pose_records=pose_records if pose_records is not None else [pose_record(0)],
        motion=motion_payload(angles, trajectories),
        selection=selection or {"selected_segments": []},
        segment_states=[],
    )


def run(adapter: RealPoseAdapter, stage: str, registry: ContractRegistry) -> dict[str, Any]:
    from tennis_backend.analysis.adapters import StageRequest

    return run_stage(
        adapter,
        stage,
        StageRequest(task_id="t", version=1, fixture_id=None, media=neutral_probe()),
        cancel=CancellationToken(),
        registry=registry,
    )


@pytest.fixture
def registry() -> ContractRegistry:
    return ContractRegistry.load(REPO_ROOT / "contracts" / "v1")


def test_simulation_flag_is_false() -> None:
    real = make_adapter()
    assert real.simulation is False
    assert real.kind == "real_pose"


def test_capabilities_declare_all_stages_and_limitations() -> None:
    caps = make_adapter().capabilities()
    assert caps.supported_stages == ("strokes", "timing", "metrics")
    assert caps.simulation is False
    assert any("2D image-plane" in item for item in caps.limitations)


def test_strokes_are_empty_and_contract_valid(registry: ContractRegistry) -> None:
    result = run(make_adapter(), "strokes", registry)
    assert result["events"] == []
    assert result["simulation"] is False


def test_timing_reports_all_kinds_honestly(registry: ContractRegistry) -> None:
    result = run(make_adapter(), "timing", registry)
    kinds = {event["kind"]: event for event in result["events"]}
    assert set(kinds) == set(EVENT_REQUIRES)
    # ball/racket requirements: not_visible, no invented time
    assert kinds["bounce_candidate"]["value_state"] == "not_visible"
    assert kinds["bounce_candidate"]["requires"] == ["ball"]
    assert kinds["racket_drop"]["value_state"] == "not_visible"
    # body/feet requirements: insufficient_evidence (no validated detector)
    assert kinds["preparation"]["value_state"] == "insufficient_evidence"
    assert kinds["first_step"]["value_state"] == "insufficient_evidence"
    assert kinds["leg_drive_rotation"]["requires"] == ["feet", "body"]
    for event in result["events"]:
        assert event["original_s"] is None
    assert result["simulation"] is False
    assert any("no time is invented" in item for item in result["limitations"])


def test_metrics_angle_ranges_observed_and_contract_valid(
    registry: ContractRegistry,
) -> None:
    angles = [
        angle_frame(i, {"left_elbow_angle": 60.0 + i * 10, "shoulder_line_deg": 5.0})
        for i in range(MIN_VALID_FRAMES)
    ]
    result = run(make_adapter(angles=angles), "metrics", registry)
    by_id = {metric["metric_id"]: metric for metric in result["metrics"]}
    elbow = by_id["pose.range.left_elbow_angle"]
    assert elbow["value"] == 20.0  # max 80 - min 60
    assert elbow["value_state"] == "observed"
    assert elbow["unit"] == "relative"
    assert any("2D image-plane" in item for item in elbow["limitations"])
    # single-value angles (constant 5.0): range 0, still observed
    shoulder = by_id["pose.range.shoulder_line_deg"]
    assert shoulder["value"] == 0.0
    assert shoulder["value_state"] == "observed"
    assert result["simulation"] is False


def test_metrics_below_min_frames_are_insufficient(registry: ContractRegistry) -> None:
    angles = [angle_frame(0, {"left_elbow_angle": 60.0})]  # 1 frame < MIN_VALID_FRAMES
    result = run(make_adapter(angles=angles), "metrics", registry)
    by_id = {metric["metric_id"]: metric for metric in result["metrics"]}
    elbow = by_id["pose.range.left_elbow_angle"]
    assert elbow["value"] is None
    assert elbow["value_state"] == "insufficient_evidence"


def test_metrics_wrist_envelope_is_body_size_normalized(registry: ContractRegistry) -> None:
    # wrist sweeps 100px horizontally inside a 200px-tall subject box → 0.5
    trajectories = {
        "right_wrist": [
            {"frame": i, "pts_s": i / 25.0, "x": 100.0 + i * 50.0, "y": 190.0} for i in range(4)
        ],
        "left_wrist": [None, None, None, None],
    }
    result = run(make_adapter(trajectories=trajectories), "metrics", registry)
    by_id = {metric["metric_id"]: metric for metric in result["metrics"]}
    span_x = by_id["pose.envelope.right_wrist_x"]
    assert span_x["value"] == pytest.approx(0.75)  # 150px / 200px box height
    assert span_x["value_state"] == "observed"
    assert span_x["unit"] == "normalized"
    # all-gap left wrist: insufficient
    assert by_id["pose.envelope.left_wrist_x"]["value_state"] == "insufficient_evidence"


def test_metrics_segment_duration_and_gap(registry: ContractRegistry) -> None:
    # 3 segments / 2 gaps: both means have enough samples to be observed
    # (phase4 review D1: a single segment/gap stays insufficient)
    selection = {
        "selected_segments": [
            {"start_frame": 0, "end_frame": 24, "start_s": 0.0, "end_s": 1.0, "frames": 25},
            {"start_frame": 50, "end_frame": 74, "start_s": 2.0, "end_s": 3.0, "frames": 25},
            {"start_frame": 100, "end_frame": 124, "start_s": 4.0, "end_s": 5.0, "frames": 25},
        ]
    }
    result = run(make_adapter(selection=selection), "metrics", registry)
    by_id = {metric["metric_id"]: metric for metric in result["metrics"]}
    assert by_id["pose.action_segment_mean_duration_s"]["value"] == pytest.approx(1.0)
    assert by_id["pose.action_segment_mean_duration_s"]["value_state"] == "observed"
    assert by_id["pose.action_segment_mean_gap_s"]["value"] == pytest.approx(1.0)
    assert by_id["pose.action_segment_mean_gap_s"]["value_state"] == "observed"


def test_metrics_single_segment_or_gap_is_insufficient(registry: ContractRegistry) -> None:
    selection = {
        "selected_segments": [
            {"start_frame": 0, "end_frame": 24, "start_s": 0.0, "end_s": 1.0, "frames": 25},
            {"start_frame": 50, "end_frame": 74, "start_s": 2.0, "end_s": 3.0, "frames": 25},
        ]
    }
    result = run(make_adapter(selection=selection), "metrics", registry)
    by_id = {metric["metric_id"]: metric for metric in result["metrics"]}
    assert by_id["pose.action_segment_mean_duration_s"]["value_state"] == "insufficient_evidence"
    assert by_id["pose.action_segment_mean_duration_s"]["value"] is None
    assert by_id["pose.action_segment_mean_gap_s"]["value_state"] == "insufficient_evidence"


def test_metrics_with_no_records_are_all_insufficient(registry: ContractRegistry) -> None:
    result = run(make_adapter(angles=[], trajectories={}, pose_records=[]), "metrics", registry)
    assert result["metrics"]
    for metric in result["metrics"]:
        assert metric["value"] is None
        assert metric["value_state"] == "insufficient_evidence"


def test_every_stage_validates_against_frozen_contracts(
    registry: ContractRegistry,
) -> None:
    angles = [
        angle_frame(i, {"left_elbow_angle": 60.0 + i * 5, "hip_line_deg": 3.0}) for i in range(4)
    ]
    adapter = make_adapter(
        angles=angles,
        trajectories={
            "left_wrist": [
                {"frame": i, "pts_s": i / 25.0, "x": 100 + i, "y": 190} for i in range(4)
            ]
        },
        selection={
            "selected_segments": [
                {"start_frame": 0, "end_frame": 3, "start_s": 0.0, "end_s": 0.16, "frames": 4},
                {"start_frame": 8, "end_frame": 11, "start_s": 0.32, "end_s": 0.48, "frames": 4},
            ]
        },
    )
    for stage in ("strokes", "timing", "metrics"):
        result = run(adapter, stage, registry)
        assert result["simulation"] is False


def test_unsupported_stage_refused() -> None:
    from tennis_backend.analysis.adapters import StageRequest

    adapter = make_adapter()
    with pytest.raises(DomainViolation):
        run_stage(
            adapter,
            "quality",
            StageRequest(task_id="t", version=1, fixture_id=None, media=neutral_probe()),
            cancel=CancellationToken(),
            registry=ContractRegistry.load(REPO_ROOT / "contracts" / "v1"),
        )


# ---------------------------------------------------------------------------
# real_evidence_entries
# ---------------------------------------------------------------------------


def media_asset(time_mapping_valid: bool = True) -> MediaAsset:
    return MediaAsset(
        media_id="m-1",
        role="standardized",
        original_ref="projects/t/v1/media/original.mp4",
        derived_from="m-0",
        probe=neutral_probe(),
        time_mapping=[
            {"normalized_s": 0.0, "original_s": 1.0},
            {"normalized_s": 10.0, "original_s": 11.0},
        ],
        time_mapping_valid=time_mapping_valid,
        simulation=False,
    )


def test_evidence_entries_map_to_original_time() -> None:
    selection = {
        "selected_segments": [
            {"start_frame": 0, "end_frame": 24, "start_s": 0.0, "end_s": 1.0, "frames": 25},
            {"start_frame": 50, "end_frame": 74, "start_s": 2.0, "end_s": 3.0, "frames": 25},
        ]
    }
    entries = real_evidence_entries(
        task_id="t1", version=1, media=media_asset(), selection=selection
    )
    assert len(entries) == 2
    first = entries[0]
    # normalized 0s → original 1s through the measured mapping (+1 offset)
    assert first["original_start_s"] == 1.0
    assert first["original_end_s"] == 2.0
    assert first["kind"] == "video_clip"
    assert first["visible_objects"] == ["body"]
    assert first["simulation"] is False
    assert first["evidence_id"] == "ev-t1-v1-seg-0"


def test_evidence_entries_refused_without_time_mapping() -> None:
    with pytest.raises(DomainViolation):
        real_evidence_entries(
            task_id="t1",
            version=1,
            media=media_asset(time_mapping_valid=False),
            selection={
                "selected_segments": [
                    {"start_frame": 0, "end_frame": 24, "start_s": 0.0, "end_s": 1.0, "frames": 25}
                ]
            },
        )


def test_evidence_entries_beyond_mapping_span_refused_not_clamped() -> None:
    """Phase4 review B1 regression: an interval outside the measured span is
    refused (EVIDENCE_OUTSIDE_MAPPING), never clamped to the last point —
    a clamped entry would publish the WRONG original frame as evidence."""
    selection = {
        "selected_segments": [
            {"start_frame": 0, "end_frame": 24, "start_s": 0.0, "end_s": 1.0, "frames": 25},
            # the fixture mapping spans only 10s of normalized time
            {
                "start_frame": 300,
                "end_frame": 324,
                "start_s": 12.0,
                "end_s": 13.0,
                "frames": 25,
            },
        ]
    }
    with pytest.raises(DomainViolation) as error:
        real_evidence_entries(task_id="t1", version=1, media=media_asset(), selection=selection)
    assert error.value.code == "DOMAIN_EVIDENCE_OUTSIDE_MAPPING"
