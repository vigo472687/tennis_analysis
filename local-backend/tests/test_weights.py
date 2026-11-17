"""Model weights are resolved inside the project and never downloaded silently."""

from __future__ import annotations

from pathlib import Path

import pytest

from tennis_backend.domain.models import DomainViolation
from tennis_backend.media.weights import (
    DETECTOR_ENV,
    DETECTOR_NAME,
    detector_weights,
    is_lfs_pointer,
    models_root,
    require,
)


def test_weights_live_under_the_repository_models_directory() -> None:
    assert models_root().name == "models"
    assert detector_weights() == models_root() / "detector" / DETECTOR_NAME


def test_environment_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(DETECTOR_ENV, str(tmp_path / "x.pth"))
    assert detector_weights() == tmp_path / "x.pth"


def test_a_missing_weight_is_a_stable_coded_refusal(tmp_path: Path) -> None:
    with pytest.raises(DomainViolation) as raised:
        require(tmp_path / "none.pth", code="X_MISSING", what="demo", env="E")
    assert raised.value.code.endswith("X_MISSING")
    assert "none.pth" in str(raised.value)


def test_an_existing_weight_is_returned(tmp_path: Path) -> None:
    weight = tmp_path / "w.pth"
    weight.write_bytes(b"x")
    assert require(weight, code="X", what="demo", env="E") == weight


def test_the_shipped_weights_are_in_the_project() -> None:
    root = models_root()
    for relative in ("ball/wasb_tennis_best.pth.tar", "pose/pose_landmarker_full.task"):
        assert (root / relative).is_file(), relative


def test_an_unpulled_lfs_pointer_is_refused_with_the_fix(tmp_path: Path) -> None:
    pointer = tmp_path / "w.pth"
    pointer.write_bytes(
        b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 366287238\n"
    )
    assert is_lfs_pointer(pointer) is True
    with pytest.raises(DomainViolation) as raised:
        require(pointer, code="X_MISSING", what="demo", env="E")
    assert "git lfs pull" in str(raised.value)


def test_real_weights_are_not_mistaken_for_a_pointer(tmp_path: Path) -> None:
    real = tmp_path / "w.pth"
    real.write_bytes(b"PK\x03\x04" + b"0" * 1000)
    assert is_lfs_pointer(real) is False
    assert require(real, code="X", what="demo", env="E") == real
