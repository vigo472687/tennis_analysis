"""Where the model weights live: inside the project, so a clone runs offline.

Every weight the backend loads is resolved here, in one place:

========================  ==================================  ==========
weight                    path under ``models/``              size
========================  ==================================  ==========
RF-DETR nano (COCO)       ``detector/rf-detr-nano.pth``       366 MB
WASB ball detector        ``ball/wasb_tennis_best.pth.tar``   6 MB
MediaPipe pose landmarks  ``pose/pose_landmarker_full.task``  9 MB
========================  ==================================  ==========

Each can be overridden with an environment variable (useful for tests and for
installs that keep weights elsewhere). A missing file is a clear refusal with a
stable code, never a silent download: RF-DETR would otherwise fetch 366 MB
into ``~/.roboflow`` on first use.
"""

from __future__ import annotations

import os
from pathlib import Path

from tennis_backend.domain.models import DomainViolation

DETECTOR_ENV = "TENNIS_DETECTOR_WEIGHTS"
DETECTOR_NAME = "rf-detr-nano.pth"
BALL_NAME = "wasb_tennis_best.pth.tar"
POSE_NAME = "pose_landmarker_full.task"
#: MD5 published by RF-DETR for this checkpoint (it verifies it on load).
DETECTOR_MD5 = "fb6504cce7fbdc783f7a46991f07639f"


def models_root() -> Path:
    """The repository's ``models/`` directory (``local-backend/../models``)."""
    return Path(__file__).resolve().parents[4] / "models"


def detector_weights() -> Path:
    override = os.environ.get(DETECTOR_ENV)
    return Path(override) if override else models_root() / "detector" / DETECTOR_NAME


#: Git LFS pointer files start with this line and are ~130 bytes.
_LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


def is_lfs_pointer(path: Path) -> bool:
    """True when the file is an unpulled Git LFS pointer, not the weights."""
    try:
        with path.open("rb") as handle:
            return handle.read(len(_LFS_POINTER)) == _LFS_POINTER
    except OSError:
        return False


def require(path: Path, *, code: str, what: str, env: str) -> Path:
    """The weight file, or a stable-coded refusal naming how to get it.

    A clone made without Git LFS leaves a ~130 byte pointer where the weights
    should be. That is not a usable file, so it is refused with the command that
    fixes it, rather than failing later inside the model loader.
    """
    if not path.is_file():
        raise DomainViolation(
            code,
            f"{what} weights {path.name} not found; place the file under models/ or set {env}",
        )
    if is_lfs_pointer(path):
        raise DomainViolation(
            code,
            f"{what} weights {path.name} is a Git LFS pointer, not the file; "
            "run `git lfs pull` in the repository root",
        )
    return path
