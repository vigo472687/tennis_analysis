"""Evidence rendering and target skeleton tests (BE-020).

Real FFmpeg extracts frames and builds ruler/slow-motion clips from a real
synthetic clip; real OpenCV burns the original-time ruler and draws the
target skeleton. Contract shapes are validated against the frozen
``evidence:render_result`` and ``analysis:target_result`` definitions, and
the separation rule is asserted: the target artifact never appears in the
evidence channel.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.domain.models import DomainViolation
from tennis_backend.evidence.catalog import EvidenceCatalog
from tennis_backend.evidence.render import generate_target_skeleton, render_evidence
from tennis_backend.media.probe import probe_video

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "v1"
FIXTURE_ROOT = REPO_ROOT / "fixtures"
FFMPEG = shutil.which("ffmpeg")


def make_clip(directory: Path, name: str = "src.mp4", seconds: float = 3.0) -> Path:
    assert FFMPEG is not None
    target = directory / name
    subprocess.run(
        [
            FFMPEG,
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=320x240:d={seconds}:r=25",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    return target


def catalog_for(tmp_path: Path, fixture_id: str = "iphone-flat") -> EvidenceCatalog:
    from tennis_backend.analysis.adapters import StageRequest
    from tennis_backend.analysis.fixture import FixtureAnalysisAdapter, FixtureLibrary
    from tennis_backend.application.worker import CancellationToken
    from tennis_backend.domain.models import MediaAsset, MediaProbe, TimePoint

    adapter = FixtureAnalysisAdapter(FixtureLibrary.load(FIXTURE_ROOT))
    request = StageRequest(
        task_id="task-demo",
        version=1,
        fixture_id=fixture_id,
        media=MediaProbe(
            container="mp4",
            codec="h264",
            duration_s=3.0,
            fps=30.0,
            width=320,
            height=240,
            rotation="0",
            slow_motion="unknown",
            hdr="no",
            device_source="unknown",
            panorama="flat",
            observation_kind="measured",
        ),
    )
    token = CancellationToken()
    mapping = [
        TimePoint(normalized_s=round(i * 0.1, 3), original_s=round(i * 0.1, 3)) for i in range(31)
    ]
    media = MediaAsset(
        media_id="media-src-1-std-1",
        role="standardized",
        original_ref="projects/task-demo/v1/media/source.mp4",
        derived_from="media-src-1",
        probe=probe_video(make_clip(tmp_path)).media,
        time_mapping=mapping,
        time_mapping_valid=True,
        simulation=True,
    )
    return EvidenceCatalog.build(
        task_id="task-demo",
        version=1,
        subject_id="person-1",
        media=media,
        visible_objects=("body", "feet", "ball", "racket"),
        unavailable_objects=(),
        strokes=adapter.run("strokes", request, cancel=token),
        timing=adapter.run("timing", request, cancel=token),
        metrics=adapter.run("metrics", request, cancel=token),
        metric_requires={"preparation-order": ("body",), "support-continuity": ("feet", "body")},
    )


@pytest.fixture(scope="module")
def registry() -> ContractRegistry:
    return ContractRegistry.load(CONTRACTS_DIR)


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path / "public"
    root.mkdir()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    source = make_clip(media_dir)
    return root, source


def test_frames_are_real_hashed_and_contract_valid(
    workspace: tuple[Path, Path], registry: ContractRegistry, tmp_path: Path
) -> None:
    root, source = workspace
    catalog = catalog_for(tmp_path)
    outcome = render_evidence(
        source_video=source,
        root=root,
        relative_dir="projects/task-demo/v1/evidence",
        evidence=catalog.evidence,
        mode="frame",
    )
    assert outcome.result["simulation"] is True
    for resource in outcome.result["resources"]:
        assert resource["kind"] == "evidence_frame"
        assert resource["mime"] == "image/png"
        file = root / resource["path"]
        assert file.exists() and file.stat().st_size == resource["size_bytes"]
        import hashlib

        assert hashlib.sha256(file.read_bytes()).hexdigest() == resource["sha256"]
    # every rendered evidence entry links its own resource through the
    # frozen "resource" field — no extra keys beyond the frozen shape
    resource_paths = {r["path"] for r in outcome.result["resources"]}
    for entry in outcome.result["evidence"]:
        assert entry["resource"] is not None
        assert entry["resource"]["path"] in resource_paths
    # the whole result satisfies the frozen render_result as returned
    assert (
        registry.validate(registry.definition_ref("analysis", "render_result"), outcome.result)
        == ()
    )


def test_trajectory_clip_burns_original_time_ruler(
    workspace: tuple[Path, Path], registry: ContractRegistry, tmp_path: Path
) -> None:
    root, source = workspace
    catalog = catalog_for(tmp_path)
    outcome = render_evidence(
        source_video=source,
        root=root,
        relative_dir="projects/task-demo/v1/evidence",
        evidence=catalog.evidence,
        mode="trajectory",
    )
    clip = outcome.files[-1]
    assert clip.exists()
    summary = probe_video(clip).summary
    assert summary["container"] == "mp4"
    assert summary["duration_s"] > 0.1
    resource = outcome.result["resources"][0]
    assert resource["kind"] == "trajectory"
    assert resource["sha256"] == __import__("hashlib").sha256(clip.read_bytes()).hexdigest()
    # the whole result satisfies the frozen render_result as returned
    assert (
        registry.validate(registry.definition_ref("analysis", "render_result"), outcome.result)
        == ()
    )


def test_slow_motion_stretches_measured_duration(
    workspace: tuple[Path, Path], tmp_path: Path
) -> None:
    root, source = workspace
    catalog = catalog_for(tmp_path)
    outcome = render_evidence(
        source_video=source,
        root=root,
        relative_dir="projects/task-demo/v1/evidence",
        evidence=catalog.evidence,
        mode="slow_motion",
        slow_factor=2.0,
    )
    clip = outcome.files[-1]
    summary = probe_video(clip).summary
    interval = max(e["original_end_s"] for e in catalog.evidence) - min(
        e["original_start_s"] for e in catalog.evidence
    )
    assert summary["duration_s"] == pytest.approx(interval * 2.0, rel=0.15)
    assert outcome.result["resources"][0]["kind"] == "evidence_clip"


def test_missing_source_and_empty_evidence_refused(workspace: tuple[Path, Path]) -> None:
    root, source = workspace
    with pytest.raises(DomainViolation) as error:
        render_evidence(
            source_video=root / "nope.mp4",
            root=root,
            relative_dir="x",
            evidence=({"evidence_id": "e", "original_start_s": 0.5, "original_end_s": 1.0},),
        )
    assert error.value.code == "DOMAIN_RENDER_SOURCE_MISSING"
    with pytest.raises(DomainViolation) as error:
        render_evidence(source_video=source, root=root, relative_dir="x", evidence=())
    assert error.value.code == "DOMAIN_RENDER_NO_EVIDENCE"
    with pytest.raises(DomainViolation):
        render_evidence(
            source_video=source,
            root=root,
            relative_dir="x",
            evidence=({"evidence_id": "e", "original_start_s": 0.5, "original_end_s": 1.0},),
            mode="hologram",
        )


# ---------------------------------------------------------------------------
# Target skeleton: separate artifact, never evidence
# ---------------------------------------------------------------------------


def test_target_skeleton_is_stamped_simulated_and_separate(
    workspace: tuple[Path, Path], registry: ContractRegistry, tmp_path: Path
) -> None:
    root, source = workspace
    result = generate_target_skeleton(
        root=root,
        relative_dir="projects/task-demo/v1/target",
        task_id="task-demo",
        version=1,
        features=["contact height reference", "shoulder turn reference"],
    )
    assert result["kind"] == "target_pose"
    assert result["simulation"] is True
    assert any(
        "NOT MEASURED" in item or "not a detected pose" in item for item in result["limitations"]
    )
    assert registry.validate(registry.definition_ref("analysis", "target_result"), result) == ()
    file = root / result["resource"]["path"]
    assert file.exists() and file.stat().st_size == result["resource"]["size_bytes"]
    assert result["resource"]["kind"] == "target_pose"

    # the target artifact is structurally separate from evidence
    catalog = catalog_for(tmp_path)
    evidence_ids = {entry["evidence_id"] for entry in catalog.evidence}
    assert result["resource"]["resource_id"] not in evidence_ids
