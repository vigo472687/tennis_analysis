from __future__ import annotations

from pathlib import Path

import pytest

from tennis_backend.experience.runner import read_json, run_stage, write_json


def test_pose_requires_successful_inspection_and_verified_model(tmp_path: Path) -> None:
    write_json(tmp_path / "job.json", {"handedness": "right"})
    with pytest.raises(ValueError, match="INSPECTION_REQUIRED"):
        run_stage(tmp_path, "pose", tmp_path / "missing.task")
    (tmp_path / "preview.mp4").write_bytes(b"not an actual video")
    with pytest.raises(ValueError, match="POSE_MODEL_UNAVAILABLE"):
        run_stage(tmp_path, "pose", tmp_path / "missing.task")
    assert not (tmp_path / "pose.json").exists()


def test_result_write_is_atomic_and_preserves_unicode(tmp_path: Path) -> None:
    location = tmp_path / "result.json"
    write_json(location, {"message": "视频", "simulation": False})
    assert read_json(location)["simulation"] is False
    assert read_json(location)["message"] == "视频"
    assert not location.with_suffix(".json.tmp").exists()


def test_unknown_stage_cannot_create_a_successful_result(tmp_path: Path) -> None:
    write_json(tmp_path / "job.json", {})
    with pytest.raises(ValueError, match="STAGE_UNKNOWN"):
        run_stage(tmp_path, "unknown", tmp_path / "missing.task")
    assert not (tmp_path / "result.json").exists()
