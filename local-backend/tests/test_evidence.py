"""Evidence catalog tests: back-links, gating and refusals (BE-019).

Builds the catalog from the real frozen fixture adapter results and a
standardized media asset — in one case a genuinely FFmpeg-standardized clip
with a measured time mapping — and asserts the publication gates: unmapped
or partially covering time mappings refuse evidence, invisible objects yield
no entries, footwork support metrics need an observed axis, target pose is
never evidence, and every published shape satisfies the frozen contracts.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.analysis.fixture import FixtureAnalysisAdapter, FixtureLibrary
from tennis_backend.application.worker import CancellationToken
from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.domain.models import DomainViolation, MediaAsset, MediaProbe, TimePoint
from tennis_backend.evidence.catalog import EvidenceCatalog
from tennis_backend.media.normalize import normalize_video

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "v1"
FIXTURE_ROOT = REPO_ROOT / "fixtures"
FFMPEG = shutil.which("ffmpeg")
NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)


def asset_with_mapping(
    *,
    span_s: float = 3.0,
    points: int = 30,
    valid: bool = True,
    media_id: str = "media-src-1-std-1",
) -> MediaAsset:
    step = span_s / max(points - 1, 1)
    mapping = [
        TimePoint(normalized_s=round(i * step, 6), original_s=round(i * step, 6))
        for i in range(points)
    ]
    probe = MediaProbe(
        container="mp4",
        codec="h264",
        duration_s=span_s,
        fps=25.0,
        width=1920,
        height=1080,
        rotation="0",
        slow_motion="unknown",
        hdr="no",
        device_source="unknown",
        panorama="flat",
        observation_kind="measured",
    )
    return MediaAsset(
        media_id=media_id,
        role="standardized",
        original_ref="projects/task-demo/v1/media/source.mp4",
        derived_from="media-src-1",
        probe=probe,
        time_mapping=mapping,
        time_mapping_valid=valid,
        simulation=True,
    )


def stage_results(fixture_id: str) -> dict[str, dict[str, Any]]:
    adapter = FixtureAnalysisAdapter(FixtureLibrary.load(FIXTURE_ROOT))
    request = Any  # adapter only reads fixture_id/task fields via raw_params
    from tennis_backend.analysis.adapters import StageRequest

    probe = MediaProbe(
        container="mp4",
        codec="h264",
        duration_s=3.0,
        fps=30.0,
        width=1920,
        height=1080,
        rotation="0",
        slow_motion="unknown",
        hdr="no",
        device_source="unknown",
        panorama="flat",
        observation_kind="measured",
    )
    req = StageRequest(task_id="task-demo", version=1, fixture_id=fixture_id, media=probe)
    del request
    token = CancellationToken()
    return {
        "strokes": adapter.run("strokes", req, cancel=token),
        "timing": adapter.run("timing", req, cancel=token),
        "metrics": adapter.run("metrics", req, cancel=token),
    }


@pytest.fixture(scope="module")
def registry() -> ContractRegistry:
    return ContractRegistry.load(CONTRACTS_DIR)


FULL_VISIBLE = ("body", "feet", "ball", "racket")
METRIC_REQUIRES = {
    "preparation-order": ("body",),
    "support-continuity": ("feet", "body"),
}


def build(fixture_id: str = "iphone-flat", **overrides: Any) -> EvidenceCatalog:
    kwargs: dict[str, Any] = dict(
        task_id="task-demo",
        version=1,
        subject_id="person-1",
        media=asset_with_mapping(),
        visible_objects=FULL_VISIBLE,
        unavailable_objects=(),
        strokes=stage_results(fixture_id)["strokes"],
        timing=stage_results(fixture_id)["timing"],
        metrics=stage_results(fixture_id)["metrics"],
        metric_requires=METRIC_REQUIRES,
    )
    kwargs.update(overrides)
    return EvidenceCatalog.build(**kwargs)


# ---------------------------------------------------------------------------
# Happy path: contract-valid, back-linked evidence
# ---------------------------------------------------------------------------


def test_catalog_publishes_back_linked_evidence(registry: ContractRegistry) -> None:
    catalog = build()
    assert catalog.evidence, "a fully visible fixture publishes stroke evidence"
    for entry in catalog.evidence:
        assert registry.validate(registry.definition_ref("analysis", "evidence"), entry) == ()
        assert 0.0 <= entry["original_start_s"] <= entry["original_end_s"] <= 3.0
        assert entry["simulation"] is True
    clip = catalog.evidence[0]
    assert clip["kind"] == "video_clip"
    assert clip["asset_id"].startswith("media-src-1")  # back-linked to the asset
    assert "mapping_span_s=3.000" in catalog.notes[0]


def test_metric_definitions_carry_reference_and_limitations(registry: ContractRegistry) -> None:
    catalog = build()
    # preparation-order observes against the forward_swing phase; the frozen
    # support-continuity definition has no measurable reference phase, so the
    # catalog honestly refuses it instead of publishing a guessed value
    assert {m["metric_id"] for m in catalog.metrics} == {"preparation-order"}
    refusal = [r for r in catalog.refused if r.item == "metric:support-continuity"]
    assert refusal and "value_state" in refusal[0].detail
    for metric in catalog.metrics:
        assert registry.validate(registry.definition_ref("analysis", "metric"), metric) == ()
        assert metric["reference"]
        assert any("not a measured physical quantity" in item for item in metric["limitations"])
        assert metric["unit"] in ("relative", "normalized", "ratio", "event_order")


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
def test_catalog_over_a_real_standardized_clip(registry: ContractRegistry) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "src.mp4"
        subprocess_make_clip(source)
        outcome = normalize_video(
            source,
            output_dir=Path(tmp) / "out",
            original_media_id="media-real-1",
            original_ref="projects/task-demo/v1/media/source.mp4",
        )
        assert outcome.media is not None and outcome.media.time_mapping_valid
        catalog = build(media=outcome.media)
        assert catalog.mapping_span_s == pytest.approx(outcome.mapping[-1].normalized_s)
        for entry in catalog.evidence:
            assert registry.validate(registry.definition_ref("analysis", "evidence"), entry) == ()


def subprocess_make_clip(target: Path) -> None:
    import subprocess

    assert FFMPEG is not None
    subprocess.run(
        [
            FFMPEG,
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=3:r=25",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(target),
        ],
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Hard refusals: broken time mapping, outside span, target as evidence
# ---------------------------------------------------------------------------


def test_invalid_time_mapping_refuses_all_evidence() -> None:
    with pytest.raises(DomainViolation) as error:
        build(media=asset_with_mapping(valid=False))
    assert error.value.code == "DOMAIN_EVIDENCE_TIME_UNMAPPED"


def test_interval_beyond_mapping_span_refuses() -> None:
    short = asset_with_mapping(span_s=1.0, points=10)
    with pytest.raises(DomainViolation) as error:
        build(media=short)  # iphone-flat stroke window reaches 2.8s
    assert error.value.code == "DOMAIN_EVIDENCE_OUTSIDE_MAPPING"


def test_zero_span_mapping_refuses() -> None:
    flat = asset_with_mapping(span_s=0.0, points=1)
    with pytest.raises(DomainViolation) as error:
        build(media=flat)
    assert error.value.code == "DOMAIN_EVIDENCE_TIME_UNMAPPED"


def test_target_pose_is_never_evidence() -> None:
    with pytest.raises(DomainViolation) as error:
        build(target_resources=("res-target-1",))
    assert error.value.code == "DOMAIN_EVIDENCE_TARGET_POSE"


def test_metric_citing_unknown_evidence_refused() -> None:
    results = stage_results("iphone-flat")
    results["metrics"]["metrics"][0]["evidence_ids"] = ["ev-does-not-exist"]
    with pytest.raises(DomainViolation) as error:
        build(metrics=results["metrics"])
    assert error.value.code == "DOMAIN_EVIDENCE_REFERENCE_UNKNOWN"


# ---------------------------------------------------------------------------
# Item-level gating: invisible objects and footwork axis
# ---------------------------------------------------------------------------


def test_invisible_timing_events_yield_no_evidence_but_are_refused() -> None:
    catalog = build("missing-feet", visible_objects=("body", "ball", "racket"))
    refused_reasons = [r.reason for r in catalog.refused]
    assert "EVIDENCE_OBJECT_NOT_VISIBLE" in refused_reasons
    kinds = [
        entry["evidence_id"] for entry in catalog.evidence if "-timing-" in entry["evidence_id"]
    ]
    assert all("recovery" not in kind for kind in kinds)  # recovery requires feet
    observed = [r for r in catalog.refused if "timing:preparation" in r.item]
    assert observed == []  # preparation only needs body, which is visible


def test_metric_requiring_unavailable_object_is_refused() -> None:
    catalog = build(
        "missing-feet",
        visible_objects=("body", "ball", "racket"),
        unavailable_objects=("feet",),
    )
    published_ids = {m["metric_id"] for m in catalog.metrics}
    assert "support-continuity" not in published_ids
    refusal = [r for r in catalog.refused if r.item == "metric:support-continuity"]
    assert refusal and refusal[0].reason == "EVIDENCE_METRIC_REFUSED"
    assert "feet" in refusal[0].detail


def test_footwork_without_observed_axis_is_refused() -> None:
    catalog = build(
        "insta360-flat",
        footwork_axis_observed=False,
        metric_requires=METRIC_REQUIRES,
    )
    axis_refusal = [r for r in catalog.refused if "same-axis" in r.detail]
    assert axis_refusal, "footwork axis gate must be recorded honestly"
    published_ids = {m["metric_id"] for m in catalog.metrics}
    assert "support-continuity" not in published_ids


def test_footwork_with_observed_axis_falls_through_to_honest_value_state() -> None:
    catalog = build(
        "insta360-flat",
        footwork_axis_observed=True,
        metric_requires=METRIC_REQUIRES,
    )
    axis_refusal = [r for r in catalog.refused if "same-axis" in r.detail]
    assert axis_refusal == []  # the axis gate passed
    # the frozen support-continuity definition still has no measurable
    # reference phase, so the honest value_state refusal applies instead
    value_refusal = [
        r
        for r in catalog.refused
        if r.item == "metric:support-continuity" and "value_state" in r.detail
    ]
    assert value_refusal


def test_refused_items_never_leak_into_published() -> None:
    catalog = build("missing-feet", visible_objects=("body", "ball", "racket"))
    refused_items = {r.item for r in catalog.refused}
    for metric in catalog.metrics:
        assert f"metric:{metric['metric_id']}" not in refused_items
    for entry in catalog.evidence:
        assert f"evidence:{entry['evidence_id']}" not in refused_items
