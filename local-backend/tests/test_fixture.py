"""Frozen-fixture analysis adapter tests (BE-016).

Runs against the real frozen fixture library (fixtures/manifest.json +
fixtures/expected). Every stage result is validated through the shared
BE-015 ``run_stage`` against the frozen contracts; the quality report is
validated against ``media:quality``. Honesty cases: unknown fixture refused,
unknown action never force-classified, invisible required objects never
given invented times, insufficient-evidence metrics stay unset, and every
response is marked simulation=true.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tennis_backend.analysis.adapters import StageRequest, run_stage
from tennis_backend.analysis.fixture import FixtureAnalysisAdapter, FixtureLibrary
from tennis_backend.application.worker import CancellationToken
from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.domain.models import DomainViolation, MediaProbe

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "v1"
FIXTURE_ROOT = REPO_ROOT / "fixtures"

FULL_PROBE = MediaProbe(
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


def request_for(fixture_id: str) -> StageRequest:
    return StageRequest(task_id="task-demo", version=1, media=FULL_PROBE, fixture_id=fixture_id)


@pytest.fixture(scope="module")
def library() -> FixtureLibrary:
    return FixtureLibrary.load(FIXTURE_ROOT)


@pytest.fixture(scope="module")
def registry() -> ContractRegistry:
    return ContractRegistry.load(CONTRACTS_DIR)


@pytest.fixture
def adapter(library: FixtureLibrary) -> FixtureAnalysisAdapter:
    return FixtureAnalysisAdapter(library)


def run(
    adapter: FixtureAnalysisAdapter, stage: str, fixture_id: str, registry: ContractRegistry
) -> dict[str, Any]:
    return run_stage(
        adapter, stage, request_for(fixture_id), cancel=CancellationToken(), registry=registry
    )


# ---------------------------------------------------------------------------
# Seven supported strokes come out of the frozen library, contract-valid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_id,expected_kind",
    [
        ("iphone-flat", "forehand"),
        ("android-flat", "double_backhand"),
        ("gopro-flat", "serve"),
        ("insta360-flat", "footwork"),
        ("single-backhand", "single_backhand"),
        ("forehand-volley", "forehand_volley"),
        ("backhand-volley", "backhand_volley"),
    ],
)
def test_seven_supported_strokes_are_frozen_and_contract_valid(
    adapter: FixtureAnalysisAdapter, registry: ContractRegistry, fixture_id: str, expected_kind: str
) -> None:
    result = run(adapter, "strokes", fixture_id, registry)
    assert result["simulation"] is True
    event = result["events"][0]
    assert event["kind"] == expected_kind
    assert event["end_s"] >= event["start_s"] >= 0.0
    assert event["confidence"] in ("high", "medium", "low")


def test_capabilities_declare_synthetic_limitations(adapter: FixtureAnalysisAdapter) -> None:
    caps = adapter.capabilities()
    assert caps.simulation is True
    assert set(caps.supported_stages) == {"strokes", "timing", "metrics"}
    assert any("not a measured model" in item for item in caps.limitations)


# ---------------------------------------------------------------------------
# Unknown fixture / unknown action honesty
# ---------------------------------------------------------------------------


def test_unknown_fixture_refused(
    adapter: FixtureAnalysisAdapter, registry: ContractRegistry
) -> None:
    with pytest.raises(DomainViolation) as error:
        run(adapter, "strokes", "fixture-not-in-library", registry)
    assert error.value.code == "DOMAIN_FIXTURE_UNKNOWN"


def test_unknown_action_never_force_classified(
    adapter: FixtureAnalysisAdapter, registry: ContractRegistry
) -> None:
    result = run(adapter, "strokes", "unknown-action", registry)
    event = result["events"][0]
    assert event["kind"] == "unable"
    assert event["confidence"] == "low"
    assert result["simulation"] is True


def test_damaged_fixture_reports_unable_stroke(
    adapter: FixtureAnalysisAdapter, registry: ContractRegistry
) -> None:
    result = run(adapter, "strokes", "damaged", registry)
    assert result["events"][0]["kind"] == "unable"


# ---------------------------------------------------------------------------
# Timing: visible requirements gate every invented time
# ---------------------------------------------------------------------------


def test_timing_fully_visible_fixture_has_observed_times(
    adapter: FixtureAnalysisAdapter, registry: ContractRegistry
) -> None:
    result = run(adapter, "timing", "iphone-flat", registry)
    assert result["simulation"] is True
    assert result["events"], "iphone-flat phases must produce events"
    for event in result["events"]:
        assert event["value_state"] == "observed"
        assert event["original_s"] is not None
        assert 0.0 <= event["original_s"] <= 3.0


@pytest.mark.parametrize(
    "fixture_id,blocked_object",
    [
        ("missing-feet", "feet"),  # recovery requires feet
        ("missing-body", "body"),  # preparation and forward_swing require body
    ],
)
def test_timing_with_missing_objects_reports_not_visible_without_times(
    adapter: FixtureAnalysisAdapter,
    registry: ContractRegistry,
    fixture_id: str,
    blocked_object: str,
) -> None:
    result = run(adapter, "timing", fixture_id, registry)
    visible = set(FixtureLibrary.load(FIXTURE_ROOT).sample(fixture_id)["visible_objects"])
    visible_events = [e for e in result["events"] if e["value_state"] == "observed"]
    blocked = [e for e in result["events"] if e["value_state"] == "not_visible"]
    assert blocked, f"{fixture_id} must report not-visible events"
    for event in blocked:
        assert event["original_s"] is None  # no invented time
        assert any(obj not in visible for obj in event["requires"])
    # events not requiring any missing object keep honest observed values
    for event in visible_events:
        assert all(obj in visible for obj in event["requires"])
    assert blocked_object in {obj for e in blocked for obj in e["requires"]}


@pytest.mark.parametrize("fixture_id", ["missing-ball", "missing-racket"])
def test_timing_without_requiring_phases_has_no_blocked_events(
    adapter: FixtureAnalysisAdapter, registry: ContractRegistry, fixture_id: str
) -> None:
    # the frozen phases of these samples never require the missing object,
    # so nothing is blocked and nothing may pretend otherwise
    result = run(adapter, "timing", fixture_id, registry)
    assert all(event["value_state"] == "observed" for event in result["events"])


def test_timing_is_deterministic(
    adapter: FixtureAnalysisAdapter, registry: ContractRegistry
) -> None:
    first = run(adapter, "timing", "iphone-flat", registry)
    second = run(adapter, "timing", "iphone-flat", registry)
    assert first == second


# ---------------------------------------------------------------------------
# Metrics: relative definitions only, insufficient evidence stays unset
# ---------------------------------------------------------------------------


def test_metrics_frozen_definitions_contract_valid(
    adapter: FixtureAnalysisAdapter, registry: ContractRegistry
) -> None:
    result = run(adapter, "metrics", "iphone-flat", registry)
    assert result["simulation"] is True
    ids = [m["metric_id"] for m in result["metrics"]]
    assert ids == ["preparation-order", "support-continuity"]
    for metric in result["metrics"]:
        assert metric["unit"] in ("relative", "normalized", "ratio", "event_order")
        assert any("synthetic" in item for item in metric["limitations"])


def test_metric_reference_not_in_phases_stays_insufficient(
    adapter: FixtureAnalysisAdapter, registry: ContractRegistry
) -> None:
    # a fixture whose phases lack forward_swing cannot observe preparation-order
    result = run(adapter, "metrics", "missing-ball", registry)
    by_id = {m["metric_id"]: m for m in result["metrics"]}
    case = FixtureLibrary.load(FIXTURE_ROOT).action_case("missing-ball")
    if "forward_swing" not in case.get("phases", []):
        assert by_id["preparation-order"]["value_state"] == "insufficient_evidence"
        assert by_id["preparation-order"]["value"] is None
    else:
        assert by_id["preparation-order"]["value_state"] == "observed"


# ---------------------------------------------------------------------------
# Quality report: media:quality contract shape from frozen cases
# ---------------------------------------------------------------------------


def test_quality_report_pass_case_contract_valid(
    adapter: FixtureAnalysisAdapter, registry: ContractRegistry
) -> None:
    report = adapter.quality_report(request_for("iphone-flat"))
    assert report["status"] == "pass"
    assert report["issues"] == []
    assert report["simulation"] is True
    assert report["media"]["support"] == "supported"
    assert registry.validate(registry.definition_ref("media", "quality"), report) == ()


@pytest.mark.parametrize(
    "fixture_id,expected_status,expected_code,expected_object",
    [
        ("missing-feet", "limited", "missing_feet", "feet"),
        ("missing-ball", "limited", "missing_ball", "ball"),
        ("missing-racket", "limited", "missing_racket", "racket"),
        ("small-person", "fail", "subject_too_small", "subject"),
        ("mirror-conflict", "limited", "direction_confirmation_required", "direction"),
        ("overlong", "fail", "duration_exceeded", "media"),
    ],
)
def test_quality_reports_frozen_issues_with_objects_and_guidance(
    adapter: FixtureAnalysisAdapter,
    registry: ContractRegistry,
    fixture_id: str,
    expected_status: str,
    expected_code: str,
    expected_object: str,
) -> None:
    report = adapter.quality_report(request_for(fixture_id))
    assert report["status"] == expected_status
    codes = {issue["code"]: issue for issue in report["issues"]}
    assert expected_code in codes
    assert codes[expected_code]["object"] == expected_object
    assert codes[expected_code]["reshoot_guidance"]
    assert registry.validate(registry.definition_ref("media", "quality"), report) == ()


def test_quality_report_requires_subject_candidates_for_multiple(
    adapter: FixtureAnalysisAdapter, registry: ContractRegistry
) -> None:
    report = adapter.quality_report(request_for("multiple-subjects"))
    assert report["subject_candidates"], "multiple subjects must be surfaced, never picked"
    assert report["status"] == "limited"


def test_quality_time_mapping_never_claimed_valid(
    adapter: FixtureAnalysisAdapter, registry: ContractRegistry
) -> None:
    # the fixture adapter has no measured mapping; claiming one would fake it
    report = adapter.quality_report(request_for("iphone-flat"))
    assert report["time_mapping_valid"] is False
    assert report["evidence"] == []
