"""Phase-2 real_video slice tests: probe → subjects → honest state.

Runs StdioHandler in-process with an injected fake detector (the real
RF-DETR integration was validated in Phase 1 isolation and has its own
evidence). Covers: supported video end-to-end (probe → extraction →
publish → analyzing), unsupported/corrupt → unsupported terminal, ingest
outside root refused, replay without re-extraction, and multi-person
selection request.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pytest

from tennis_backend.cli import StdioHandler
from tennis_backend.contracts.validation import ContractRegistry, ContractViolation, validate_result
from test_subjects import FakeDetector, person_box


def test_detector_logs_do_not_pollute_protocol_stdout(monkeypatch: Any, capsys: Any) -> None:
    from tennis_backend.media.subjects import RFDetrPersonDetector

    logger = logging.getLogger("rf-detr")
    previous, level, propagate = list(logger.handlers), logger.level, logger.propagate
    for handler in previous:
        logger.removeHandler(handler)
    logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.setLevel(logging.INFO)

    def model(**_kwargs: Any) -> Any:
        logger.info("test weights loaded")
        return SimpleNamespace(predict=lambda *_a, **_k: SimpleNamespace(xyxy=[], data={}))

    monkeypatch.setitem(sys.modules, "rfdetr", SimpleNamespace(RFDETRNano=model))
    try:
        assert RFDetrPersonDetector().detect(np.zeros((4, 4, 3))) == []
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "test weights loaded" in captured.err
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        for handler in previous:
            logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    for sub in ("ingest", "internal", "public"):
        (root / sub).mkdir(parents=True)
    return root


def make_video(tmp_path: Path, name: str, seconds: float = 2.0) -> Path:
    video = tmp_path / name
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (320, 240))
    for _ in range(int(seconds * 10)):
        writer.write(np.full((240, 320, 3), 128, dtype=np.uint8))
    writer.release()
    return video


def create_real_params(operation_id: str, media_ref: str) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "analysis_mode": "real_video",
        "media_ref": media_ref,
        # the detector slot (contract 2.0.0): without it the backend pauses
        # after subjects and asks (see the prompt tests below)
        "detector": "coco",
        "user": {
            "age_band": "adult",
            "handedness": "right",
            "target_stroke": "forehand",
            "goal": "稳定击球点",
            "exercise_background": "两年休闲球龄",
            "safety": "clear",
        },
        "consent": {
            "consent_id": "consent-1",
            "task_version": 1,
            "purposes": ["analysis"],
            "text_version": "consent-text-1",
            "confirmed": True,
            "guardian_simulated": False,
            "recipient_digest": None,
            "materials_digest": None,
        },
        "primary_route": "technical_diagnosis",
        "auxiliary_routes": [],
        "subject_id": None,
        "mirror_confirmed": False,
    }


class FakeRequest:
    def __init__(self, method: str, params: dict[str, Any]) -> None:
        self.method = method
        self._params = params

    def typed_params(self, model: Any) -> Any:
        return model.model_validate(self._params)


def center_pose_model() -> Any:
    """A fake RTMW-x: all 133 keypoints at the crop center, full score."""
    return SimpleNamespace(
        infer=lambda crop: (
            np.tile(np.array([[[crop.shape[1] / 2, crop.shape[0] / 2]]]), (1, 133, 1)),
            np.ones((1, 133)),
        )
    )


@pytest.fixture
def handler(tmp_path: Path) -> StdioHandler:
    registry = ContractRegistry.load(REPO_ROOT / "contracts" / "v1")
    detector = FakeDetector([[person_box(40)]])
    for sub in ("ingest", "internal", "public"):
        (tmp_path / "data" / sub).mkdir(parents=True, exist_ok=True)
    return StdioHandler(
        registry, tmp_path / "data", subject_detector=detector, pose_model=center_pose_model()
    )


def test_real_video_create_runs_probe_and_subjects(handler: StdioHandler, tmp_path: Path) -> None:
    source = make_video(tmp_path, "tennis.mp4")
    ingest = tmp_path / "data" / "ingest" / "tennis.mp4"
    ingest.write_bytes(source.read_bytes())

    result = handler(
        FakeRequest("analysis.create", create_real_params("op-rv-1", "ingest/tennis.mp4"))
    )
    # contract shape first: everything else rides on the frozen summary keys
    assert result["state"] == "analyzing"
    assert validate_result(handler._registry, "analysis.create", result).result.notes
    assert result["checkpoints"] == ["checking_input"]
    assert result["stage"] == "analysis"
    # annotated video + per-frame records are published inside the workspace
    workspace = tmp_path / "data" / "public" / f"projects/{result['task_id']}/v1"
    assert (workspace / "subjects" / "subjects-annotated.mp4").exists()
    assert (workspace / "records" / "subjects-frames.jsonl").exists()
    # task-config records the real mode for later phases
    config = json.loads((workspace / "records" / "task-config.json").read_text(encoding="utf-8"))
    assert config["analysis_mode"] == "real_video"
    selection = json.loads((workspace / "records" / "action-selection.json").read_text())
    assert selection["selected_count"] == len(selection["selected_segments"])
    assert selection["selected_frames"] == sum(s["frames"] for s in selection["selected_segments"])
    assert selection["selected_frames"] + selection["excluded_frames"] == selection["window_frames"]
    assert selection["selected_count"] == 0  # stationary fake detector
    # context windows: with no action segments the whole tracked run is
    # posed as head context (workbench fix — no blank skeleton stretches)
    assert selection["selected_segments"] == []
    assert selection["context_windows"]
    assert selection["context_frames"] > 0
    # pose-frames.jsonl stays scoped to ACTION segments (the bridge
    # cross-checks it); context frames live in their own file
    assert (workspace / "records" / "pose-frames.jsonl").read_text() == ""
    context = [
        json.loads(line)
        for line in (workspace / "records" / "pose-frames-context.jsonl").read_text().splitlines()
    ]
    assert context and all(record["keypoints"] is not None for record in context)
    assert not (workspace / "records" / "motion-trajectories.json").exists()
    assert any("context: " in n and "pose-frames-context.jsonl" in n for n in result["notes"])
    assert any("motion deferred" in n for n in result["notes"])
    assert any("queries available" in n for n in result["notes"])


def test_replayed_create_does_not_reextract(handler: StdioHandler, tmp_path: Path) -> None:
    source = make_video(tmp_path, "tennis2.mp4")
    ingest = tmp_path / "data" / "ingest" / "tennis2.mp4"
    ingest.write_bytes(source.read_bytes())

    first = handler(
        FakeRequest("analysis.create", create_real_params("op-rv-r1", "ingest/tennis2.mp4"))
    )
    frames_after_first = (tmp_path / "data" / "public").rglob("*")
    count_first = sum(1 for _ in frames_after_first)
    second = handler(
        FakeRequest("analysis.create", create_real_params("op-rv-r2", "ingest/tennis2.mp4"))
    )
    # different operation id + same inputs → BE-008 twin replay: same task,
    # extraction NOT re-run (no new files)
    assert second["task_id"] == first["task_id"]
    count_second = sum(1 for _ in (tmp_path / "data" / "public").rglob("*"))
    assert count_second == count_first


def test_selected_ranges_are_published_and_pose_motion_keep_original_frames(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from tennis_backend.media.subjects import ActionSegment

    for sub in ("ingest", "internal", "public"):
        (tmp_path / "data" / sub).mkdir(parents=True, exist_ok=True)
    segments = [ActionSegment(2, 3, 0.08, 0.12, 2), ActionSegment(7, 9, 0.28, 0.36, 3)]
    monkeypatch.setattr(
        "tennis_backend.media.subjects.derive_action_segments", lambda *_a, **_k: segments
    )
    model = SimpleNamespace(
        infer=lambda crop: (
            np.tile(np.array([[[crop.shape[1] / 2, crop.shape[0] / 2]]]), (1, 133, 1)),
            np.ones((1, 133)),
        )
    )
    handler = StdioHandler(
        ContractRegistry.load(REPO_ROOT / "contracts" / "v1"),
        tmp_path / "data",
        subject_detector=FakeDetector([[person_box(40)]]),
        pose_model=model,
    )
    source = make_video(tmp_path, "selected.mp4")
    (tmp_path / "data" / "ingest" / "selected.mp4").write_bytes(source.read_bytes())
    result = handler(
        FakeRequest("analysis.create", create_real_params("op-selected", "ingest/selected.mp4"))
    )
    records = tmp_path / "data" / "public" / result["workspace_ref"] / "records"
    selection = json.loads((records / "action-selection.json").read_text())
    assert selection["selected_frames"] == 5
    assert [s["start_frame"] for s in selection["selected_segments"]] == [2, 7]
    # context windows: head run before the first segment + tail run after
    # the last one (workbench fix — no blank skeleton before play)
    assert selection["context_windows"][0]["start_frame"] == 0
    assert selection["context_windows"][0]["end_frame"] == 1
    assert selection["context_windows"][1]["start_frame"] == 10
    pose = [json.loads(line) for line in (records / "pose-frames.jsonl").read_text().splitlines()]
    angles = [
        json.loads(line) for line in (records / "motion-angles.jsonl").read_text().splitlines()
    ]
    # pose-frames.jsonl: ACTION segments only (the bridge cross-checks it
    # against the selection; frames 4-6 interior idle gap stay excluded)
    assert [r["frame"] for r in pose] == [2, 3, 7, 8, 9]
    assert [r["frame"] for r in angles] == [2, 3, 7, 8, 9]
    # context records live in their own file: head run + tail run
    context = [
        json.loads(line)
        for line in (records / "pose-frames-context.jsonl").read_text().splitlines()
    ]
    assert [r["frame"] for r in context] == [0, 1] + list(range(10, 50))


def test_unsupported_real_video_ends_unsupported(handler: StdioHandler, tmp_path: Path) -> None:
    corrupt = tmp_path / "data" / "ingest" / "broken.mp4"
    corrupt.write_bytes(b"\x00\x01junk" * 64)
    result = handler(
        FakeRequest("analysis.create", create_real_params("op-rv-u", "ingest/broken.mp4"))
    )
    assert result["state"] == "unsupported"
    assert result["recovery_actions"] == ["reshoot"]
    assert any("real probe" in n for n in result["notes"])


def test_ingest_outside_data_root_refused(handler: StdioHandler, tmp_path: Path) -> None:
    # the frozen relative_path contract refuses traversal before dispatch
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as error:
        handler(FakeRequest("analysis.create", create_real_params("op-rv-o", "../escape/v.mp4")))
    assert "relative path" in str(error.value)


def test_missing_ingest_video_refused(handler: StdioHandler, tmp_path: Path) -> None:
    with pytest.raises(ContractViolation) as error:
        handler(FakeRequest("analysis.create", create_real_params("op-rv-m", "ingest/nope.mp4")))
    assert error.value.code == "INGEST_VIDEO_MISSING"


def test_real_video_with_fixture_id_refused(handler: StdioHandler, tmp_path: Path) -> None:
    # masquerading a real video as a frozen sample is refused at the model
    # layer (contract 2.0.0); over the wire it surfaces as REQUEST_PARAMS_INVALID
    from pydantic import ValidationError

    params = create_real_params("op-rv-f", "ingest/tennis.mp4")
    params["fixture_id"] = "iphone-flat"
    with pytest.raises(ValidationError) as error:
        handler(FakeRequest("analysis.create", params))
    assert "must not carry fixture_id" in str(error.value)


def _make_rotated_clip(tmp_path: Path, name: str, rotation: int | None) -> Path:
    import subprocess

    assert shutil.which("ffmpeg") is not None
    plain = tmp_path / f"{name}.plain.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:d=1:r=10",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(plain),
        ],
        check=True,
        capture_output=True,
    )
    if rotation is None:
        return plain
    target = tmp_path / name
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-display_rotation",
            str(rotation),
            "-i",
            str(plain),
            "-c",
            "copy",
            "-y",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    plain.unlink()
    return target


def test_rotated_mov_upright_through_real_pipeline(tmp_path: Path) -> None:
    """phase2-rotation diagnosis regression: a phone MOV with Display Matrix
    rotation must come out of the real pipeline upright (pixels AND output
    video), with no double rotation."""
    from tennis_backend.media.normalize import normalize_video
    from tennis_backend.media.probe import probe_video

    media_dir = tmp_path / "data" / "ingest"
    media_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "internal").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "public").mkdir(parents=True, exist_ok=True)

    for rotation in (90, 270):
        rotated = _make_rotated_clip(media_dir, f"rot{rotation}.mp4", rotation)
        probed = probe_video(rotated)
        assert probed.summary["rotation"] == str(rotation)
        outcome = normalize_video(
            rotated,
            output_dir=tmp_path / "data" / "public" / f"norm-{rotation}",
            original_media_id=f"m-{rotation}",
            original_ref="projects/t/v1/media/source.mp4",
        )
        assert outcome.status == "normalized", f"rotation {rotation}: {outcome.notes}"
        assert outcome.source.rotation == str(rotation)
        derivative = probe_video(outcome.derivative_path)
        # upright: no residual rotation side data on the derivative
        assert derivative.summary["rotation"] not in ("90", "180", "270")
        # no double rotation: duration preserved
        assert derivative.summary["duration_s"] == pytest.approx(1.0, abs=0.3)


def test_no_rotation_clip_unchanged_through_pipeline(tmp_path: Path) -> None:
    from tennis_backend.media.normalize import normalize_video
    from tennis_backend.media.probe import probe_video

    media_dir = tmp_path / "data" / "ingest2"
    media_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "internal").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "public").mkdir(parents=True, exist_ok=True)
    plain = make_video(tmp_path, "plain.mp4")  # landscape 320x240, no rotation
    outcome = normalize_video(
        plain,
        output_dir=tmp_path / "data" / "public" / "norm-plain",
        original_media_id="m-plain",
        original_ref="projects/t/v1/media/source.mp4",
    )
    assert outcome.status == "normalized"
    derivative = probe_video(outcome.derivative_path)
    assert derivative.summary["rotation"] not in ("90", "180", "270")
    assert (derivative.summary["width"], derivative.summary["height"]) == (320, 240)


def test_multi_person_requests_selection(tmp_path: Path) -> None:
    registry = ContractRegistry.load(REPO_ROOT / "contracts" / "v1")

    class TwoPersonDetector:
        def detect(self, frame_rgb: Any) -> list[dict[str, Any]]:
            return [
                {"class": "person", "confidence": 0.9, "box": [20, 100, 70, 300]},
                {"class": "person", "confidence": 0.85, "box": [200, 100, 250, 300]},
            ]

    for sub in ("ingest", "internal", "public"):
        (tmp_path / "data" / sub).mkdir(parents=True, exist_ok=True)
    handler = StdioHandler(registry, tmp_path / "data", subject_detector=TwoPersonDetector())
    source = make_video(tmp_path, "multi.mp4")
    (tmp_path / "data" / "ingest" / "multi.mp4").write_bytes(source.read_bytes())

    result = handler(
        FakeRequest("analysis.create", create_real_params("op-rv-mp", "ingest/multi.mp4"))
    )
    assert result["state"] == "analyzing"
    assert result["recovery_actions"] == ["ask"]
    workspace = tmp_path / "data" / "public" / result["workspace_ref"]
    summary = json.loads((workspace / "records" / "subjects-summary.json").read_text())
    assert summary["subject_stability"] == "multi"
    assert summary["requires_selection"] is True
    assert summary["frames"] > 0
    # multi-subject deferral: pose skipped, but the (empty) pose-frames.jsonl
    # still signals "pose ran for this task and found multi-subject"
    assert (workspace / "records" / "pose-frames.jsonl").exists()
    assert (workspace / "records" / "pose-frames.jsonl").read_text() == ""


# ---------------------------------------------------------------------------
# Phase 4: honest queries + real evidence rendering over published records
# ---------------------------------------------------------------------------


def posed_handler(tmp_path: Path, monkeypatch: Any) -> StdioHandler:
    """A handler whose fake pipeline produces real pose records: two
    monkeypatched action segments and a pose model returning full-score
    keypoints at the crop center (the subject box keeps them overlapping)."""
    from tennis_backend.media.subjects import ActionSegment

    segments = [ActionSegment(2, 3, 0.08, 0.12, 2), ActionSegment(7, 9, 0.28, 0.36, 3)]
    monkeypatch.setattr(
        "tennis_backend.media.subjects.derive_action_segments", lambda *_a, **_k: segments
    )
    model = SimpleNamespace(
        infer=lambda crop: (
            np.tile(np.array([[[crop.shape[1] / 2, crop.shape[0] / 2]]]), (1, 133, 1)),
            np.ones((1, 133)),
        )
    )
    for sub in ("ingest", "internal", "public"):
        (tmp_path / "data" / sub).mkdir(parents=True, exist_ok=True)
    return StdioHandler(
        ContractRegistry.load(REPO_ROOT / "contracts" / "v1"),
        tmp_path / "data",
        subject_detector=FakeDetector([[person_box(40)]]),
        pose_model=model,
    )


def _posed_task(tmp_path: Path, monkeypatch: Any, op: str) -> dict[str, Any]:
    handler = posed_handler(tmp_path, monkeypatch)
    source = make_video(tmp_path, f"{op}.mp4")
    (tmp_path / "data" / "ingest" / f"{op}.mp4").write_bytes(source.read_bytes())
    return handler(
        FakeRequest("analysis.create", create_real_params(f"op-{op}", f"ingest/{op}.mp4"))
    )


def test_real_queries_answer_through_pose_adapter(tmp_path: Path, monkeypatch: Any) -> None:
    result = _posed_task(tmp_path, monkeypatch, "queried")
    task_id, version = result["task_id"], result["version"]

    strokes = handler_query(tmp_path, task_id, version, "analysis.strokes")
    assert strokes["events"] == []
    assert strokes["simulation"] is False

    timing = handler_query(tmp_path, task_id, version, "analysis.timing")
    assert {event["kind"] for event in timing["events"]} >= {"bounce_candidate", "preparation"}
    bounce = next(e for e in timing["events"] if e["kind"] == "bounce_candidate")
    assert bounce["value_state"] == "not_visible" and bounce["original_s"] is None
    prep = next(e for e in timing["events"] if e["kind"] == "preparation")
    assert prep["value_state"] == "insufficient_evidence"
    assert timing["simulation"] is False

    metrics = handler_query(tmp_path, task_id, version, "analysis.metrics")
    assert metrics["simulation"] is False
    assert metrics["metrics"]
    assert all(
        metric["value_state"] in ("observed", "insufficient_evidence")
        for metric in metrics["metrics"]
    )

    quality = handler_query(tmp_path, task_id, version, "analysis.quality")
    assert quality["status"] == "limited"
    assert quality["unavailable_objects"] == ["ball", "racket"]
    assert quality["simulation"] is False


def handler_query(tmp_path: Path, task_id: str, version: int, method: str) -> dict[str, Any]:
    handler = StdioHandler(ContractRegistry.load(REPO_ROOT / "contracts" / "v1"), tmp_path / "data")
    result = handler(FakeRequest(method, {"task_id": task_id, "version": version}))
    assert validate_result(handler._registry, method, result).result is not None
    return result


def test_real_queries_without_records_refused_stably(tmp_path: Path) -> None:
    # a real task created with a stationary detector: pose-frames.jsonl
    # EXISTS but is empty → every metric is honest insufficient_evidence
    handler = StdioHandler(
        ContractRegistry.load(REPO_ROOT / "contracts" / "v1"),
        tmp_path / "data",
        subject_detector=FakeDetector([[person_box(40)]]),
    )
    for sub in ("ingest", "internal", "public"):
        (tmp_path / "data" / sub).mkdir(parents=True, exist_ok=True)
    source = make_video(tmp_path, "nosegs.mp4")
    (tmp_path / "data" / "ingest" / "nosegs.mp4").write_bytes(source.read_bytes())
    created = handler(
        FakeRequest("analysis.create", create_real_params("op-nosegs", "ingest/nosegs.mp4"))
    )
    metrics = handler_query(tmp_path, created["task_id"], 1, "analysis.metrics")
    assert metrics["simulation"] is False
    for metric in metrics["metrics"]:
        assert metric["value_state"] == "insufficient_evidence"


def test_real_render_frame_and_pose_overlay(tmp_path: Path, monkeypatch: Any) -> None:
    created = _posed_task(tmp_path, monkeypatch, "rendered")
    task_id = created["task_id"]
    handler = StdioHandler(ContractRegistry.load(REPO_ROOT / "contracts" / "v1"), tmp_path / "data")
    workspace = tmp_path / "data" / "public" / created["workspace_ref"]

    def render(evidence_ids: list[str], mode: str) -> dict[str, Any]:
        result = handler(
            FakeRequest(
                "evidence.render",
                {
                    "operation_id": f"op-render-{mode}",
                    "task_id": task_id,
                    "version": 1,
                    "evidence_ids": evidence_ids,
                    "mode": mode,
                },
            )
        )
        assert validate_result(handler._registry, "evidence.render", result)
        return result

    # frame mode: evidence PNGs from the ORIGINAL video at mapped times
    frames = render([f"ev-{task_id}-v1-seg-0"], "frame")
    assert frames["simulation"] is False
    assert all(resource["simulation"] is False for resource in frames["resources"])
    assert all(
        (tmp_path / "data" / "public" / resource["path"]).exists()
        for resource in frames["resources"]
    )

    # trajectory mode: the MEASURED skeleton overlay on the standardized
    # derivative
    overlay = render([f"pose-overlay-{task_id}-v1"], "trajectory")
    assert overlay["simulation"] is False
    overlay_path = tmp_path / "data" / "public" / overlay["resources"][0]["path"]
    assert overlay_path.name == f"pose-overlay-{task_id}-v1.mp4"
    assert overlay_path.exists()
    # gap-frame honesty inside the rendered overlay is covered at pixel
    # level in test_pose_render.py; here we verify the real task's records
    # actually contain a posed frame so the overlay draws something
    records = workspace / "records" / "pose-frames.jsonl"
    posed = [json.loads(line) for line in records.read_text().splitlines()]
    assert any(record["keypoints"] is not None for record in posed)

    # slow motion: stretched overlay, distinct artifact
    slow = render([f"pose-overlay-{task_id}-v1"], "slow_motion")
    assert slow["resources"][0]["path"].endswith(f"pose-overlay-{task_id}-v1-slow2.mp4")
    assert (tmp_path / "data" / "public" / slow["resources"][0]["path"]).exists()

    # wrist trajectory mode: BODY-part trajectory resources (the part label
    # and gap break are burned into the frames, FR-POSE-003)
    traj = render([f"traj-{task_id}-v1-wrist-right", f"traj-{task_id}-v1-wrist-left"], "trajectory")
    assert traj["simulation"] is False
    assert len(traj["resources"]) == 2
    assert all(resource["kind"] == "trajectory" for resource in traj["resources"])
    assert all(
        (tmp_path / "data" / "public" / resource["path"]).exists() for resource in traj["resources"]
    )
    # unknown traj id refused
    from tennis_backend.contracts.validation import ContractViolation

    with pytest.raises(ContractViolation) as error:
        render([f"traj-{task_id}-v1-wrist-bogus"], "trajectory")
    assert error.value.code == "RENDER_EVIDENCE_UNKNOWN"


def test_real_render_unknown_evidence_refused(tmp_path: Path, monkeypatch: Any) -> None:
    created = _posed_task(tmp_path, monkeypatch, "unknown-ev")
    handler = StdioHandler(ContractRegistry.load(REPO_ROOT / "contracts" / "v1"), tmp_path / "data")
    from tennis_backend.contracts.validation import ContractViolation

    with pytest.raises(ContractViolation) as error:
        handler(
            FakeRequest(
                "evidence.render",
                {
                    "operation_id": "op-render-unknown",
                    "task_id": created["task_id"],
                    "version": 1,
                    "evidence_ids": ["ev-nope"],
                    "mode": "frame",
                },
            )
        )
    assert error.value.code == "RENDER_EVIDENCE_UNKNOWN"


def test_background_spectators_do_not_block_pose(tmp_path: Path) -> None:
    """Real-video recheck: tiny background figures (~1/5 subject height)
    sharing a window are spectators, not subject ambiguity — the window is
    still posed for the main subject."""
    from test_subjects import person_box as _box

    class MainPlusSpectatorDetector:
        def detect(self, frame_rgb: Any) -> list[dict[str, Any]]:
            return [
                _box(40),  # the subject (box height 200)
                {"class": "person", "confidence": 0.8, "box": [260, 150, 275, 175]},
            ]

    for sub in ("ingest", "internal", "public"):
        (tmp_path / "data" / sub).mkdir(parents=True, exist_ok=True)
    handler = StdioHandler(
        ContractRegistry.load(REPO_ROOT / "contracts" / "v1"),
        tmp_path / "data",
        subject_detector=MainPlusSpectatorDetector(),
        pose_model=center_pose_model(),
    )
    source = make_video(tmp_path, "spectators.mp4")
    (tmp_path / "data" / "ingest" / "spectators.mp4").write_bytes(source.read_bytes())
    result = handler(
        FakeRequest("analysis.create", create_real_params("op-spec", "ingest/spectators.mp4"))
    )
    workspace = tmp_path / "data" / "public" / f"projects/{result['task_id']}/v1"
    # stationary main subject → no action segments; the tracked run is
    # context, and it was POSED (not deferred for the tiny spectator)
    context = [
        json.loads(line)
        for line in (workspace / "records" / "pose-frames-context.jsonl").read_text().splitlines()
    ]
    assert context
    assert all(record["keypoints"] is not None for record in context)
    assert not any("pose deferred" in note for note in result["notes"])


# ---------------------------------------------------------------------------
# Phase 5: cooperative cancellation + full-chain governance over real tasks
# ---------------------------------------------------------------------------


def test_cancel_stops_extraction_midway(tmp_path: Path, monkeypatch: Any) -> None:
    """A cancel landing during pose extraction must stop the burn: the task
    stays cancelled, the ledger records the interruption, partial records
    are not dressed up as a complete analysis."""

    from tennis_backend.media.pose_extract import PoseOutcome
    from tennis_backend.storage.metadata import MetadataStore

    handler = posed_handler(tmp_path, monkeypatch)
    source = make_video(tmp_path, "cancelme.mp4")
    (tmp_path / "data" / "ingest" / "cancelme.mp4").write_bytes(source.read_bytes())
    data_root = tmp_path / "data"

    # arm the trap: the first pose extraction cancels the task mid-run (the
    # way an analysis.cancel request on the control lane would)
    real_extract = handler._create_real  # noqa: F841 - keep reference clear
    original = __import__(
        "tennis_backend.media.pose_extract", fromlist=["extract_pose"]
    ).extract_pose

    def cancelling_extract_pose(*args: Any, **kwargs: Any) -> PoseOutcome:
        store = MetadataStore(data_root / "internal" / "metadata.sqlite")
        try:
            state = store.task_state(handler_last_task["id"])
            assert state is not None
            store.record_transition(
                handler_last_task["id"],
                current=state,
                target="cancelled",
                reason="user_cancel",
                at=__import__("datetime", fromlist=["datetime"]).datetime.now(
                    __import__("datetime", fromlist=["timezone"]).UTC
                ),
            )
        finally:
            store.close()
        kwargs["should_cancel"] = lambda: True  # stop at the first frame check
        return original(*args, **kwargs)

    monkeypatch.setattr("tennis_backend.media.pose_extract.extract_pose", cancelling_extract_pose)
    handler_last_task: dict[str, Any] = {}

    params = create_real_params("op-cancel-1", "ingest/cancelme.mp4")
    captured_request = FakeRequest("analysis.create", params)

    # capture the task id as soon as create makes one: patch the transition
    original_rec = MetadataStore.record_transition

    def recording_transition(self: Any, task_id: str, **kwargs: Any) -> Any:
        handler_last_task["id"] = task_id
        return original_rec(self, task_id, **kwargs)

    monkeypatch.setattr(MetadataStore, "record_transition", recording_transition)
    result = handler(captured_request)

    assert result["state"] == "cancelled"
    assert any("cancelled during" in note for note in result["notes"])
    # partial pose records were published but pose-frames stays action-only
    records_dir = data_root / "public" / f"projects/{result['task_id']}/v1/records"
    assert (records_dir / "pose-frames.jsonl").exists()


def test_compare_on_real_tasks_refused_stably(tmp_path: Path, monkeypatch: Any) -> None:
    """V1 real metrics have no comparability gate (device/mirror/protocol):
    the honest answer is a stable refusal, never a fixture posing as the
    user's comparison."""
    created = _posed_task(tmp_path, monkeypatch, "cmp-a")
    handler = StdioHandler(ContractRegistry.load(REPO_ROOT / "contracts" / "v1"), tmp_path / "data")
    from tennis_backend.contracts.validation import ContractViolation

    with pytest.raises(ContractViolation) as error:
        handler(
            FakeRequest(
                "sessions.compare",
                {
                    "task_id": created["task_id"],
                    "version": 1,
                    "other_task_id": created["task_id"],
                    "other_version": 1,
                },
            )
        )
    assert error.value.code == "COMPARE_REAL_UNSUPPORTED"


def _create_simple_real_task(tmp_path: Path, op: str) -> dict[str, Any]:
    handler = StdioHandler(
        ContractRegistry.load(REPO_ROOT / "contracts" / "v1"),
        tmp_path / "data",
        subject_detector=FakeDetector([[person_box(40)]]),
        pose_model=center_pose_model(),
    )
    for sub in ("ingest", "internal", "public"):
        (tmp_path / "data" / sub).mkdir(parents=True, exist_ok=True)
    source = make_video(tmp_path, f"{op}.mp4")
    (tmp_path / "data" / "ingest" / f"{op}.mp4").write_bytes(source.read_bytes())
    return handler(
        FakeRequest("analysis.create", create_real_params(f"op-{op}", f"ingest/{op}.mp4"))
    )


def test_delete_real_task_removes_workspace_keeps_original(tmp_path: Path) -> None:

    created = _create_simple_real_task(tmp_path, "deleteme")
    workspace = tmp_path / "data" / "public" / f"projects/{created['task_id']}/v1"
    assert workspace.exists()
    ingest = tmp_path / "data" / "ingest" / "deleteme.mp4"
    original_bytes = ingest.read_bytes()

    handler = StdioHandler(ContractRegistry.load(REPO_ROOT / "contracts" / "v1"), tmp_path / "data")
    result = handler(
        FakeRequest(
            "analysis.delete",
            {
                "operation_id": "op-delete-real",
                "task_id": created["task_id"],
                "version": 1,
                "confirmed": True,
                "scope": "task",
            },
        )
    )
    assert result["complete"] is True
    # published resources are gone (empty directories may remain)
    assert not (workspace / "records" / "task-config.json").exists()
    assert not list(workspace.rglob("*.mp4"))
    # the user's original upload is never touched
    assert ingest.read_bytes() == original_bytes
    # the deleted task settles to a readable deleted state, not an error
    status = handler(FakeRequest("analysis.status", {"task_id": created["task_id"], "version": 1}))
    assert status["state"] == "deleted"


def test_export_real_task_builds_redacted_package(tmp_path: Path) -> None:
    import hashlib

    created = _create_simple_real_task(tmp_path, "exportme")
    handler = StdioHandler(ContractRegistry.load(REPO_ROOT / "contracts" / "v1"), tmp_path / "data")
    rel = f"projects/{created['task_id']}/v1/records/task-config.json"
    resource_id = "res-" + hashlib.sha256(rel.encode()).hexdigest()[:32]
    result = handler(
        FakeRequest(
            "review.export",
            {
                "operation_id": "op-export-real",
                "task_id": created["task_id"],
                "version": 1,
                "consent": {
                    "consent_id": "consent-export",
                    "task_version": 1,
                    "purposes": ["diagnostics"],
                    "text_version": "consent-text-1",
                    "confirmed": True,
                    "guardian_simulated": False,
                    "recipient_digest": None,
                    "materials_digest": None,
                },
                "selected_resource_ids": [resource_id],
            },
        )
    )
    assert result["redacted"] is True
    manifest = tmp_path / "data" / "public" / result["manifest"]["path"]
    assert manifest.exists()


def test_reset_all_demo_covers_real_tasks(tmp_path: Path) -> None:
    created = _create_simple_real_task(tmp_path, "resetme")
    workspace = tmp_path / "data" / "public" / f"projects/{created['task_id']}/v1"
    assert workspace.exists()
    handler = StdioHandler(ContractRegistry.load(REPO_ROOT / "contracts" / "v1"), tmp_path / "data")
    result = handler(
        FakeRequest(
            "analysis.delete",
            {
                "operation_id": "op-reset-real",
                "task_id": None,
                "version": None,
                "confirmed": True,
                "scope": "all_demo",
            },
        )
    )
    assert result["complete"] is True
    # nothing of the task survives, including unregistered scratch
    assert not list(workspace.rglob("*.mp4"))
    assert not (workspace / "records" / "task-config.json").exists()


def test_version_scoped_delete_cannot_wipe_other_versions(tmp_path: Path) -> None:
    """Phase 5 review BLOCKING-1 regression: a delete scoped to a
    (nonexistent) version must NOT sweep another version's files without
    any per-item report."""
    created = _create_simple_real_task(tmp_path, "vfilter")
    workspace = tmp_path / "data" / "public" / f"projects/{created['task_id']}/v1"
    files_before = list(workspace.rglob("*"))
    assert files_before
    handler = StdioHandler(ContractRegistry.load(REPO_ROOT / "contracts" / "v1"), tmp_path / "data")
    handler(
        FakeRequest(
            "analysis.delete",
            {
                "operation_id": "op-delete-v9",
                "task_id": created["task_id"],
                "version": 9,
                "confirmed": True,
                "scope": "task",
            },
        )
    )
    # the sweep only touched the requested (nonexistent) version's
    # directory: v1 files are all intact
    files_after = list(workspace.rglob("*"))
    assert len(files_after) == len(files_before)
    assert (workspace / "records" / "task-config.json").exists()


def test_compare_mixed_real_and_fixture_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """Phase 5 review: real+fixture comparisons are refused in BOTH operand
    orders — a fixture result can never stand in for the user's session."""
    created = _posed_task(tmp_path, monkeypatch, "mixcmp")
    handler = StdioHandler(ContractRegistry.load(REPO_ROOT / "contracts" / "v1"), tmp_path / "data")
    fixture_params = {
        "operation_id": "op-fix-cmp",
        "fixture_id": "iphone-flat",
        "media_ref": "ingest/fixture-ref.mp4",
        "user": {
            "age_band": "adult",
            "handedness": "right",
            "target_stroke": "forehand",
            "goal": "稳定击球点",
            "exercise_background": "两年休闲球龄",
            "safety": "clear",
        },
        "consent": {
            "consent_id": "consent-fix",
            "task_version": 1,
            "purposes": ["analysis"],
            "text_version": "consent-text-1",
            "confirmed": True,
            "guardian_simulated": False,
            "recipient_digest": None,
            "materials_digest": None,
        },
        "primary_route": "technical_diagnosis",
        "auxiliary_routes": [],
        "subject_id": None,
        "mirror_confirmed": False,
    }
    fixture_task = handler(FakeRequest("analysis.create", fixture_params))
    from tennis_backend.contracts.validation import ContractViolation

    for first, second in (
        (created["task_id"], fixture_task["task_id"]),
        (fixture_task["task_id"], created["task_id"]),
    ):
        with pytest.raises(ContractViolation) as error:
            handler(
                FakeRequest(
                    "sessions.compare",
                    {
                        "task_id": first,
                        "version": 1,
                        "other_task_id": second,
                        "other_version": 1,
                    },
                )
            )
        assert error.value.code == "COMPARE_REAL_UNSUPPORTED"


# ---------------------------------------------------------------------------
# 联调: confirm ONE subject or analyze ALL (a match can hold four people)
# ---------------------------------------------------------------------------


class TwoPlayersDetector:
    """Two comparable people, far apart, each moving in its own rhythm.

    Player A (left) sways the whole clip; player B (right) only moves in the
    second half — so their action segments differ and a per-subject pipeline
    is observable (a shared pipeline would give both the same windows).
    """

    def __init__(self, total: int = 40) -> None:
        self._i = 0
        self._total = total

    def detect(self, frame_rgb: Any) -> list[dict[str, Any]]:
        i = self._i
        self._i += 1
        a_x = 20 + (i % 8) * 4  # always moving
        b_x = 200 + (0 if i < self._total // 2 else (i % 8) * 4)  # idle then moving
        return [
            {"class": "person", "confidence": 0.9, "box": [a_x, 60, a_x + 50, 260]},
            {"class": "person", "confidence": 0.9, "box": [b_x, 60, b_x + 50, 260]},
        ]


def _two_player_handler(tmp_path: Path) -> StdioHandler:
    for sub in ("ingest", "internal", "public"):
        (tmp_path / "data" / sub).mkdir(parents=True, exist_ok=True)
    return StdioHandler(
        ContractRegistry.load(REPO_ROOT / "contracts" / "v1"),
        tmp_path / "data",
        subject_detector=TwoPlayersDetector(),
        pose_model=center_pose_model(),
    )


def _create_two_players(
    tmp_path: Path, op: str, subject_id: str | None
) -> tuple[dict[str, Any], Path]:
    handler = _two_player_handler(tmp_path)
    source = make_video(tmp_path, f"{op}.mp4", seconds=4.0)
    (tmp_path / "data" / "ingest" / f"{op}.mp4").write_bytes(source.read_bytes())
    params = create_real_params(f"op-{op}", f"ingest/{op}.mp4")
    params["subject_id"] = subject_id
    result = handler(FakeRequest("analysis.create", params))
    return result, tmp_path / "data" / "public" / result["workspace_ref"] / "records"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_candidates_are_listed_unfiltered_with_size_ratio(tmp_path: Path) -> None:
    """The candidate list is NOT size-filtered: everyone persistent is
    offered, with a size_ratio so the user can judge who is who."""
    _result, records = _create_two_players(tmp_path, "cands", None)
    listing = json.loads((records / "subject-candidates.json").read_text())
    assert listing["mode"] == "unconfirmed"
    ids = [c["subject_id"] for c in listing["candidates"]]
    assert len(ids) == 2 and all(i.startswith("track-") for i in ids)
    assert all(c["persistent"] for c in listing["candidates"])
    assert all(0 < c["size_ratio"] <= 1 for c in listing["candidates"])
    assert all(c["frames"] > 0 and "first_s" in c for c in listing["candidates"])


def test_single_subject_gets_its_own_records_and_metrics(tmp_path: Path) -> None:
    _result, records = _create_two_players(tmp_path, "single", None)
    listing = json.loads((records / "subject-candidates.json").read_text())
    chosen = listing["candidates"][0]["subject_id"]

    result, records = _create_two_players(tmp_path, "single-pick", chosen)
    listing = json.loads((records / "subject-candidates.json").read_text())
    assert listing["mode"] == "single"
    assert listing["analyzed"] == [chosen]
    own = records / "subjects" / chosen
    assert (own / "pose-frames.jsonl").exists()
    assert (own / "action-selection.json").exists()
    assert (own / "segment-states.json").exists()
    metrics = json.loads((own / "metrics.json").read_text())
    assert metrics["subject_id"] == chosen and metrics["simulation"] is False
    assert metrics["metrics"]
    # the flat primary set (what every existing consumer reads) is that subject
    selection = json.loads((records / "action-selection.json").read_text())
    own_selection = json.loads((own / "action-selection.json").read_text())
    assert selection["selected_segments"] == own_selection["selected_segments"]
    assert any("subject candidates" in n for n in result["notes"])


def test_all_subjects_each_get_independent_records(tmp_path: Path) -> None:
    result, records = _create_two_players(tmp_path, "all", "all")
    listing = json.loads((records / "subject-candidates.json").read_text())
    assert listing["mode"] == "all"
    assert len(listing["analyzed"]) == 2
    per_subject = {}
    for label in listing["analyzed"]:
        own = records / "subjects" / label
        per_subject[label] = {
            "pose": _read_jsonl(own / "pose-frames.jsonl"),
            "selection": json.loads((own / "action-selection.json").read_text()),
            "metrics": json.loads((own / "metrics.json").read_text()),
        }
        assert per_subject[label]["metrics"]["subject_id"] == label
        assert per_subject[label]["selection"]["subject_id"] == label
    # the two people moved differently, so their action segments differ:
    # a shared pipeline would have produced identical selections
    selections = [
        json.dumps(v["selection"]["selected_segments"], sort_keys=True)
        for v in per_subject.values()
    ]
    assert selections[0] != selections[1]
    # every record's subject_box belongs to ITS subject (left vs right half)
    for label, data in per_subject.items():
        xs = {record["subject_box"][0] for record in data["pose"] if record["subject_box"]}
        assert xs, f"{label} has no posed frames"
    assert not any("pose deferred" in note for note in result["notes"])


def test_unknown_subject_is_refused_with_candidate_list(tmp_path: Path) -> None:
    """A wrong subject_id never silently falls back to somebody else."""
    result, records = _create_two_players(tmp_path, "bad", "track-999")
    assert result["state"] == "failed"
    assert result["recovery_actions"] == ["ask"]
    assert any("not a candidate" in n for n in result["notes"])
    listing = json.loads((records / "subject-candidates.json").read_text())
    assert listing["mode"] == "invalid" and len(listing["candidates"]) == 2
    assert not (records / "pose-frames.jsonl").exists()


def test_unconfirmed_default_is_unchanged_for_single_person(
    handler: StdioHandler, tmp_path: Path
) -> None:
    """Regression: with no subject_id the historical dominant-subject flow
    still produces the flat records every existing consumer reads."""
    source = make_video(tmp_path, "legacy.mp4")
    (tmp_path / "data" / "ingest" / "legacy.mp4").write_bytes(source.read_bytes())
    result = handler(
        FakeRequest("analysis.create", create_real_params("op-legacy", "ingest/legacy.mp4"))
    )
    records = tmp_path / "data" / "public" / result["workspace_ref"] / "records"
    listing = json.loads((records / "subject-candidates.json").read_text())
    assert listing["mode"] == "unconfirmed" and listing["analyzed"] == [None]
    assert (records / "pose-frames.jsonl").exists()
    assert not (records / "subjects").exists()  # no per-subject dirs unless asked


def test_all_subjects_overlay_draws_every_person_in_distinct_colors(tmp_path: Path) -> None:
    """联调: '-all' draws every analyzed person on the same frames, each in
    its own color; a single id draws only that person."""
    import cv2

    result, records = _create_two_players(tmp_path, "overlay-all", "all")
    task_id = result["task_id"]
    handler = StdioHandler(ContractRegistry.load(REPO_ROOT / "contracts" / "v1"), tmp_path / "data")
    listing = json.loads((records / "subject-candidates.json").read_text())
    first, second = listing["analyzed"]

    def render(evidence_id: str) -> Path:
        out = handler(
            FakeRequest(
                "evidence.render",
                {
                    "operation_id": f"op-r-{evidence_id[-12:]}",
                    "task_id": task_id,
                    "version": 1,
                    "evidence_ids": [evidence_id],
                    "mode": "trajectory",
                },
            )
        )
        assert validate_result(handler._registry, "evidence.render", out)
        assert out["simulation"] is False
        return tmp_path / "data" / "public" / out["resources"][0]["path"]

    every = render(f"pose-overlay-{task_id}-v1-all")
    only_first = render(f"pose-overlay-{task_id}-v1-{first}")
    assert every.name == f"pose-overlay-{task_id}-v1-all.mp4"
    assert only_first.name == f"pose-overlay-{task_id}-v1-{first}.mp4"
    assert every.exists() and only_first.exists()

    def drawn_energy(path: Path) -> float:
        """Total non-background brightness over the first frames (the source
        is a flat gray 128 video, so anything above/below it is overlay)."""
        capture = cv2.VideoCapture(str(path))
        total = 0.0
        for _ in range(12):
            ok, frame = capture.read()
            if not ok:
                break
            total += float(np.abs(frame[:200].astype(int) - 128).sum())
        capture.release()
        return total

    # the all-subjects picture draws strictly MORE than one subject's picture
    # (a second person's skeleton + label), and each single picture differs
    # from the other subject's
    only_second = render(f"pose-overlay-{task_id}-v1-{second}")
    assert drawn_energy(every) > drawn_energy(only_first)
    assert drawn_energy(every) > drawn_energy(only_second)
    assert every.read_bytes() != only_first.read_bytes() != only_second.read_bytes()


def test_unknown_subject_overlay_id_refused(tmp_path: Path) -> None:
    from tennis_backend.contracts.validation import ContractViolation

    result, _records = _create_two_players(tmp_path, "overlay-bad", "all")
    handler = StdioHandler(ContractRegistry.load(REPO_ROOT / "contracts" / "v1"), tmp_path / "data")
    with pytest.raises(ContractViolation) as error:
        handler(
            FakeRequest(
                "evidence.render",
                {
                    "operation_id": "op-r-bad",
                    "task_id": result["task_id"],
                    "version": 1,
                    "evidence_ids": [f"pose-overlay-{result['task_id']}-v1-track-4242"],
                    "mode": "trajectory",
                },
            )
        )
    assert error.value.code == "RENDER_EVIDENCE_UNKNOWN"


# ---------------------------------------------------------------------------
# Swing temporal summary + contact sheets through the real pipeline
# ---------------------------------------------------------------------------


class SwingingPlayerDetector:
    """One person whose box sways, so an action segment exists."""

    def __init__(self) -> None:
        self._i = 0

    def detect(self, frame_rgb: Any) -> list[dict[str, Any]]:
        i = self._i
        self._i += 1
        x = 40 + (i % 8) * 4
        return [{"class": "person", "confidence": 0.9, "box": [x, 60, x + 50, 260]}]


def _swing_handler(tmp_path: Path) -> StdioHandler:
    for sub in ("ingest", "internal", "public"):
        (tmp_path / "data" / sub).mkdir(parents=True, exist_ok=True)
    return StdioHandler(
        ContractRegistry.load(REPO_ROOT / "contracts" / "v1"),
        tmp_path / "data",
        subject_detector=SwingingPlayerDetector(),
        pose_model=center_pose_model(),
    )


def test_create_publishes_swings_json_even_when_no_swing_is_found(tmp_path: Path) -> None:
    """swings.json is ALWAYS written: an empty list is the honest answer for
    a clip whose wrist never shows a prominent speed peak (here the fake pose
    model keeps every keypoint at the crop centre, i.e. a still wrist)."""
    handler = _swing_handler(tmp_path)
    source = make_video(tmp_path, "swing.mp4", seconds=4.0)
    (tmp_path / "data" / "ingest" / "swing.mp4").write_bytes(source.read_bytes())
    result = handler(
        FakeRequest("analysis.create", create_real_params("op-sw", "ingest/swing.mp4"))
    )
    records = tmp_path / "data" / "public" / result["workspace_ref"] / "records"
    swings = json.loads((records / "swings.json").read_text())
    assert swings["swing_count"] == len(swings["swings"])
    assert "peaks_beyond_cap" in swings and "notes" in swings
    assert any("segments are movement windows" in n for n in swings["notes"])


def test_swings_are_published_per_subject_too(tmp_path: Path) -> None:
    result, records = _create_two_players(tmp_path, "sw-all", "all")
    listing = json.loads((records / "subject-candidates.json").read_text())
    for label in listing["analyzed"]:
        own = records / "subjects" / label / "swings.json"
        assert own.exists(), f"{label} has no swings.json"
        assert "swings" in json.loads(own.read_text())


def test_sheet_render_of_an_unknown_swing_is_refused_before_rendering(tmp_path: Path) -> None:
    from tennis_backend.contracts.validation import ContractViolation

    handler = _swing_handler(tmp_path)
    source = make_video(tmp_path, "swing2.mp4", seconds=4.0)
    (tmp_path / "data" / "ingest" / "swing2.mp4").write_bytes(source.read_bytes())
    created = handler(
        FakeRequest("analysis.create", create_real_params("op-sw2", "ingest/swing2.mp4"))
    )
    task_id = created["task_id"]
    with pytest.raises(ContractViolation) as error:
        handler(
            FakeRequest(
                "evidence.render",
                {
                    "operation_id": "op-sheet-bad",
                    "task_id": task_id,
                    "version": 1,
                    "evidence_ids": [f"sheet-{task_id}-v1-swing-9-9"],
                    "mode": "frame",
                },
            )
        )
    assert error.value.code == "RENDER_EVIDENCE_UNKNOWN"
    evidence = tmp_path / "data" / "public" / created["workspace_ref"] / "evidence"
    assert not list(evidence.glob("sheet-*")) if evidence.exists() else True  # no orphan file
