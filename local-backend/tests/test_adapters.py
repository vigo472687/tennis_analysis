"""Analysis adapter interface tests (BE-015).

Covers: the typed shared interface, simulation-flag honesty (a fixture is
always simulation=true, a real adapter always simulation=false), frozen
contract result validation per stage, the honest unavailable placeholder
(real pose model) that never returns data, stage support checks and
cancellation between stages. All payloads are synthetic fixture-shaped data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tennis_backend.analysis.adapters import (
    STAGE_METHODS,
    AdapterCapabilities,
    AnalysisAdapter,
    StageRequest,
    UnavailableAdapter,
    run_stage,
)
from tennis_backend.application.worker import CancellationToken, JobCancelled
from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.domain.models import DomainViolation, MediaProbe

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "v1"

MEASURED_PROBE = MediaProbe(
    container="mp4",
    codec="h264",
    duration_s=12.0,
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


def stage_request() -> StageRequest:
    return StageRequest(task_id="task-demo", version=1, media=MEASURED_PROBE, fixture_id="fx-1")


def strokes_result(simulation: bool = True) -> dict[str, Any]:
    return {
        "events": [
            {
                "event_id": "stroke-1",
                "kind": "forehand",
                "start_s": 1.0,
                "end_s": 2.0,
                "phases": ["preparation"],
                "confidence": "low",
                "evidence_ids": [],
            }
        ],
        "simulation": simulation,
    }


@pytest.fixture(scope="module")
def registry() -> ContractRegistry:
    return ContractRegistry.load(CONTRACTS_DIR)


class BareAdapter(AnalysisAdapter):
    """Concrete minimal adapter used to test the base construction rules."""

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            kind=self.kind, simulation=self.simulation, supported_stages=(), limitations=()
        )

    def run(
        self, stage: str, request: StageRequest, *, cancel: CancellationToken
    ) -> dict[str, Any]:
        raise AssertionError("BareAdapter never runs")


class RecordingFixtureAdapter(AnalysisAdapter):
    """Minimal fixture adapter that returns prepared stage payloads."""

    def __init__(self, results: dict[str, dict[str, Any]]) -> None:
        super().__init__(kind="fixture", simulation=True)
        self._results = results
        self.cancel_points_seen = 0

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            kind="fixture",
            simulation=True,
            supported_stages=tuple(self._results),
            limitations=("fixed fixture responses, not a measured model",),
        )

    def run(
        self, stage: str, request: StageRequest, *, cancel: CancellationToken
    ) -> dict[str, Any]:
        cancel.check_point()
        self.cancel_points_seen += 1
        return self._results[stage]


# ---------------------------------------------------------------------------
# Simulation honesty at construction
# ---------------------------------------------------------------------------


def test_fixture_adapter_must_declare_simulation_true() -> None:
    with pytest.raises(DomainViolation) as error:
        BareAdapter(kind="fixture", simulation=False)
    assert error.value.code == "DOMAIN_ADAPTER_SIMULATION_MISMATCH"


def test_real_adapter_must_declare_simulation_false() -> None:
    with pytest.raises(DomainViolation) as error:
        BareAdapter(kind="real", simulation=True)
    assert error.value.code == "DOMAIN_ADAPTER_SIMULATION_MISMATCH"


def test_fixture_adapter_accepts_simulation_true() -> None:
    adapter = BareAdapter(kind="fixture", simulation=True)
    assert adapter.simulation is True and adapter.kind == "fixture"


# ---------------------------------------------------------------------------
# Honest unavailability of the real model
# ---------------------------------------------------------------------------


def test_unavailable_adapter_declares_no_stages_and_refuses_runs(
    registry: ContractRegistry,
) -> None:
    adapter = UnavailableAdapter(
        kind="real_pose",
        limitations=("real pose model not installed", "not validated on any sample"),
    )
    caps = adapter.capabilities()
    assert caps.supported_stages == ()
    assert caps.simulation is False
    assert "not installed" in caps.limitations[0]
    with pytest.raises(DomainViolation) as error:
        run_stage(
            adapter, "strokes", stage_request(), cancel=CancellationToken(), registry=registry
        )
    assert error.value.code == "DOMAIN_ADAPTER_STAGE_UNSUPPORTED"


def test_unavailable_adapter_must_state_limitations() -> None:
    with pytest.raises(DomainViolation) as error:
        UnavailableAdapter(kind="real_pose", limitations=())
    assert error.value.code == "DOMAIN_ADAPTER_UNAVAILABLE"


def test_unavailable_adapter_is_a_real_kind_so_simulation_false_forced() -> None:
    adapter = UnavailableAdapter(kind="real_pose", limitations=("not installed",))
    # it can never be flipped into claiming real results
    assert adapter.simulation is False
    with pytest.raises(DomainViolation):
        BareAdapter(kind="real_pose", simulation=True)


# ---------------------------------------------------------------------------
# Stage support and contract-validated results
# ---------------------------------------------------------------------------


def test_stage_runs_and_result_passes_frozen_contract(registry: ContractRegistry) -> None:
    adapter = RecordingFixtureAdapter({"strokes": strokes_result()})
    result = run_stage(
        adapter, "strokes", stage_request(), cancel=CancellationToken(), registry=registry
    )
    assert result["simulation"] is True
    assert adapter.cancel_points_seen == 1


def test_result_violating_adapter_simulation_is_refused(registry: ContractRegistry) -> None:
    # a fixture adapter that returns simulation=false results is refused
    adapter = RecordingFixtureAdapter({"strokes": strokes_result(simulation=False)})
    with pytest.raises(DomainViolation) as error:
        run_stage(
            adapter, "strokes", stage_request(), cancel=CancellationToken(), registry=registry
        )
    assert error.value.code == "DOMAIN_ADAPTER_SIMULATION_MISMATCH"


def test_result_violating_frozen_schema_is_refused(registry: ContractRegistry) -> None:
    adapter = RecordingFixtureAdapter({"strokes": {"events": "not-a-list", "simulation": True}})
    with pytest.raises(Exception) as error:
        run_stage(
            adapter, "strokes", stage_request(), cancel=CancellationToken(), registry=registry
        )
    assert "RESULT_INVALID" in str(getattr(error.value, "code", error.value))


def test_unsupported_stage_refused(registry: ContractRegistry) -> None:
    adapter = RecordingFixtureAdapter({"metrics": {"metrics": [], "simulation": True}})
    with pytest.raises(DomainViolation) as error:
        run_stage(
            adapter, "strokes", stage_request(), cancel=CancellationToken(), registry=registry
        )
    assert error.value.code == "DOMAIN_ADAPTER_STAGE_UNSUPPORTED"


def test_unknown_stage_refused(registry: ContractRegistry) -> None:
    adapter = RecordingFixtureAdapter({})
    with pytest.raises(DomainViolation) as error:
        adapter.assert_stage_supported("poses")
    assert error.value.code == "DOMAIN_ADAPTER_STAGE_UNSUPPORTED"


def test_timing_and_metrics_stages_validate_against_contracts(
    registry: ContractRegistry,
) -> None:
    timing = {"events": [], "limitations": ["no timing evidence"], "simulation": True}
    metrics = {"metrics": [], "simulation": True}
    adapter = RecordingFixtureAdapter({"timing": timing, "metrics": metrics})
    for stage in ("timing", "metrics"):
        result = run_stage(
            adapter, stage, stage_request(), cancel=CancellationToken(), registry=registry
        )
        assert result["simulation"] is True
    assert set(STAGE_METHODS) == {"strokes", "timing", "metrics"}


# ---------------------------------------------------------------------------
# Cancellation between stages
# ---------------------------------------------------------------------------


def test_cancelled_token_stops_before_run(registry: ContractRegistry) -> None:
    adapter = RecordingFixtureAdapter({"strokes": strokes_result()})
    token = CancellationToken()
    token.cancel()
    with pytest.raises(JobCancelled):
        run_stage(adapter, "strokes", stage_request(), cancel=token, registry=registry)
    assert adapter.cancel_points_seen == 0  # run never started
