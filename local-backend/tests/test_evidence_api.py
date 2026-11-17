"""``evidence.get`` and ``analysis.estimate_storage`` over the stdio handler.

The model inference is patched out; everything around it — the contract
validation, the detector prompt flow, the evidence store, the frame images on
disk and the retrieval — runs for real. These cases pin what the expert gets:

- create without a ``detector`` asks (ReAct) instead of guessing, counts the
  prompts per media_ref, and after three applies the measured fallback;
- with a detector the create stores per-frame evidence and writes the images
  of the retained ranges;
- ``evidence.get`` returns resources (not raw pixels) and stays inside what
  was retained;
- ``estimate_storage`` flags itself as an estimate and demands confirmation
  for ``full``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pytest

from tennis_backend.cli import StdioHandler
from tennis_backend.contracts.validation import ContractRegistry, validate_result
from test_real_video import FakeRequest, make_video
from test_subjects import FakeDetector, person_box

REPO_ROOT = Path(__file__).resolve().parents[2]


def near_scan() -> Any:
    """Ball approaching a racket, closest at frame 30 (triangular profile).

    The ball stays inside the 320x240 frame on every frame: a real detection
    never sits at a negative coordinate, and the close-up writer must be able
    to crop around it.
    """
    from tennis_backend.media.ball_detect import BallRacketScan

    scan = BallRacketScan(fps=10.0, width=320, height=240)
    for frame in range(60):
        distance = min(abs(frame - 30) * 2.0, 60.0)
        ball = [160.0 - distance, 110.0, 160.0 - distance + 8.0, 118.0, 0.9]
        scan.frames[frame] = {
            "ball": [ball],
            "racket": [[130.0, 100.0, 190.0, 150.0, 0.9]],
        }
    return scan


@pytest.fixture
def handler(tmp_path: Path) -> StdioHandler:
    registry = ContractRegistry.load(REPO_ROOT / "contracts" / "v1")
    detector = FakeDetector([[person_box(40)]])
    for sub in ("ingest", "internal", "public"):
        (tmp_path / "data" / sub).mkdir(parents=True, exist_ok=True)
    return StdioHandler(registry, tmp_path / "data", subject_detector=detector, pose_model=None)


def request(method: str, params: dict[str, Any], *, simulation: bool = False) -> FakeRequest:
    return SimpleNamespace(
        method=method,
        typed_params=lambda model: model.model_validate(params),
        simulation=simulation,
    )  # type: ignore[return-value]


def create_params(operation_id: str, media_ref: str, **extra: Any) -> dict[str, Any]:
    from test_real_video import create_real_params

    params = {**create_real_params(operation_id, media_ref), **extra}
    # the shared helper defaults to detector="coco"; the prompt tests need the
    # slot empty, so it is only sent when a test asked for it
    if "detector" not in extra:
        params.pop("detector", None)
    return params


@pytest.fixture
def video(tmp_path: Path) -> Path:
    source = make_video(tmp_path, "tennis.mp4")
    ingest = tmp_path / "data" / "ingest" / "tennis.mp4"
    ingest.write_bytes(source.read_bytes())
    return ingest


def fake_ball_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[int]]:
    """Replace model inference with stored fake detections and real images.

    The patched ``run`` reuses the real builder and image writer, so the
    store, the retention planning and the files on disk are the production
    code paths; only the detectors are fake.
    """
    from tennis_backend.application import evidence_run
    from tennis_backend.application.evidence_builder import build_evidence
    from tennis_backend.evidence.frame_images import retained_only, write_frames

    calls: list[dict[str, Any]] = []

    def fake_run(video: Path, **kwargs: Any) -> Any:
        calls.append({**kwargs, "detector": kwargs.get("detector")})
        scan = near_scan()
        summary = build_evidence(
            kwargs["store"],
            scan,
            task_id=kwargs["task_id"],
            version=kwargs["version"],
            route=kwargs["route"],
            retain_mode=kwargs["retain_mode"],
            uploaded_at=kwargs["uploaded_at"],
        )
        ranges = kwargs["store"].retained_ranges(kwargs["task_id"], kwargs["version"])
        images = write_frames(
            video,
            kwargs["public_root"],
            kwargs["relative_evidence_dir"],
            retained_only(
                kwargs["store"].between(kwargs["task_id"], kwargs["version"], 0, 10**9), ranges
            ),
            far=False,
        )
        return SimpleNamespace(
            detector=kwargs["detector"],
            fallback=bool(kwargs.get("detector") is None),
            summary=summary,
            images=images,
            notes=["fake run"],
        )

    monkeypatch.setattr(evidence_run, "run", fake_run)
    return calls


# ----------------------------------------------------------------------
# detector prompt flow (contract 2.0.0 detector slot)
# ----------------------------------------------------------------------


def test_create_without_detector_asks_and_does_not_guess(
    handler: StdioHandler, tmp_path: Path, video: Path
) -> None:
    result = handler(request("analysis.create", create_params("op-ask-1", "ingest/tennis.mp4")))
    notes = " ".join(result["notes"])
    assert "需要指定检测器" in notes
    task_id = result["task_id"]
    prompt = tmp_path / "data" / "public" / f"projects/{task_id}/v1/records/detector-prompt.json"
    payload = json.loads(prompt.read_text(encoding="utf-8"))
    assert payload["prompts_used"] == 1
    assert payload["options"] == ["coco", "wasb"]
    # the pose pipeline is paused: no skeleton records yet
    assert not (
        tmp_path / "data" / "public" / f"projects/{task_id}/v1/records/pose-frames.jsonl"
    ).exists()


def test_frames_without_a_detected_person_do_not_crash_the_detector_flow(
    tmp_path: Path,
) -> None:
    """A frame where the person detector found nobody has no height to take.

    The real-model smoke hit this: ``max()`` over an empty box list crashed
    the create before the prompt could be issued.
    """
    from tennis_backend.cli import StdioHandler
    from tennis_backend.contracts.validation import ContractRegistry
    from test_subjects import FakeDetector

    registry = ContractRegistry.load(REPO_ROOT / "contracts" / "v1")
    detector = FakeDetector([[]])  # every frame: no person at all
    for sub in ("ingest", "internal", "public"):
        (tmp_path / "data" / sub).mkdir(parents=True, exist_ok=True)
    ingest = tmp_path / "data" / "ingest" / "tennis.mp4"
    ingest.write_bytes(make_video(tmp_path, "tennis.mp4").read_bytes())
    plain = StdioHandler(registry, tmp_path / "data", subject_detector=detector, pose_model=None)
    result = plain(request("analysis.create", create_params("op-nob-1", "ingest/tennis.mp4")))
    notes = " ".join(result.get("notes", []))
    assert "需要指定检测器" in notes
    prompt = (
        tmp_path
        / "data"
        / "public"
        / f"projects/{result['task_id']}/v1/records/detector-prompt.json"
    )
    payload = json.loads(prompt.read_text(encoding="utf-8"))
    assert payload["subject_height_ratio"] is None


def test_prompts_count_per_media_ref_and_third_time_stops_asking(
    handler: StdioHandler, video: Path
) -> None:
    # the idempotency layer replays a create whose inputs are identical to a
    # settled one, so the repeats below ride the replayed path and must still
    # advance the prompt count
    seen: list[str] = []
    for index in range(1, 4):
        result = handler(
            request("analysis.create", create_params(f"op-ask-{index}", "ingest/tennis.mp4"))
        )
        seen.append(" ".join(result.get("notes", [])))
    assert "需要指定检测器（第 1/3 次询问）" in seen[0]
    assert "需要指定检测器" in seen[1]
    assert "已询问 3 次" in seen[2], seen


# ----------------------------------------------------------------------
# create with a detector stores evidence; evidence.get retrieves it
# ----------------------------------------------------------------------


def test_create_with_detector_stores_evidence_and_get_returns_it(
    handler: StdioHandler,
    tmp_path: Path,
    video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ball_run(monkeypatch)
    created = handler(
        request(
            "analysis.create",
            create_params("op-ev-1", "ingest/tennis.mp4", detector="coco"),
        )
    )
    validate_result(handler._registry, "analysis.create", created)
    task_id = created["task_id"]
    store_path = tmp_path / "data" / "internal" / "evidence.sqlite"
    assert store_path.is_file()

    got = handler(
        request(
            "evidence.get",
            {"task_id": task_id, "version": 1, "frame_start": 25, "frame_end": 35},
        )
    )
    validate_result(handler._registry, "evidence.get", got)
    assert got["reason"] is None
    assert got["frames"], "retained range must carry images"
    for frame in got["frames"]:
        assert frame["image"]["kind"] == "evidence_frame"
        assert frame["image"]["path"].endswith(".jpg")
        assert len(frame["image"]["sha256"]) == 64
    # the whole-frame image exists under the authorized root
    first = got["frames"][0]["image"]["path"]
    assert (tmp_path / "data" / "public" / first).is_file()


def test_evidence_get_by_candidate_returns_window_and_candidates(
    handler: StdioHandler,
    tmp_path: Path,
    video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ball_run(monkeypatch)
    created = handler(
        request(
            "analysis.create",
            create_params("op-ev-2", "ingest/tennis.mp4", detector="coco"),
        )
    )
    task_id = created["task_id"]
    from tennis_backend.storage.evidence_store import EvidenceStore

    with EvidenceStore(tmp_path / "data" / "internal" / "evidence.sqlite") as store:
        stored = store.candidates(task_id, 1)
    assert stored, "the fake scan has an anchor at frame 30"
    got = handler(
        request(
            "evidence.get",
            {"task_id": task_id, "version": 1, "candidate_id": stored[0].candidate_id},
        )
    )
    validate_result(handler._registry, "evidence.get", got)
    assert got["candidates"], "the candidate list rides along for the workbench"
    assert got["candidates"][0]["basis"] == "ball_racket"
    assert got["window_level"] == stored[0].window_level
    assert got["trajectory"], "ball points are normalised into the trajectory"
    point = got["trajectory"][0]
    assert 0.0 <= point["x"] <= 1.0 and 0.0 <= point["y"] <= 1.0
    assert point["source"] == "detected"


def test_evidence_get_outside_retention_is_refused_in_chinese(
    handler: StdioHandler, tmp_path: Path, video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ball_run(monkeypatch)
    created = handler(
        request(
            "analysis.create",
            create_params("op-ev-3", "ingest/tennis.mp4", detector="coco"),
        )
    )
    got = handler(
        request(
            "evidence.get",
            {"task_id": created["task_id"], "version": 1, "frame_start": 0, "frame_end": 5},
        )
    )
    assert got["reason"] == "not_retained"
    assert got["frames"] == []
    assert got["message"]


def test_evidence_get_for_a_task_without_ball_evidence(
    handler: StdioHandler, tmp_path: Path, video: Path
) -> None:
    created = handler(
        request("analysis.create", create_params("op-ev-4", "ingest/tennis.mp4", detector="coco"))
    )
    got = handler(
        request(
            "evidence.get",
            {"task_id": created["task_id"], "version": 1, "frame_start": 0, "frame_end": 3},
        )
    )
    validate_result(handler._registry, "evidence.get", got)
    assert got["reason"] in ("not_retained", "expired")
    assert got["frames"] == []


# ----------------------------------------------------------------------
# storage estimate
# ----------------------------------------------------------------------


def test_estimate_storage_is_an_estimate_and_full_needs_confirmation(
    handler: StdioHandler, tmp_path: Path, video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tennis_backend.application import evidence_run

    monkeypatch.setattr(evidence_run, "detect_video", lambda *_a, **_k: near_scan())
    created = handler(
        request(
            "analysis.create",
            create_params("op-est-1", "ingest/tennis.mp4", detector="coco"),
        )
    )
    task_id = created["task_id"]

    trimmed = handler(
        request(
            "analysis.estimate_storage",
            {"task_id": task_id, "version": 1, "retain_mode": "route_trimmed"},
        )
    )
    validate_result(handler._registry, "analysis.estimate_storage", trimmed)
    assert trimmed["mode"] == "route_trimmed"
    assert trimmed["requires_confirmation"] is False
    assert trimmed["retained_frames"] > 0
    assert trimmed["estimated_bytes"] == trimmed["retained_frames"] * 29_000

    full = handler(
        request(
            "analysis.estimate_storage",
            {"task_id": task_id, "version": 1, "retain_mode": "full"},
        )
    )
    validate_result(handler._registry, "analysis.estimate_storage", full)
    assert full["requires_confirmation"] is True
    assert full["ranges"] == [{"start": 0, "end": 59}]


def test_estimate_storage_without_a_video_is_refused(
    handler: StdioHandler,
    tmp_path: Path,
    video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tennis_backend.contracts.validation import ContractViolation

    fake_ball_run(monkeypatch)
    created = handler(
        request("analysis.create", create_params("op-est-2", "ingest/tennis.mp4", detector="coco"))
    )
    (tmp_path / "data" / "ingest" / "tennis.mp4").unlink()
    with pytest.raises(ContractViolation) as error:
        handler(
            request(
                "analysis.estimate_storage",
                {"task_id": created["task_id"], "version": 1, "retain_mode": "full"},
            )
        )
    assert error.value.code == "INGEST_VIDEO_MISSING"


_ = np, cv2  # imported for parity with sibling test modules
