"""Comparable-retest comparison tests (BE-021, AC-SPEC-029).

Uses metric sets taken from the real frozen fixture catalog. Covers: the
comparable gate over every condition (device scenario, stroke, subject,
config, contract, mirror, safety, protocol), relative changes only when
comparable, differences+reshoot guidance only when not, metric coverage
gaps listed without dropping the rest, and the frozen compare_result
contract including the comparable=false → empty changes rule. No permanent
rating vocabulary anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.analysis.adapters import StageRequest
from tennis_backend.analysis.comparison import SessionSnapshot, compare_sessions
from tennis_backend.analysis.fixture import FixtureAnalysisAdapter, FixtureLibrary
from tennis_backend.application.worker import CancellationToken
from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.domain.models import DomainViolation, MediaProbe

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "v1"
FIXTURE_ROOT = REPO_ROOT / "fixtures"

PROTOCOL = (
    ("ball_task", "coach feed from the front"),
    ("camera", "flat behind baseline"),
    ("observations", "contact point, balance"),
    ("success_condition", "three clean hits in view"),
)


def snapshot(
    task_id: str = "task-base",
    version: int = 1,
    fixture_id: str = "iphone-flat",
    **overrides: Any,
) -> SessionSnapshot:
    adapter = FixtureAnalysisAdapter(FixtureLibrary.load(FIXTURE_ROOT))
    probe = MediaProbe(
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
    )
    request = StageRequest(task_id=task_id, version=version, fixture_id=fixture_id, media=probe)
    token = CancellationToken()
    metrics_result = adapter.run("metrics", request, cancel=token)
    strokes = adapter.run("strokes", request, cancel=token)
    manifest = FixtureLibrary.load(FIXTURE_ROOT).sample(fixture_id)
    fields: dict[str, Any] = {
        "task_id": task_id,
        "version": version,
        "fixture_id": fixture_id,
        "device_scenario": str(manifest["device_scenario"]),
        "stroke_kind": str(strokes["events"][0]["kind"]),
        "subject_id": "person-1",
        "config_version": "fixture-config-1",
        "contract_version": "1.1.0",
        "mirror_confirmed": True,
        "safety": "clear",
        "protocol": PROTOCOL,
        "metrics": tuple(metrics_result["metrics"]),
    }
    fields.update(overrides)
    return SessionSnapshot(**fields)


@pytest.fixture(scope="module")
def registry() -> ContractRegistry:
    return ContractRegistry.load(CONTRACTS_DIR)


def result_valid(registry: ContractRegistry, result: dict[str, Any]) -> bool:
    return registry.validate(registry.definition_ref("analysis", "compare_result"), result) == ()


# ---------------------------------------------------------------------------
# Comparable path: relative changes with the frozen contract shape
# ---------------------------------------------------------------------------


def test_identical_conditions_compare_with_deltas(registry: ContractRegistry) -> None:
    baseline = snapshot()
    retest = snapshot(task_id="task-retest", version=2)
    result = compare_sessions(baseline, retest)
    assert result["comparable"] is True
    assert result["simulation"] is True
    metric_ids = {c["metric_id"] for c in result["changes"]}
    assert "preparation-order" in metric_ids
    change = next(c for c in result["changes"] if c["metric_id"] == "preparation-order")
    assert change["value"] == 0.0  # deterministic fixture values: identical
    assert change["reference"].startswith("delta against baseline task-base v1")
    assert any("not a rating, level or permanent score" in item for item in change["limitations"])
    assert result_valid(registry, result)


def test_relative_change_reflects_actual_metric_difference(
    registry: ContractRegistry,
) -> None:
    baseline = snapshot()
    retest_metrics = [dict(m) for m in snapshot().metrics]
    for metric in retest_metrics:
        if metric["metric_id"] == "preparation-order":
            metric["value"] = float(metric["value"]) + 0.05
    retest = snapshot(task_id="task-retest", metrics=tuple(retest_metrics))
    result = compare_sessions(baseline, retest)
    change = next(c for c in result["changes"] if c["metric_id"] == "preparation-order")
    assert change["value"] == pytest.approx(0.05, abs=1e-6)
    assert result_valid(registry, result)


# ---------------------------------------------------------------------------
# Not comparable: differences and reshoot guidance only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides,expected_label",
    [
        ({"device_scenario": "android-flat-export"}, "camera position"),
        ({"stroke_kind": "serve"}, "stroke kind"),
        ({"subject_id": "person-2"}, "subject"),
        ({"config_version": "fixture-config-2"}, "configuration version"),
        ({"contract_version": "0.9.0"}, "contract version"),
        ({"mirror_confirmed": False}, "mirror confirmation"),
        ({"safety": "pain"}, "safety state"),
    ],
)
def test_condition_mismatch_is_not_comparable(
    registry: ContractRegistry, overrides: dict[str, Any], expected_label: str
) -> None:
    result = compare_sessions(snapshot(), snapshot(task_id="task-retest", **overrides))
    assert result["comparable"] is False
    assert result["changes"] == []  # frozen rule: no changes when not comparable
    joined = json.dumps(result["differences"], ensure_ascii=False)
    assert expected_label in joined
    # every difference carries its remedial action after the dash
    assert all(" — " in item and len(item.split(" — ")[1]) > 5 for item in result["differences"])
    assert result_valid(registry, result)


def test_protocol_mismatch_lists_missing_items(registry: ContractRegistry) -> None:
    weaker_protocol = tuple(PROTOCOL)[:3]  # drops the success condition
    result = compare_sessions(snapshot(), snapshot(task_id="task-retest", protocol=weaker_protocol))
    assert result["comparable"] is False
    joined = json.dumps(result["differences"])
    assert "success_condition" in joined
    assert result_valid(registry, result)


# ---------------------------------------------------------------------------
# Coverage gaps: conditions comparable, affected metric listed, rest compares
# ---------------------------------------------------------------------------


def test_metric_coverage_gap_listed_without_dropping_rest(
    registry: ContractRegistry,
) -> None:
    baseline = snapshot()
    # drop support-continuity on the retest side: the remaining common metric
    # (preparation-order) still compares, and the gap is listed as a difference
    retest_metrics = tuple(
        m for m in snapshot(task_id="task-retest").metrics if m["metric_id"] != "support-continuity"
    )
    retest = snapshot(task_id="task-retest", metrics=retest_metrics)
    result = compare_sessions(baseline, retest)
    assert result["comparable"] is True  # conditions matched
    assert any("metric coverage: support-continuity" in d for d in result["differences"])
    assert {c["metric_id"] for c in result["changes"]} == {"preparation-order"}
    assert result_valid(registry, result)


def test_unit_mismatch_listed_as_difference(registry: ContractRegistry) -> None:
    retest_metrics = [dict(m) for m in snapshot().metrics]
    retest_metrics[0]["unit"] = "normalized"
    result = compare_sessions(
        snapshot(), snapshot(task_id="task-retest", metrics=tuple(retest_metrics))
    )
    assert any("units differ" in d for d in result["differences"])


def test_definition_mismatch_is_listed_and_no_delta_published(
    registry: ContractRegistry,
) -> None:
    retest_metrics = [dict(m) for m in snapshot().metrics]
    for metric in retest_metrics:
        if metric["metric_id"] == "preparation-order":
            metric["definition"] = "a different definition with the same id"
    result = compare_sessions(
        snapshot(), snapshot(task_id="task-retest", metrics=tuple(retest_metrics))
    )
    # no delta may be fabricated across different definitions
    published_ids = {c["metric_id"] for c in result["changes"]}
    assert "preparation-order" not in published_ids
    assert any("definitions differ" in d for d in result["differences"])
    assert result_valid(registry, result)


def test_non_numeric_metric_values_not_compared(registry: ContractRegistry) -> None:
    retest_metrics = [dict(m) for m in snapshot().metrics]
    retest_metrics[0]["value"] = None
    result = compare_sessions(
        snapshot(), snapshot(task_id="task-retest", metrics=tuple(retest_metrics))
    )
    assert any("non-numeric" in d for d in result["differences"])


# ---------------------------------------------------------------------------
# Honesty: no permanent rating vocabulary anywhere
# ---------------------------------------------------------------------------


def test_no_permanent_rating_vocabulary(registry: ContractRegistry) -> None:
    result = compare_sessions(snapshot(), snapshot(task_id="task-retest", version=2))
    # rating vocabulary may only appear inside the explicit disclaimer
    # limitation ("relative change only; not a rating, ..."), never as a
    # value, unit, reference, definition or difference
    allowed_context = "not a rating, level or permanent score"
    for banned in ("ntrp", "rating", "level", "score"):
        if banned == "ntrp":
            assert banned not in json.dumps(result).lower()
            continue
        for metric in result["changes"]:
            for key in ("definition", "unit", "reference", "value"):
                assert banned not in json.dumps(metric[key]).lower(), (
                    f"rating vocabulary leaked into {key}: {banned}"
                )
        for limitation in metric["limitations"]:
            if banned in limitation.lower():
                assert allowed_context in limitation
    assert result_valid(registry, result)


def test_unordered_protocol_refused() -> None:
    scrambled = tuple(reversed(PROTOCOL))
    with pytest.raises(DomainViolation) as error:
        snapshot(protocol=scrambled)
    assert error.value.code == "DOMAIN_PROTOCOL_UNORDERED"
