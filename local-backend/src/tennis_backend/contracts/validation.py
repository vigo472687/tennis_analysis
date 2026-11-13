"""Validation layer for the frozen Codex-Claude wire contracts (v1).

The draft-07 schemas in ``contracts/v1`` are authoritative and frozen
(``integration/reports/contract-baseline.json``). This module does two things
and nothing else:

1. It validates payloads against the frozen schemas with a local
   ``referencing`` registry, so ``$ref`` targets resolve inside the nine
   contract files and never touch the network.
2. It exposes typed Pydantic shapes plus a method registry that mirror the
   frozen definitions, so the later transport batch can hand typed objects to
   the application layer instead of raw dictionaries.

Nothing here adds meaning the contracts do not have: every check either comes
from a schema keyword, from a documented rule in ``contracts/README.md``, or
from cross-field consistency (simulation flags, operation id, evidence
exclusion). Schema changes stay owned by Codex; a mismatch between the frozen
schemas and this module fails loudly instead of being worked around locally.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError as JsonSchemaError
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from pydantic import ValidationError as PydanticValidationError
from referencing import Registry
from referencing import Resource as ReferencingResource
from referencing.jsonschema import DRAFT7

#: Frozen wire contract this module validates against (contracts/v1).
CONTRACT_VERSION = "1.1.0"

#: Base URI all contract ``$id`` values share; refs stay local to it.
BASE_URI = "https://tennis.local/contracts/v1/"

#: The nine frozen schema files, in contract order.
SCHEMA_FILES: tuple[str, ...] = (
    "analysis.schema.json",
    "common.schema.json",
    "deletion.schema.json",
    "error.schema.json",
    "feedback.schema.json",
    "media.schema.json",
    "report-input.schema.json",
    "runtime.schema.json",
    "task.schema.json",
)

#: Envelope and error definitions used by every method.
REQUEST_REF = f"{BASE_URI}common.schema.json#/definitions/request"
RESPONSE_REF = f"{BASE_URI}common.schema.json#/definitions/response"

_DEFAULT_CONTRACTS_DIR = Path(__file__).resolve().parents[4] / "contracts" / "v1"

# README: evidence resources exclude target_pose; target pose has its own
# result type.
_TARGET_POSE = "target_pose"


class ContractViolation(Exception):
    """A payload, schema set or method does not satisfy the frozen contracts.

    ``code`` is a stable machine-readable code, ``violations`` carries one
    human-readable message per offending location. Messages never contain raw
    media or credential material because the contract layer only sees JSON.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "CONTRACT_INVALID",
        violations: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.violations = tuple(violations)


# ---------------------------------------------------------------------------
# Typed shapes mirroring the frozen definitions
# ---------------------------------------------------------------------------


def _validate_relative_path(value: str) -> str:
    """Reject absolute paths, drive letters and ``..`` traversal segments.

    The frozen ``relative_path`` pattern uses look-around, which the Pydantic
    core regex engine does not support, so the identical frozen pattern is
    applied with the standard ``re`` module here.
    """
    pattern = r"^(?!/)(?![A-Za-z]:)(?!.*(?:^|/)\.\.(?:/|$))[^\x00]+$"
    if re.search(pattern, value) is None:
        raise ValueError("must be a relative path that stays inside an authorized root")
    return value


Id = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"),
]
RelativePath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=1024),
    AfterValidator(_validate_relative_path),
]
ErrorCode = Annotated[
    str,
    StringConstraints(min_length=1, max_length=80, pattern=r"^[A-Z][A-Z0-9_]+$"),
]
MethodName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=96, pattern=r"^[a-z]+\.[a-z_]+$"),
]
Sha256Hex = Annotated[
    str, StringConstraints(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=128)]
VersionTag = Annotated[str, StringConstraints(min_length=1, max_length=32)]
DigestRef = Annotated[str, StringConstraints(min_length=1, max_length=128)] | None

StateOfValue = Literal[
    "observed",
    "unknown",
    "not_provided",
    "not_visible",
    "not_applicable",
    "insufficient_evidence",
]
ConfidenceLevel = Literal["high", "medium", "low", "insufficient_evidence"]
ResourceKind = Literal[
    "input_copy",
    "evidence_frame",
    "evidence_clip",
    "trajectory",
    "target_pose",
    "report",
    "diagnostics",
    "record",
]
EvidenceResourceKind = Literal["evidence_frame", "evidence_clip", "trajectory"]
EvidenceKind = Literal["video_frame", "video_clip", "trajectory", "user_statement", "quality_frame"]
ConsentPurpose = Literal[
    "analysis",
    "save",
    "retest",
    "network",
    "share",
    "email",
    "diagnostics",
    "improvement",
    "training_data",
]
Route = Literal["question_answer", "tactics", "technical_diagnosis", "style_optimization"]
TargetStroke = Literal[
    "forehand",
    "single_backhand",
    "double_backhand",
    "serve",
    "forehand_volley",
    "backhand_volley",
    "footwork",
]
AgeBand = Literal["adult", "youth"]
Handedness = Literal["left", "right", "ambidextrous"]
SafetyFlag = Literal[
    "clear",
    "pain",
    "injury",
    "dizziness",
    "worsening",
    "rehab",
    "fatigue",
    "uncertain",
]
TaskState = Literal[
    "draft",
    "waiting_for_capability",
    "checking_input",
    "analyzing",
    "researching",
    "rendering",
    "complete",
    "partial",
    "reshoot_required",
    "unsupported",
    "safety_stop",
    "failed",
    "cancelled",
    "deleted",
]
TaskStage = Literal["input", "media", "analysis", "research", "render", "export", "delete", "none"]
CheckpointName = Literal["checking_input", "analyzing", "researching", "rendering"]
RecoveryAction = Literal["retry", "reshoot", "repair", "cancel", "delete", "ask", "none"]
ErrorStage = Literal["runtime", "input", "media", "analysis", "render", "export", "delete"]
UserAction = Literal["retry", "reshoot", "confirm", "repair", "export", "delete", "none"]
RuntimeState = Literal[
    "not_initialized",
    "awaiting_consent",
    "preparing",
    "verifying",
    "installed_stopped",
    "starting",
    "ready",
    "repair_required",
    "upgrading",
    "uninstalling",
]
RuntimeBackend = Literal["fixture", "python"]
RuntimeIntent = Literal["prepare", "upgrade", "rollback"]
RuntimeRequirement = Literal["required", "not_required", "uncertain"]
StrokeKind = Literal[
    "forehand",
    "single_backhand",
    "double_backhand",
    "serve",
    "forehand_volley",
    "backhand_volley",
    "footwork",
    "unable",
]
TimingEventKind = Literal[
    "preparation",
    "first_step",
    "main_movement",
    "moving_backswing",
    "bounce_candidate",
    "adjustment",
    "support",
    "racket_drop",
    "leg_drive_rotation",
    "forward_swing",
    "recovery",
]
VisibleObject = Literal["body", "feet", "ball", "racket"]
MetricUnit = Literal["relative", "normalized", "ratio", "event_order"]
RenderMode = Literal["frame", "trajectory", "slow_motion"]
QualityStatus = Literal["pass", "limited", "fail"]
QualityObject = Literal[
    "body", "feet", "ball", "racket", "subject", "direction", "lighting", "media"
]
ContainerKind = Literal["mp4", "mov", "unknown"]
RotationKind = Literal["0", "90", "180", "270", "unknown"]
TriState = Literal["yes", "no", "unknown"]
PanoramaKind = Literal["flat", "native", "unknown"]
SupportKind = Literal["supported", "unsupported", "unable"]
ObservationKind = Literal["measured", "simulated"]
DeleteScope = Literal["task", "all_demo"]
DeleteItemStatus = Literal["deleted", "failed", "outside_control", "not_found"]


class _Frozen(BaseModel):
    """Base shape: unknown fields are rejected, instances stay immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_unique(values: list[Any]) -> list[Any]:
    if len(set(values)) != len(values):
        raise ValueError("must not contain duplicate entries")
    return values


class Resource(_Frozen):
    """``common:resource`` - a controlled local reference, never raw media."""

    resource_id: Id
    path: RelativePath
    mime: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    sha256: Sha256Hex
    size_bytes: int = Field(ge=0)
    kind: ResourceKind
    simulation: bool


class EvidenceResource(Resource):
    """``common:resource`` narrowed by ``analysis:evidence`` to evidence kinds."""

    kind: EvidenceResourceKind


class TargetPoseResource(Resource):
    """``common:resource`` narrowed by ``analysis:target_result`` to target_pose."""

    kind: Literal["target_pose"]


class Consent(_Frozen):
    """``common:consent`` - a fresh snapshot bound to one task version."""

    consent_id: Id
    task_version: int = Field(ge=1)
    purposes: Annotated[list[ConsentPurpose], Field(max_length=9), AfterValidator(_require_unique)]
    text_version: Id
    confirmed: bool
    guardian_simulated: bool
    recipient_digest: DigestRef
    materials_digest: DigestRef


class ErrorModel(_Frozen):
    """``error:error`` - the unified structured error package."""

    code: ErrorCode
    stage: ErrorStage
    retryable: bool
    user_action: UserAction
    retained: list[LongText] = Field(max_length=200)
    details_ref: RelativePath | None
    simulation: bool


class Request(_Frozen):
    """``common:request`` - one NDJSON request line."""

    contract_version: Literal["1.1.0"]
    request_id: Id
    operation_id: Id | None
    method: MethodName
    params: dict[str, Any]
    simulation: bool


class Response(_Frozen):
    """``common:response`` - one NDJSON response line.

    ``result`` stays ``Any`` because the frozen envelope allows any value and
    per-method shapes are enforced separately by ``validate_result``.
    """

    contract_version: Literal["1.1.0"]
    request_id: Id
    ok: bool
    result: Any
    error: ErrorModel | None
    simulation: bool

    @model_validator(mode="after")
    def _check_ok_pairing(self) -> Response:
        if self.ok and self.error is not None:
            raise ValueError("ok=true responses must not carry an error")
        if not self.ok:
            if self.result is not None:
                raise ValueError("ok=false responses must carry result=null")
            if self.error is None:
                raise ValueError("ok=false responses must carry a structured error")
        return self


class User(_Frozen):
    """``task:user`` - declared context, never a measured profile."""

    age_band: AgeBand
    handedness: Handedness
    target_stroke: TargetStroke
    goal: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    exercise_background: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    safety: SafetyFlag


class CreateInput(_Frozen):
    """``task:create_input`` (contract 1.1.0).

    ``analysis_mode`` selects the analysis source: ``fixture`` (default,
    requires ``fixture_id``) or ``real_video`` (approved user video analyzed
    by ``media_ref``; ``fixture_id`` must stay unset — masquerading a real
    video as a frozen sample is a contract violation).
    """

    operation_id: Id
    analysis_mode: Literal["fixture", "real_video"] = "fixture"
    fixture_id: Id | None = None
    media_ref: RelativePath
    user: User
    consent: Consent
    primary_route: Route
    auxiliary_routes: Annotated[list[Route], Field(max_length=2), AfterValidator(_require_unique)]
    subject_id: Id | None
    mirror_confirmed: bool

    @model_validator(mode="after")
    def _check_mode_fixture_pairing(self) -> CreateInput:
        if self.analysis_mode == "real_video" and self.fixture_id is not None:
            raise ValueError(
                "analysis_mode=real_video must not carry fixture_id; "
                "a real video must not masquerade as a frozen sample"
            )
        if self.analysis_mode == "fixture" and self.fixture_id is None:
            raise ValueError("analysis_mode=fixture requires fixture_id")
        return self


class Lookup(_Frozen):
    """``task:lookup``."""

    task_id: Id
    version: int = Field(ge=1)


class CancelInput(_Frozen):
    """``task:cancel_input``."""

    operation_id: Id
    task_id: Id
    version: int = Field(ge=1)


class TaskSummary(_Frozen):
    """``task:summary``."""

    task_id: Id
    version: int = Field(ge=1)
    state: TaskState
    stage: TaskStage
    checkpoints: Annotated[
        list[CheckpointName], Field(max_length=200), AfterValidator(_require_unique)
    ]
    retained: list[LongText] = Field(max_length=200)
    recovery_actions: list[RecoveryAction] = Field(max_length=200)
    workspace_ref: RelativePath
    simulation: bool
    notes: list[LongText] = Field(default_factory=list, max_length=200)


class RuntimeStatusInput(_Frozen):
    """``runtime:status_input``."""

    runtime_requirement: RuntimeRequirement


class RuntimePrepareInput(_Frozen):
    """``runtime:prepare_input``."""

    operation_id: Id
    consent_confirmed: Literal[True]
    target_version: VersionTag
    intent: RuntimeIntent
    scope_digest: DigestRef


class RuntimeStartInput(_Frozen):
    """``runtime:start_input``."""

    operation_id: Id
    runtime_requirement: Literal["required"]


class RuntimeRepairInput(_Frozen):
    """``runtime:repair_input``."""

    operation_id: Id
    consent_confirmed: Literal[True]
    scope_digest: DigestRef


class RuntimeUninstallInput(_Frozen):
    """``runtime:uninstall_input``."""

    operation_id: Id
    confirmed: Literal[True]
    keep_project_data: bool


class RuntimeCheck(_Frozen):
    """``runtime:result`` checks item."""

    code: Id
    passed: bool


class RuntimeResult(_Frozen):
    """``runtime:result``."""

    state: RuntimeState
    version: VersionTag | None
    backend: RuntimeBackend
    checks: list[RuntimeCheck] = Field(max_length=200)
    retained: list[LongText] = Field(max_length=200)
    simulation: bool


class Evidence(_Frozen):
    """``analysis:evidence`` - an observation linked to its own resource.

    Target pose output is never evidence, so ``resource.kind`` is narrowed to
    the evidence kinds; the frozen schema applies the same narrowing through
    ``allOf``.
    """

    evidence_id: Id
    kind: EvidenceKind
    asset_id: Id
    subject_id: Id
    original_start_s: float = Field(ge=0)
    original_end_s: float = Field(ge=0)
    visible_objects: list[VisibleObject] = Field(max_length=200)
    resource: EvidenceResource | None
    simulation: bool

    @model_validator(mode="after")
    def _check_ordered_interval(self) -> Evidence:
        # README: nonnegative ordered intervals.
        if self.original_end_s < self.original_start_s:
            raise ValueError("original_end_s must not be before original_start_s")
        return self


class Stroke(_Frozen):
    """``analysis:stroke``."""

    event_id: Id
    kind: StrokeKind
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    phases: list[LongText] = Field(max_length=200)
    confidence: ConfidenceLevel
    evidence_ids: list[Id] = Field(max_length=200)

    @model_validator(mode="after")
    def _check_ordered_interval(self) -> Stroke:
        if self.end_s < self.start_s:
            raise ValueError("end_s must not be before start_s")
        return self


class StrokesResult(_Frozen):
    """``analysis:strokes_result``."""

    events: list[Stroke] = Field(max_length=200)
    simulation: bool


class TimingEvent(_Frozen):
    """``analysis:timing_event``."""

    event_id: Id
    kind: TimingEventKind
    original_s: float | None = Field(default=None, ge=0)
    value_state: StateOfValue
    requires: list[VisibleObject] = Field(max_length=200)
    evidence_ids: list[Id] = Field(max_length=200)


class TimingResult(_Frozen):
    """``analysis:timing_result``."""

    events: list[TimingEvent] = Field(max_length=200)
    limitations: list[LongText] = Field(max_length=200)
    simulation: bool


class Metric(_Frozen):
    """``analysis:metric`` - relative or normalized only, never physical force."""

    metric_id: Id
    definition: LongText
    value: float | str | None
    value_state: StateOfValue
    unit: MetricUnit
    reference: LongText
    evidence_ids: list[Id] = Field(max_length=200)
    limitations: list[LongText] = Field(max_length=200)


class MetricsResult(_Frozen):
    """``analysis:metrics_result``."""

    metrics: list[Metric] = Field(max_length=200)
    simulation: bool


class RenderInput(_Frozen):
    """``analysis:render_input``."""

    operation_id: Id
    task_id: Id
    version: int = Field(ge=1)
    evidence_ids: list[Id] = Field(min_length=1, max_length=200)
    mode: RenderMode


class RenderResult(_Frozen):
    """``analysis:render_result``.

    Render output carries evidence resources only; target pose has its own
    result type (``analysis:target_result``).
    """

    resources: list[Resource] = Field(max_length=200)
    evidence: list[Evidence] = Field(max_length=200)
    simulation: bool

    @model_validator(mode="after")
    def _check_no_target_pose(self) -> RenderResult:
        violations = [
            resource.resource_id for resource in self.resources if resource.kind == _TARGET_POSE
        ]
        if violations:
            raise ValueError(
                f"target_pose resources belong to target_result, not render: {violations}"
            )
        return self


TargetFeature = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class TargetInput(_Frozen):
    """``analysis:target_input``."""

    operation_id: Id
    task_id: Id
    version: int = Field(ge=1)
    features: list[TargetFeature] = Field(min_length=1, max_length=6)


class TargetResult(_Frozen):
    """``analysis:target_result`` - a generic simulated skeleton, not a pose claim."""

    kind: Literal["target_pose"]
    resource: TargetPoseResource
    features: list[LongText] = Field(max_length=200)
    limitations: list[LongText] = Field(max_length=200)
    simulation: bool


class CompareInput(_Frozen):
    """``analysis:compare_input``."""

    task_id: Id
    version: int = Field(ge=1)
    other_task_id: Id
    other_version: int = Field(ge=1)


class CompareResult(_Frozen):
    """``analysis:compare_result``."""

    comparable: bool
    differences: list[LongText] = Field(max_length=200)
    changes: list[Metric] = Field(max_length=200)
    simulation: bool

    @model_validator(mode="after")
    def _check_not_comparable(self) -> CompareResult:
        if not self.comparable and self.changes:
            raise ValueError("comparable=false results must not carry metric changes")
        return self


class MediaSummary(_Frozen):
    """``media:summary`` - probe metadata; never a real pose or support claim."""

    container: ContainerKind
    codec: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    duration_s: float = Field(ge=0)
    fps: float = Field(ge=0)
    width: int = Field(ge=0)
    height: int = Field(ge=0)
    rotation: RotationKind
    slow_motion: TriState
    hdr: TriState
    device_source: ShortText
    panorama: PanoramaKind
    support: SupportKind
    observation_kind: ObservationKind


class QualityIssue(_Frozen):
    """``media:quality`` issues item."""

    code: Id
    object: QualityObject
    impact: LongText
    evidence_ids: list[Id] = Field(max_length=200)
    reshoot_guidance: LongText


class Quality(_Frozen):
    """``media:quality``."""

    status: QualityStatus
    issues: list[QualityIssue] = Field(max_length=200)
    unavailable_objects: list[VisibleObject] = Field(max_length=200)
    subject_candidates: list[Id] = Field(max_length=200)
    time_mapping_valid: bool
    media: MediaSummary
    evidence: list[Evidence] = Field(max_length=200)
    simulation: bool


class TimeMappingPoint(_Frozen):
    """``media:time_mapping`` item (``normalized_s`` is always fixture-derived)."""

    normalized_s: float = Field(ge=0)
    original_s: float = Field(ge=0)


class ExportInput(_Frozen):
    """``deletion:export_input``."""

    operation_id: Id
    task_id: Id
    version: int = Field(ge=1)
    selected_resource_ids: list[Id] = Field(min_length=1, max_length=200)
    consent: Consent


class ExportResult(_Frozen):
    """``deletion:export_result``."""

    package_id: Id
    manifest: Resource
    resources: list[Resource] = Field(max_length=200)
    redacted: bool
    simulation: bool


class DeleteInput(_Frozen):
    """``deletion:delete_input``."""

    operation_id: Id
    task_id: Id | None
    version: int | None = Field(default=None, ge=1)
    scope: DeleteScope
    confirmed: Literal[True]


class DeleteItem(_Frozen):
    """``deletion:delete_result`` items item."""

    resource_id: Id
    status: DeleteItemStatus
    code: Annotated[str, StringConstraints(min_length=1, max_length=80)] | None


class DeleteResult(_Frozen):
    """``deletion:delete_result``."""

    operation_id: Id
    items: list[DeleteItem] = Field(max_length=200)
    complete: bool
    external_copies_uncontrolled: Literal[True]
    runtime_uninstalled: Literal[False]
    simulation: bool


#: Typed shape per ``file:definition``; bare definitions belong to that file.
_TYPED_DEFINITIONS: dict[tuple[str, str], Any] = {
    ("common", "request"): Request,
    ("common", "response"): Response,
    ("common", "resource"): Resource,
    ("common", "consent"): Consent,
    ("error", "error"): ErrorModel,
    ("task", "create_input"): CreateInput,
    ("task", "lookup"): Lookup,
    ("task", "cancel_input"): CancelInput,
    ("task", "summary"): TaskSummary,
    ("runtime", "status_input"): RuntimeStatusInput,
    ("runtime", "prepare_input"): RuntimePrepareInput,
    ("runtime", "start_input"): RuntimeStartInput,
    ("runtime", "repair_input"): RuntimeRepairInput,
    ("runtime", "uninstall_input"): RuntimeUninstallInput,
    ("runtime", "result"): RuntimeResult,
    ("media", "summary"): MediaSummary,
    ("media", "quality"): Quality,
    ("media", "time_mapping"): list[TimeMappingPoint],
    ("analysis", "strokes_result"): StrokesResult,
    ("analysis", "timing_result"): TimingResult,
    ("analysis", "metrics_result"): MetricsResult,
    ("analysis", "render_input"): RenderInput,
    ("analysis", "render_result"): RenderResult,
    ("analysis", "target_input"): TargetInput,
    ("analysis", "target_result"): TargetResult,
    ("analysis", "compare_input"): CompareInput,
    ("analysis", "compare_result"): CompareResult,
    ("deletion", "export_input"): ExportInput,
    ("deletion", "export_result"): ExportResult,
    ("deletion", "delete_input"): DeleteInput,
    ("deletion", "delete_result"): DeleteResult,
}


# ---------------------------------------------------------------------------
# Semantic checks documented in contracts/README.md
# ---------------------------------------------------------------------------


def check_simulation_consistency(
    value: Any,
    *,
    expected: bool,
    path: str = "$",
) -> tuple[str, ...]:
    """Return violations where nested ``simulation`` flags contradict ``expected``.

    Every simulated payload must be marked consistently: an envelope that says
    ``simulation=true`` cannot carry a result claiming ``simulation=false``.
    """
    violations: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key == "simulation":
                if not isinstance(item, bool):
                    violations.append(
                        f"{child}: simulation must be a boolean, got {type(item).__name__}"
                    )
                elif item is not expected:
                    violations.append(f"{child}: simulation={item} contradicts expected {expected}")
            else:
                violations.extend(check_simulation_consistency(item, expected=expected, path=child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            violations.extend(
                check_simulation_consistency(item, expected=expected, path=f"{path}[{index}]")
            )
    return tuple(violations)


def check_target_pose_not_evidence(value: Any, path: str = "$") -> tuple[str, ...]:
    """Return violations where a ``target_pose`` resource is presented as evidence.

    README: ``Evidence.kind`` and its ``resource.kind`` exclude target_pose, and
    target resources have their own result type.
    """
    violations: list[str] = []
    if isinstance(value, Mapping):
        if "evidence_id" in value:
            resource = value.get("resource")
            kind = resource.get("kind") if isinstance(resource, Mapping) else None
            if kind == _TARGET_POSE:
                violations.append(
                    f"{path}: target_pose resource used as evidence {value.get('evidence_id')!r}"
                )
        for key, item in value.items():
            violations.extend(check_target_pose_not_evidence(item, path=f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            violations.extend(check_target_pose_not_evidence(item, path=f"{path}[{index}]"))
    return tuple(violations)


# ---------------------------------------------------------------------------
# Contract registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolBinding:
    """One MCP tool: NDJSON method plus frozen input/output definitions."""

    name: str
    method: str
    input_ref: str
    output_ref: str
    side_effect: bool
    purposes: tuple[str, ...]


_M = TypeVar("_M", bound=BaseModel)


@dataclass(frozen=True)
class ValidatedRequest:
    """A request that passed envelope, method and params validation."""

    request_id: str
    operation_id: str | None
    method: str
    tool: ToolBinding
    raw_params: Mapping[str, Any]
    params: Any
    simulation: bool

    def typed_params(self, model: type[_M]) -> _M:
        """Return ``params`` as the requested typed shape, or fail loudly."""
        if not isinstance(self.params, model):
            raise ContractViolation(
                f"method {self.method} params do not match {model.__name__}",
                code="CONTRACT_SHAPE_MISMATCH",
            )
        return self.params


@dataclass(frozen=True)
class ValidatedResult:
    """A method result that passed schema, typed shape and consistency checks."""

    method: str
    tool: ToolBinding
    result: Any
    simulation: bool


class ContractRegistry:
    """Loads the nine frozen schemas and resolves every reference locally."""

    def __init__(
        self,
        *,
        registry: Registry,
        schemas: Mapping[str, Mapping[str, Any]],
        contract_version: Literal["1.1.0"],
    ) -> None:
        self._registry = registry
        self._schemas = dict(schemas)
        self._contract_version: Literal["1.1.0"] = contract_version
        self._tools = self._load_tools()

    @classmethod
    def load(cls, contracts_dir: Path | None = None) -> ContractRegistry:
        """Load all nine frozen schemas from ``contracts_dir`` (no network)."""
        directory = contracts_dir if contracts_dir is not None else _DEFAULT_CONTRACTS_DIR
        schemas: dict[str, dict[str, Any]] = {}
        resources: list[tuple[str, ReferencingResource]] = []
        for name in SCHEMA_FILES:
            contents = _read_schema(directory / name)
            schemas[name[: -len(".schema.json")]] = contents
            resource = ReferencingResource.from_contents(contents, default_specification=DRAFT7)
            base = resource.id() or f"{BASE_URI}{name}"
            if base != f"{BASE_URI}{name}":
                raise ContractViolation(
                    f"unexpected frozen $id {base!r} in {directory / name}",
                    code="CONTRACT_FILE_INVALID",
                )
            resources.append((base, resource))
        registry = Registry().with_resources(resources)
        version: Literal["1.1.0"] = schemas["common"]["definitions"]["request"]["properties"][
            "contract_version"
        ]["const"]
        if version != CONTRACT_VERSION:
            raise ContractViolation(
                f"frozen contract version {version!r} is not supported"
                f" by this backend ({CONTRACT_VERSION!r})",
                code="CONTRACT_VERSION_MISMATCH",
            )
        return cls(registry=registry, schemas=schemas, contract_version=version)

    @property
    def contract_version(self) -> Literal["1.1.0"]:
        """The frozen wire contract version loaded from ``common``."""
        return self._contract_version

    @property
    def tools(self) -> tuple[ToolBinding, ...]:
        """All tool bindings declared by the frozen ``x-tools`` registries."""
        return self._tools

    def schema(self, file: str) -> Mapping[str, Any]:
        """Return the raw frozen contents of one schema file."""
        try:
            return self._schemas[file]
        except KeyError as error:
            raise ContractViolation(
                f"unknown contract file {file!r}", code="CONTRACT_FILE_UNKNOWN"
            ) from error

    def definition_ref(self, file: str, definition: str) -> str:
        """Build the absolute reference for ``file:definition`` addressing."""
        try:
            self.schema(file)["definitions"][definition]
        except KeyError as error:
            raise ContractViolation(
                f"unknown contract definition {file}:{definition}",
                code="CONTRACT_DEFINITION_UNKNOWN",
            ) from error
        return f"{BASE_URI}{file}.schema.json#/definitions/{definition}"

    def typed_definition(self, file: str, definition: str) -> Any:
        """Return the typed shape registered for ``file:definition``."""
        try:
            return _TYPED_DEFINITIONS[(file, definition)]
        except KeyError as error:
            raise ContractViolation(
                f"no typed binding for contract definition {file}:{definition}",
                code="CONTRACT_SHAPE_MISSING",
            ) from error

    def tool(self, method: str) -> ToolBinding:
        """Return the tool binding for an NDJSON method."""
        for tool in self._tools:
            if tool.method == method:
                return tool
        raise ContractViolation(f"unknown method {method!r}", code="METHOD_UNKNOWN")

    def validate(self, ref: str, instance: Any) -> tuple[str, ...]:
        """Validate ``instance`` against a frozen definition; return messages."""
        validator = Draft7Validator({"$ref": ref}, registry=self._registry)
        return tuple(
            f"{error.json_path}: {error.message}"
            for error in sorted(validator.iter_errors(instance), key=_sort_key)
        )

    def require_valid(self, ref: str, instance: Any, *, code: str = "CONTRACT_INVALID") -> None:
        """Validate and raise :class:`ContractViolation` on any message."""
        violations = self.validate(ref, instance)
        if violations:
            raise ContractViolation(
                f"payload does not satisfy {ref}",
                code=code,
                violations=violations,
            )

    def _load_tools(self) -> tuple[ToolBinding, ...]:
        tools: list[ToolBinding] = []
        seen: set[str] = set()
        for file in sorted(self._schemas):
            for entry in self._schemas[file].get("x-tools", ()):
                binding = ToolBinding(
                    name=entry["name"],
                    method=entry["method"],
                    input_ref=self._resolve_addressing(file, entry["input"]),
                    output_ref=self._resolve_addressing(file, entry["output"]),
                    side_effect=entry["side_effect"],
                    purposes=tuple(entry["purposes"]),
                )
                if binding.method in seen:
                    raise ContractViolation(
                        f"duplicate tool method {binding.method!r}",
                        code="CONTRACT_REGISTRY_INVALID",
                    )
                seen.add(binding.method)
                self.typed_definition(*_split_ref(binding.input_ref))
                self.typed_definition(*_split_ref(binding.output_ref))
                tools.append(binding)
        return tuple(tools)

    def _resolve_addressing(self, declaring_file: str, address: str) -> str:
        file, _, definition = address.partition(":")
        if not definition:
            file, definition = declaring_file, address
        return self.definition_ref(file, definition)


def _sort_key(error: JsonSchemaError) -> tuple[str, str]:
    return (error.json_path, error.message)


def _read_schema(path: Path) -> dict[str, Any]:
    try:
        contents = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ContractViolation(
            f"missing frozen contract {path}", code="CONTRACT_FILE_MISSING"
        ) from error
    except OSError as error:
        raise ContractViolation(
            f"unreadable frozen contract {path}: {error}",
            code="CONTRACT_FILE_UNREADABLE",
        ) from error
    except ValueError as error:
        raise ContractViolation(
            f"invalid JSON in frozen contract {path}: {error}",
            code="CONTRACT_FILE_INVALID",
        ) from error
    if not isinstance(contents, dict):
        raise ContractViolation(
            f"frozen contract {path} is not a JSON object", code="CONTRACT_FILE_INVALID"
        )
    return contents


# ---------------------------------------------------------------------------
# Transport-facing validation entry points
# ---------------------------------------------------------------------------


def validate_request(registry: ContractRegistry, payload: Mapping[str, Any]) -> ValidatedRequest:
    """Validate one request line: envelope, method, params and operation id."""
    data = dict(payload)
    registry.require_valid(REQUEST_REF, data, code="REQUEST_ENVELOPE_INVALID")
    request = _model_validate(Request, data, code="REQUEST_ENVELOPE_INVALID")
    tool = registry.tool(request.method)
    registry.require_valid(tool.input_ref, request.params, code="REQUEST_PARAMS_INVALID")
    params = _model_validate(
        registry.typed_definition(*_split_ref(tool.input_ref)),
        request.params,
        code="REQUEST_PARAMS_INVALID",
    )
    violations = list(check_simulation_consistency(data, expected=request.simulation))
    violations.extend(_check_operation_id(request, tool))
    if violations:
        raise ContractViolation(
            f"request {request.request_id} violates the frozen contracts",
            code="REQUEST_INCONSISTENT",
            violations=violations,
        )
    return ValidatedRequest(
        request_id=request.request_id,
        operation_id=request.operation_id,
        method=request.method,
        tool=tool,
        raw_params=request.params,
        params=params,
        simulation=request.simulation,
    )


def validate_result(registry: ContractRegistry, method: str, result: Any) -> ValidatedResult:
    """Validate a method result against its frozen output definition."""
    tool = registry.tool(method)
    registry.require_valid(tool.output_ref, result, code="RESULT_INVALID")
    typed = _model_validate(
        registry.typed_definition(*_split_ref(tool.output_ref)),
        result,
        code="RESULT_INVALID",
    )
    violations = list(check_simulation_consistency(result, expected=typed.simulation))
    violations.extend(check_target_pose_not_evidence(result))
    if violations:
        raise ContractViolation(
            f"result for {method} violates the frozen contracts",
            code="RESULT_INCONSISTENT",
            violations=violations,
        )
    return ValidatedResult(method=method, tool=tool, result=typed, simulation=typed.simulation)


def validate_response(
    registry: ContractRegistry,
    payload: Mapping[str, Any],
    *,
    method: str | None = None,
) -> Response:
    """Validate one response line, optionally against a method output shape."""
    data = dict(payload)
    registry.require_valid(RESPONSE_REF, data, code="RESPONSE_ENVELOPE_INVALID")
    response = _model_validate(Response, data, code="RESPONSE_ENVELOPE_INVALID")
    violations = list(check_simulation_consistency(data, expected=response.simulation))
    violations.extend(check_target_pose_not_evidence(data.get("result")))
    if response.ok and method is not None:
        validated = validate_result(registry, method, response.result)
        if validated.simulation is not response.simulation:
            violations.append("$.simulation: envelope and result simulation flags differ")
    if violations:
        raise ContractViolation(
            f"response for request {response.request_id} violates the frozen contracts",
            code="RESPONSE_INCONSISTENT",
            violations=violations,
        )
    return response


def build_error(
    *,
    code: str,
    stage: ErrorStage,
    retryable: bool = False,
    user_action: UserAction = "none",
    retained: Sequence[str] = (),
    details_ref: str | None = None,
    simulation: bool = True,
) -> dict[str, Any]:
    """Build an ``error:error`` payload without hand-writing dictionaries."""
    model = _model_validate(
        ErrorModel,
        {
            "code": code,
            "stage": stage,
            "retryable": retryable,
            "user_action": user_action,
            "retained": list(retained),
            "details_ref": details_ref,
            "simulation": simulation,
        },
        code="ERROR_INVALID",
    )
    return model.model_dump()


def build_response(
    *,
    request_id: str,
    method: str,
    registry: ContractRegistry,
    result: Any,
    simulation: bool = True,
) -> dict[str, Any]:
    """Build a successful ``common:response`` payload for ``method``."""
    validated = validate_result(registry, method, result)
    if validated.simulation is not simulation:
        raise ContractViolation(
            "envelope and result simulation flags differ",
            code="RESPONSE_INCONSISTENT",
            violations=("$.simulation",),
        )
    payload = Response(
        contract_version=registry.contract_version,
        request_id=request_id,
        ok=True,
        result=result,
        error=None,
        simulation=simulation,
    )
    return payload.model_dump()


def build_error_response(
    *,
    request_id: str,
    registry: ContractRegistry,
    error: Mapping[str, Any],
    simulation: bool = True,
) -> dict[str, Any]:
    """Build a failing ``common:response`` payload with a structured error."""
    error_model = _model_validate(ErrorModel, dict(error), code="ERROR_INVALID")
    payload = Response(
        contract_version=registry.contract_version,
        request_id=request_id,
        ok=False,
        result=None,
        error=error_model,
        simulation=simulation,
    )
    violations = registry.validate(RESPONSE_REF, payload.model_dump())
    if violations:
        raise ContractViolation(
            "built error response violates the frozen contracts",
            code="RESPONSE_ENVELOPE_INVALID",
            violations=violations,
        )
    return payload.model_dump()


def contract_violation_response(
    registry: ContractRegistry,
    request_id: str,
    violation: ContractViolation,
) -> dict[str, Any]:
    """Map a :class:`ContractViolation` onto the unified error package."""
    return build_error_response(
        request_id=request_id,
        registry=registry,
        error=build_error(code=violation.code, stage="input"),
    )


def _check_operation_id(request: Request, tool: ToolBinding) -> tuple[str, ...]:
    params_operation_id = request.params.get("operation_id")
    if params_operation_id is None:
        if tool.side_effect:
            return (
                f"$.params.operation_id: side-effectful method {tool.method}"
                " requires an operation id",
            )
        return ()
    if request.operation_id is None:
        return ("$.operation_id: must repeat params.operation_id for side-effectful methods",)
    if params_operation_id != request.operation_id:
        return (
            f"$.operation_id: {request.operation_id!r} does not match params.operation_id"
            f" {params_operation_id!r}",
        )
    return ()


def _split_ref(ref: str) -> tuple[str, str]:
    file, _, definition = ref.partition("#/definitions/")
    return (file.rsplit("/", 1)[-1].removesuffix(".schema.json"), definition)


def _model_validate(model: type[_M], data: Any, *, code: str) -> _M:
    """Parse ``data`` into a typed shape, mapping failures to contract codes."""
    try:
        return model.model_validate(data)
    except PydanticValidationError as error:
        raise ContractViolation(
            f"payload does not match typed shape {model.__name__}",
            code=code,
            violations=_pydantic_messages(error),
        ) from error


def _pydantic_messages(error: PydanticValidationError) -> tuple[str, ...]:
    return tuple(
        f"{'.'.join(str(part) for part in entry['loc']) or '$'}: {entry['msg']}"
        for entry in error.errors()
    )
