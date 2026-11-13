"""Typed analysis adapter interface shared by fixture and real processing (BE-015).

One interface for every analysis source, so the fixture adapter (BE-016) and
a future real model adapter are interchangeable for their callers:

- :class:`AnalysisAdapter` is the abstract typed interface: declare
  :class:`AdapterCapabilities`, then run one analysis stage
  (``strokes``/``timing``/``metrics``) and return the frozen contract result
  shape for that stage.
- Every produced result is validated against the frozen contracts
  (``analysis.strokes``/``analysis.timing``/``analysis.metrics`` outputs) and
  against the adapter's own declared ``simulation`` flag — a fixture adapter
  must be constructed with ``simulation=True`` and a real adapter with
  ``simulation=False``; mismatches are refused at construction and at every
  run, so a stand-in can never present itself as real inference.
- :class:`UnavailableAdapter` is the honest placeholder for capabilities that
  do not exist yet (the real pose model): it declares no supported stages,
  carries explicit limitation notes and refuses every run with a stable
  code — it never returns fabricated data.

Adapters must honor the :class:`CancellationToken` between internal stages
(the BE-012 worker hands one to every job).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from tennis_backend.application.worker import CancellationToken
from tennis_backend.contracts.validation import ContractRegistry, validate_result
from tennis_backend.domain.models import DomainViolation, MediaProbe

#: Analysis stages an adapter can implement, mapped to the frozen methods
#: whose output definition each stage result must satisfy.
STAGE_METHODS: dict[str, str] = {
    "strokes": "analysis.strokes",
    "timing": "analysis.timing",
    "metrics": "analysis.metrics",
}

_ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
_STAGE_UNSUPPORTED = "ADAPTER_STAGE_UNSUPPORTED"
_SIMULATION_MISMATCH = "ADAPTER_SIMULATION_MISMATCH"


@dataclass(frozen=True)
class AdapterCapabilities:
    """What an adapter claims to be able to do — checked before every run."""

    kind: str
    simulation: bool
    supported_stages: tuple[str, ...]
    #: Explicit, user-visible limitations; a real adapter that is not
    #: installed or validated must say so here, never silently no-op.
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class StageRequest:
    """Typed inputs for one analysis stage run."""

    task_id: str
    version: int
    media: MediaProbe
    fixture_id: str | None = None


class AnalysisAdapter(ABC):
    """Abstract typed interface every analysis source implements."""

    def __init__(self, *, kind: str, simulation: bool) -> None:
        if kind == "fixture" and simulation is not True:
            # a fixture is always a simulation; claiming otherwise would turn
            # the stand-in into fake "real" inference
            raise DomainViolation(
                _SIMULATION_MISMATCH, "a fixture adapter must declare simulation=true"
            )
        if kind != "fixture" and simulation is not False:
            raise DomainViolation(
                _SIMULATION_MISMATCH, f"a {kind} adapter must declare simulation=false"
            )
        self._kind = kind
        self._simulation = simulation

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def simulation(self) -> bool:
        return self._simulation

    @abstractmethod
    def capabilities(self) -> AdapterCapabilities:
        """Declare supported stages and explicit limitations."""

    @abstractmethod
    def run(
        self, stage: str, request: StageRequest, *, cancel: CancellationToken
    ) -> dict[str, Any]:
        """Run one stage and return its frozen contract result shape."""

    def assert_stage_supported(self, stage: str) -> None:
        caps = self.capabilities()
        if stage not in caps.supported_stages or stage not in STAGE_METHODS:
            raise DomainViolation(
                _STAGE_UNSUPPORTED,
                f"adapter {self._kind} does not support stage {stage}",
            )


class UnavailableAdapter(AnalysisAdapter):
    """Honest placeholder for a capability that does not exist yet.

    Declares no supported stages, refuses every run with
    ``ADAPTER_UNAVAILABLE`` and never returns data — used for the real pose
    model until it is installed and validated (real-pose increment).
    """

    def __init__(self, *, kind: str, limitations: tuple[str, ...]) -> None:
        super().__init__(kind=kind, simulation=False)
        if not limitations:
            raise DomainViolation(
                _ADAPTER_UNAVAILABLE, "an unavailable adapter must state its limitations"
            )
        self._limitations = limitations

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            kind=self._kind,
            simulation=self._simulation,
            supported_stages=(),
            limitations=self._limitations,
        )

    def run(
        self, stage: str, request: StageRequest, *, cancel: CancellationToken
    ) -> dict[str, Any]:
        self.assert_stage_supported(stage)  # always refuses: no stages supported
        raise DomainViolation(
            _ADAPTER_UNAVAILABLE, f"{self._kind} is not available"
        )  # pragma: no cover


def run_stage(
    adapter: AnalysisAdapter,
    stage: str,
    request: StageRequest,
    *,
    cancel: CancellationToken,
    registry: ContractRegistry,
) -> dict[str, Any]:
    """Run one stage and validate its result against the frozen contracts.

    The result must satisfy the stage's frozen output definition and carry
    exactly the adapter's declared simulation flag; anything else fails
    loudly instead of reaching a caller.
    """
    adapter.assert_stage_supported(stage)
    cancel.check_point()  # refuse to start work that is already cancelled
    result = adapter.run(stage, request, cancel=cancel)
    method = STAGE_METHODS[stage]
    validated = validate_result(registry, method, result)
    if validated.simulation is not adapter.simulation:
        raise DomainViolation(
            _SIMULATION_MISMATCH,
            f"adapter {adapter.kind} returned simulation={validated.simulation}"
            f" for a simulation={adapter.simulation} adapter",
        )
    return result
