"""Domain entities and value objects for the local tennis backend (BE-004).

This layer owns the meaning of tasks and versions, consent, media, evidence,
external sources, technical issues, relative metrics, target skeletons and
resources. It is pure: standard library plus Pydantic only. It never imports
the wire contract bindings, a media library, an analysis adapter, the agent
or bridge side, or the transport; the application/transport batches later
map these domain objects onto the frozen ``contracts/v1`` shapes.

Invariants encoded here come from spec §4.2, §7.1-§7.3, §8.2 and §9.2, and
plan §8.3 and §10.1:

* ``unknown``, ``not_provided``, ``not_visible``, ``not_applicable`` and
  ``insufficient_evidence`` are distinct presence states that never collapse
  into one null value (:class:`Grounded`).
* Evidence and target output are different kinds of objects; a ``target_pose``
  reference can never be cited as evidence.
* Only relative, normalized, ratio and event-order metrics exist. Absolute
  force, speed, power, energy or professional ratings cannot be expressed, and
  probe metadata never becomes a pose or support claim.
* Original media references and original input copies are preserved until the
  user explicitly requests deletion.
* A consent snapshot grants exactly its confirmed purposes for one task
  version, and external purposes expire when recipient or materials change.

Violations raise :class:`DomainViolation` (inside Pydantic models the value
errors surface as ``pydantic.ValidationError``). Messages carry identifiers
only, never raw media or credential material; exception contents must never be
logged, and the transport batch later maps them to stable codes.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

#: Stable code prefix used by every domain violation raised outside Pydantic.
DOMAIN_ERROR_PREFIX = "DOMAIN_"


class DomainViolation(ValueError):
    """A construction or operation broke a domain invariant.

    ``code`` is a stable machine-readable code; the message carries identifiers
    only. Internal exceptions may reference raw material, so their contents are
    never written to logs and are later mapped to stable codes by transport.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code if code.startswith(DOMAIN_ERROR_PREFIX) else f"{DOMAIN_ERROR_PREFIX}{code}"


# ---------------------------------------------------------------------------
# Presence states: missing/unknown/invisible stay distinct (spec §9.2)
# ---------------------------------------------------------------------------

PresenceState = Literal[
    "observed",
    "unknown",
    "not_provided",
    "not_visible",
    "not_applicable",
    "insufficient_evidence",
]
AbsentState = Literal[
    "unknown",
    "not_provided",
    "not_visible",
    "not_applicable",
    "insufficient_evidence",
]

T = TypeVar("T")


class Grounded(BaseModel, Generic[T]):
    """A value carried together with the state that says how it is known.

    ``observed`` is the only state that may hold a value. The other states
    mean different things and must not be interchanged: ``unknown`` (the
    backend could not decide), ``not_provided`` (the user did not answer),
    ``not_visible`` (the object exists but is outside the frame),
    ``not_applicable`` (the question does not apply) and
    ``insufficient_evidence`` (visible but too weak to conclude).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: PresenceState
    value: T | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _check_state_value_pairing(self) -> Grounded[T]:
        if self.state == "observed" and self.value is None:
            raise ValueError("an observed value must carry a value")
        if self.state != "observed" and self.value is not None:
            raise ValueError(f"state {self.state} must not carry a value")
        return self

    @property
    def is_observed(self) -> bool:
        """Whether a real value is available (never true for absent states)."""
        return self.state == "observed"

    @classmethod
    def observed(cls, value: T) -> Grounded[T]:
        """Wrap a genuinely observed value."""
        return cls(state="observed", value=value)

    @classmethod
    def absent(cls, state: PresenceState, note: str | None = None) -> Grounded[T]:
        """Wrap a deliberate absence; every absent state stays distinct."""
        if state == "observed":
            raise DomainViolation("PRESENCE_STATE", "absent() requires a non-observed state")
        return cls(state=state, note=note)


# ---------------------------------------------------------------------------
# Primitive shapes
# ---------------------------------------------------------------------------


def _validate_relative_path(value: str) -> str:
    """Reject absolute paths, drive letters and ``..`` traversal segments."""
    normalized = value.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        raise ValueError("must be a relative path inside an authorized root")
    if normalized[1:2] == ":":
        raise ValueError("must not carry a drive letter")
    if any(part == ".." for part in normalized.split("/")):
        raise ValueError("must not traverse outside an authorized root with '..'")
    return value


def _validate_http_url(value: str) -> str:
    """Only public http(s) references are kept as sources."""
    if not value.startswith(("http://", "https://")):
        raise ValueError("must be an http(s) URL")
    return value


def _validate_relative_metric_unit(value: str) -> str:
    """Only relative metric families exist; absolute ones are rejected loudly."""
    if value in RELATIVE_METRIC_UNITS:
        return value
    if value.lower() in _PROHIBITED_METRIC_UNITS_LOWER:
        raise ValueError(
            f"{value!r} is an absolute physical or professional-rating unit; "
            f"only {RELATIVE_METRIC_UNITS} can be expressed"
        )
    raise ValueError(f"unit must be one of {RELATIVE_METRIC_UNITS}")


def _require_unique(values: list[str]) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError("must not contain duplicate entries")
    return values


Id = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"),
]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
VersionTag = Annotated[str, StringConstraints(min_length=1, max_length=32)]
DigestRef = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Sha256Hex = Annotated[
    str, StringConstraints(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
]
MimeType = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[\w.+-]+/[\w.+-]+$")
]
RelativePath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=1024),
    AfterValidator(_validate_relative_path),
]
HttpUrl = Annotated[
    str, StringConstraints(min_length=8, max_length=2048), AfterValidator(_validate_http_url)
]
MetricUnit = Annotated[str, AfterValidator(_validate_relative_metric_unit)]

Seconds = Annotated[float, Field(ge=0)]

#: Relative metric families a prototype may ever report (plan §8.3).
RELATIVE_METRIC_UNITS: tuple[str, ...] = ("relative", "normalized", "ratio", "event_order")

#: Absolute physical or professional-rating units that must never appear.
PROHIBITED_METRIC_UNITS: tuple[str, ...] = (
    "N",
    "newton",
    "kgf",
    "force",
    "joule",
    "energy",
    "watt",
    "power",
    "m/s",
    "km/h",
    "mph",
    "speed",
    "velocity",
    "rad/s",
    "rpm",
    "ntrp",
    "atp",
    "wta",
    "ranking",
    "rating",
    "professional_level",
)
_PROHIBITED_METRIC_UNITS_LOWER = frozenset(unit.lower() for unit in PROHIBITED_METRIC_UNITS)

# ---------------------------------------------------------------------------
# Vocabularies (domain-owned; the application layer maps them to contracts)
# ---------------------------------------------------------------------------

Route = Literal["question_answer", "tactics", "technical_diagnosis", "style_optimization"]
RuntimeRequirement = Literal["required", "not_required", "uncertain"]
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
AgeBand = Literal["adult", "youth"]
Handedness = Literal["left", "right", "ambidextrous"]
TargetStroke = Literal[
    "forehand",
    "single_backhand",
    "double_backhand",
    "serve",
    "forehand_volley",
    "backhand_volley",
    "footwork",
]
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
ExternalPurpose = Literal["share", "email"]
ObservationKind = Literal["measured", "simulated"]
TriState = Literal["yes", "no", "unknown"]
ContainerKind = Literal["mp4", "mov", "unknown"]
RotationKind = Literal["0", "90", "180", "270", "unknown"]
PanoramaKind = Literal["flat", "native", "unknown"]
MediaRole = Literal["original", "standardized"]
VisibleObject = Literal["body", "feet", "ball", "racket"]
EvidenceKind = Literal["video_frame", "video_clip", "trajectory", "user_statement", "quality_frame"]
VideoEvidenceKind = Literal["video_frame", "video_clip", "trajectory", "quality_frame"]
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
TargetResourceKind = Literal["target_pose"]
ResourceLifecycle = Literal["retained", "deleted", "delete_failed", "outside_control", "not_found"]
Sufficiency = Literal["sufficient", "partial", "insufficient"]

#: Evidence kinds that must point back to an original video timeline.
_VIDEO_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {"video_frame", "video_clip", "trajectory", "quality_frame"}
)
#: Purposes whose confirmation breaks when recipient or materials change.
_EXTERNAL_PURPOSES: frozenset[str] = frozenset({"share", "email"})


class _Entity(BaseModel):
    """Base entity: unknown fields are rejected, instances stay immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Task, version and declared user input
# ---------------------------------------------------------------------------


class UserInput(_Entity):
    """Declared user context (spec §9.2 ``用户输入``).

    Nothing here is measured and nothing is a rating: NTRP self-assessment has
    no field at all. Missing answers keep their own absence state instead of a
    null (EX-002).
    """

    question: Grounded[str]
    age_band: Grounded[AgeBand]
    handedness: Grounded[Handedness]
    target_stroke: Grounded[TargetStroke]
    goal: Grounded[str]
    exercise_background: Grounded[str]
    safety: Grounded[SafetyFlag]

    @model_validator(mode="after")
    def _check_declared_values(self) -> UserInput:
        if self.question.is_observed and not self.question.value:
            raise ValueError("an empty question does not create a task")
        return self

    def missing_inputs(self) -> tuple[str, ...]:
        """Field names that are not observed, each keeping its own state."""
        fields = (
            "question",
            "age_band",
            "handedness",
            "target_stroke",
            "goal",
            "exercise_background",
            "safety",
        )
        return tuple(name for name in fields if not getattr(self, name).is_observed)


class TaskVersion(_Entity):
    """One frozen task version: fixed input, contract and configuration.

    A version has exactly one primary route and at most two auxiliary routes
    that differ from it (FR-010). Frozen instances never change inputs; a new
    round becomes a new version.
    """

    task_id: Id
    version: int = Field(ge=1)
    primary_route: Route
    auxiliary_routes: Annotated[list[Route], Field(max_length=2), AfterValidator(_require_unique)]
    runtime_requirement: RuntimeRequirement
    contract_version: VersionTag
    config_version: VersionTag
    media_id: Id | None = None
    consent_id: Id | None = None
    subject_id: Id | None = None
    input_digest: DigestRef | None = None
    simulation: bool

    @model_validator(mode="after")
    def _check_routes_and_media(self) -> TaskVersion:
        if self.primary_route in self.auxiliary_routes:
            raise ValueError("the primary route must not repeat as an auxiliary route")
        if self.runtime_requirement == "required" and self.media_id is None:
            raise ValueError("a task that requires local capability must reference media")
        return self


def _require_unique_versions(versions: list[TaskVersion]) -> list[TaskVersion]:
    numbers = [version.version for version in versions]
    if len(set(numbers)) != len(numbers):
        raise ValueError("version numbers must be unique inside a task")
    return versions


class Task(_Entity):
    """A task with its immutable versions and its current state (plan §10.1).

    The state vocabulary is defined here; state transitions and terminal-state
    protection live in the states batch and are intentionally absent here.
    """

    task_id: Id
    versions: Annotated[
        list[TaskVersion],
        Field(min_length=1, max_length=200),
        AfterValidator(_require_unique_versions),
    ]
    state: TaskState
    stage: TaskStage
    simulation: bool

    @model_validator(mode="after")
    def _check_versions_belong_to_task(self) -> Task:
        foreign = [version.version for version in self.versions if version.task_id != self.task_id]
        if foreign:
            raise ValueError(f"versions {foreign} belong to another task_id")
        return self

    @property
    def current_version(self) -> TaskVersion:
        """The highest submitted version of this task."""
        return max(self.versions, key=lambda version: version.version)


# ---------------------------------------------------------------------------
# Consent semantics
# ---------------------------------------------------------------------------


class ConsentSnapshot(_Entity):
    """One consent snapshot bound to one task version (spec §9.1-§9.2).

    One purpose never extends to another (FR-006), ``improvement`` and
    ``training_data`` are never granted by default, external purposes carry the
    recipient and materials they were confirmed for (FR-005), and a youth
    snapshot is only valid with the simulated guardian confirmation (EX-010).
    """

    consent_id: Id
    task_id: Id
    version: int = Field(ge=1)
    purposes: Annotated[
        list[ConsentPurpose], Field(min_length=1, max_length=9), AfterValidator(_require_unique)
    ]
    text_version: Id
    confirmed: bool
    guardian_required: bool
    guardian_simulated: bool
    confirmed_at: datetime | None = None
    recipient_digest: DigestRef | None = None
    materials_digest: DigestRef | None = None

    @model_validator(mode="after")
    def _check_consent_scope(self) -> ConsentSnapshot:
        if self.confirmed and self.confirmed_at is None:
            raise ValueError("a confirmed consent needs its confirmation time")
        if not self.confirmed and self.confirmed_at is not None:
            raise ValueError("an unconfirmed consent must not carry a confirmation time")
        if self.confirmed_at is not None and self.confirmed_at.utcoffset() is None:
            raise ValueError("confirmed_at must be timezone aware")
        if self.guardian_required and self.confirmed and not self.guardian_simulated:
            raise ValueError("a youth confirmation requires the simulated guardian confirmation")
        if not self.guardian_required and self.guardian_simulated:
            raise ValueError("a guardian confirmation requires a youth-sensitive scope")
        external = [purpose for purpose in self.purposes if purpose in _EXTERNAL_PURPOSES]
        if external and (self.recipient_digest is None or self.materials_digest is None):
            raise ValueError(f"purposes {external} need the confirmed recipient and materials")
        return self

    def covers(
        self,
        purpose: ConsentPurpose,
        *,
        recipient_digest: DigestRef | None = None,
        materials_digest: DigestRef | None = None,
    ) -> bool:
        """Whether this snapshot grants exactly ``purpose`` right now.

        Purposes match exactly, so a granted purpose never extends to another.
        External purposes additionally require the current recipient and
        materials digests to be re-presented and unchanged; callers that do not
        re-present them simply get ``False`` instead of a stale grant.
        """
        if not self.confirmed:
            return False
        if self.guardian_required and not self.guardian_simulated:
            return False
        if purpose not in self.purposes:
            return False
        if purpose in _EXTERNAL_PURPOSES:
            return (
                recipient_digest is not None
                and materials_digest is not None
                and recipient_digest == self.recipient_digest
                and materials_digest == self.materials_digest
            )
        return True


# ---------------------------------------------------------------------------
# Media, time mapping and unobservable intervals
# ---------------------------------------------------------------------------


class MediaProbe(_Entity):
    """Probe metadata (spec §9.2 ``视频``).

    These fields may be genuinely measured, but they stay metadata forever:
    nothing in the domain derives a pose, a support or a quality verdict from a
    probe alone.
    """

    container: ContainerKind
    codec: ShortText
    duration_s: Seconds
    fps: Seconds
    width: int = Field(ge=0)
    height: int = Field(ge=0)
    rotation: RotationKind
    slow_motion: TriState
    hdr: TriState
    device_source: ShortText
    panorama: PanoramaKind
    observation_kind: ObservationKind


class TimePoint(_Entity):
    """One mapping point from normalized time back to original video time."""

    normalized_s: Seconds
    original_s: Seconds


def _require_monotonic(points: list[TimePoint]) -> list[TimePoint]:
    for previous, current in zip(points, points[1:], strict=False):
        if current.normalized_s <= previous.normalized_s:
            raise ValueError("normalized_s must strictly increase")
        if current.original_s < previous.original_s:
            raise ValueError("original_s must not move backwards")
    return points


class MediaAsset(_Entity):
    """A media asset: the preserved original or one standardized derivative.

    ``derived_from`` separates the two roles, so a derivative can never be
    mistaken for the user's original (plan §8.3 ``preserve originals``). A
    valid time mapping must be non-empty; a failed mapping stays marked invalid
    and blocks evidence publication (EX-043).
    """

    media_id: Id
    role: MediaRole
    original_ref: RelativePath
    derived_from: Id | None = None
    probe: MediaProbe
    time_mapping: Annotated[
        list[TimePoint], Field(max_length=2000), AfterValidator(_require_monotonic)
    ]
    time_mapping_valid: bool
    simulation: bool

    @model_validator(mode="after")
    def _check_roles_and_mapping(self) -> MediaAsset:
        if self.role == "original" and self.derived_from is not None:
            raise ValueError("an original asset must not be derived from another asset")
        if self.role == "standardized":
            if self.derived_from is None:
                raise ValueError("a standardized asset must name the asset it derives from")
            if self.derived_from == self.media_id:
                raise ValueError("a standardized asset must not derive from itself")
        if self.time_mapping_valid and not self.time_mapping:
            raise ValueError("a valid time mapping must contain at least one point")
        return self


def assert_original_preserved(origin: MediaAsset, derivative: MediaAsset) -> None:
    """Check that a derivative still points at the untouched original."""
    if derivative.original_ref != origin.original_ref:
        raise DomainViolation(
            "ORIGINAL_PRESERVED",
            f"derivative {derivative.media_id} must keep original_ref of {origin.media_id}",
        )
    if derivative.derived_from != origin.media_id:
        raise DomainViolation(
            "ORIGINAL_PRESERVED",
            f"derivative {derivative.media_id} must derive from {origin.media_id}",
        )
    if derivative.role != "standardized" or origin.role != "original":
        raise DomainViolation(
            "ORIGINAL_PRESERVED",
            "an original and a standardized derivative are required",
        )


class TimeInterval(_Entity):
    """A half-open interval on one timeline, start before end."""

    start_s: Seconds
    end_s: Seconds

    @model_validator(mode="after")
    def _check_ordered(self) -> TimeInterval:
        if self.end_s < self.start_s:
            raise ValueError("end_s must not be before start_s")
        return self

    def overlaps(self, other: TimeInterval) -> bool:
        """Whether the two intervals share at least one instant."""
        return self.start_s < other.end_s and other.start_s < self.end_s


class UnobservableInterval(_Entity):
    """An interval where nothing can be concluded, with the reason why.

    Occlusion, track jumps and crosstracking are recorded here instead of being
    silently dropped, so downstream consumers can tell "excluded" from "absent"
    (FR-031).
    """

    asset_id: Id
    interval: TimeInterval
    reason: ShortText


# ---------------------------------------------------------------------------
# Evidence and its resources (never target output)
# ---------------------------------------------------------------------------


class ResourceRef(_Entity):
    """A controlled local reference; raw media never leaves the backend."""

    resource_id: Id
    path: RelativePath
    mime: MimeType
    sha256: Sha256Hex
    size_bytes: int = Field(ge=0)
    kind: ResourceKind
    simulation: bool


class EvidenceResourceRef(ResourceRef):
    """A resource cited by evidence; ``target_pose`` is excluded here."""

    kind: EvidenceResourceKind


class TargetResourceRef(ResourceRef):
    """A target skeleton resource; only ``target_pose`` is allowed here."""

    kind: TargetResourceKind


def assert_distinct_from_evidence(resource: ResourceRef) -> None:
    """Refuse target output being used where video evidence is required."""
    if isinstance(resource, TargetResourceRef) or resource.kind == "target_pose":
        raise DomainViolation(
            "TARGET_NOT_EVIDENCE",
            f"resource {resource.resource_id} is target output and can never be evidence",
        )


class Evidence(_Entity):
    """One observation tied back to an original video time or a user statement.

    Video-derived kinds must name their asset and interval (spec §4.2 ``证据``);
    a user statement carries neither, because it is not on the video timeline.
    The resource, when present, can only be an evidence resource.
    """

    evidence_id: Id
    kind: EvidenceKind
    asset_id: Id | None = None
    subject_id: Id
    interval: TimeInterval | None = None
    visible_objects: Annotated[
        list[VisibleObject], Field(max_length=4), AfterValidator(_require_unique)
    ]
    resource: EvidenceResourceRef | None = None
    simulation: bool

    @model_validator(mode="after")
    def _check_timeline_binding(self) -> Evidence:
        if self.kind in _VIDEO_EVIDENCE_KINDS:
            if self.asset_id is None or self.interval is None:
                raise ValueError(f"{self.kind} evidence must reference an asset and an interval")
        elif self.asset_id is not None or self.interval is not None:
            raise ValueError("a user statement is not bound to the video timeline")
        return self


def _require_unique_evidence(evidence: list[Evidence]) -> list[Evidence]:
    ids = [item.evidence_id for item in evidence]
    if len(set(ids)) != len(ids):
        raise ValueError("evidence ids must be unique")
    return evidence


class EvidenceSet(_Entity):
    """The evidence of one task version plus its excluded intervals."""

    evidence: Annotated[
        list[Evidence], Field(max_length=200), AfterValidator(_require_unique_evidence)
    ]
    unobservable: Annotated[list[UnobservableInterval], Field(max_length=200)] = []

    @model_validator(mode="after")
    def _check_unobservable_excluded(self) -> EvidenceSet:
        for item in self.evidence:
            if item.asset_id is None or item.interval is None:
                continue
            for gap in self.unobservable:
                if gap.asset_id != item.asset_id:
                    continue
                if item.interval.overlaps(gap.interval):
                    raise ValueError(
                        f"evidence {item.evidence_id} overlaps unobservable interval of "
                        f"asset {item.asset_id} ({gap.reason})"
                    )
        return self

    def ids(self) -> tuple[Id, ...]:
        """All evidence ids in this set."""
        return tuple(item.evidence_id for item in self.evidence)

    def supports(self, evidence_ids: list[Id]) -> bool:
        """Whether every cited id exists in this set."""
        known = set(self.ids())
        return bool(evidence_ids) and all(item in known for item in evidence_ids)


# ---------------------------------------------------------------------------
# External sources: knowledge references, never video evidence
# ---------------------------------------------------------------------------


class Source(_Entity):
    """A public knowledge reference behind an external conclusion (spec §4.2).

    A source records what it supports, its limits and its access date, so a
    reader can tell an external reference from video evidence; it never
    substitutes one (plan §12 step 3).
    """

    source_id: Id
    title: ShortText
    author_or_org: ShortText | None = None
    url: HttpUrl
    accessed_on: date
    supports: Annotated[
        list[ShortText], Field(min_length=1, max_length=50), AfterValidator(_require_unique)
    ]
    limitations: Annotated[list[LongText], Field(min_length=1, max_length=50)]


# ---------------------------------------------------------------------------
# Relative metrics only
# ---------------------------------------------------------------------------


class Metric(_Entity):
    """A relative metric with definition, reference, evidence and limits.

    The unit vocabulary makes absolute force, speed, power, energy and any
    professional rating inexpressible (plan §8.3, FR-034, FR-035). An observed
    value must cite evidence; a non-observed value keeps its own presence state
    instead of a placeholder number.
    """

    metric_id: Id
    definition: LongText
    reference: LongText
    unit: MetricUnit
    value: Grounded[float | str]
    evidence_ids: Annotated[list[Id], Field(max_length=200), AfterValidator(_require_unique)]
    limitations: Annotated[list[LongText], Field(min_length=1, max_length=50)]
    simulation: bool

    @model_validator(mode="after")
    def _check_value_grounded_in_evidence(self) -> Metric:
        if self.value.is_observed and not self.evidence_ids:
            raise ValueError("an observed metric value must cite at least one evidence id")
        return self


# ---------------------------------------------------------------------------
# Target skeleton: a concept reference, never evidence, never a pose claim
# ---------------------------------------------------------------------------


class TargetSkeleton(_Entity):
    """A generic target skeleton or comparison sketch (spec US-032, FR-045).

    It stays marked as a concept reference for its whole life, is always a
    simulated prototype artifact, and its resource kind keeps it structurally
    separate from evidence.
    """

    target_id: Id
    task_id: Id
    version: int = Field(ge=1)
    features: Annotated[list[LongText], Field(min_length=1, max_length=6)]
    limitations: Annotated[list[LongText], Field(min_length=1, max_length=50)]
    resource: TargetResourceRef
    conceptual_reference: Literal[True]
    simulation: Literal[True]


# ---------------------------------------------------------------------------
# Resources and their lifecycle
# ---------------------------------------------------------------------------


class Resource(ResourceRef):
    """A tracked resource with its deletion lifecycle (plan §10.1 ``Resource``).

    Original input copies are preserved until the user explicitly requests
    deletion; everything else moves between retained, deleted, delete_failed,
    outside_control and not_found without reusing one "gone" value.
    """

    lifecycle: ResourceLifecycle = "retained"

    def with_lifecycle(
        self, lifecycle: ResourceLifecycle, *, user_requested: bool = False
    ) -> Resource:
        """Return the resource moved to ``lifecycle``.

        ``user_requested`` must be true to delete an original input copy, and
        a deleted resource never changes state again.
        """
        if self.lifecycle == "deleted":
            raise DomainViolation(
                "ALREADY_DELETED",
                f"resource {self.resource_id} is already deleted",
            )
        if lifecycle == "deleted" and self.kind == "input_copy" and not user_requested:
            raise DomainViolation(
                "ORIGINAL_PRESERVED",
                f"input copy {self.resource_id} is deleted only on an explicit user request",
            )
        if lifecycle == "outside_control" and self.kind == "input_copy":
            raise DomainViolation(
                "ORIGINAL_PRESERVED",
                f"input copy {self.resource_id} stays under local control",
            )
        return self.model_copy(update={"lifecycle": lifecycle})


# ---------------------------------------------------------------------------
# Technical issues and their publication bounds
# ---------------------------------------------------------------------------


class Issue(_Entity):
    """One technical issue candidate (spec §9.2 ``结论``, FR-033).

    ``sufficiency``, limitations and alternative explanations are carried
    explicitly, so an issue without enough support simply is not publishable
    instead of being silently upgraded to a conclusion.
    """

    issue_id: Id
    task_id: Id
    version: int = Field(ge=1)
    statement: LongText
    sufficiency: Sufficiency
    impact: LongText
    limitations: Annotated[list[LongText], Field(min_length=1, max_length=50)]
    alternative_explanations: Annotated[list[LongText], Field(max_length=20)] = []
    evidence_ids: Annotated[list[Id], Field(max_length=200), AfterValidator(_require_unique)] = []
    safety_blocked: bool
    modifiable: bool
    depends_on: Annotated[list[Id], Field(max_length=20), AfterValidator(_require_unique)] = []
    priority: int = Field(ge=0)

    @property
    def publishable(self) -> bool:
        """Whether this issue may appear in a report.

        Safety-blocked issues, issues without enough support and issues without
        any evidence are held back (FR-036, FR-037, EX-067, EX-080).
        """
        return (
            self.sufficiency != "insufficient"
            and bool(self.evidence_ids)
            and not self.safety_blocked
        )


def _require_unique_issues(issues: list[Issue]) -> list[Issue]:
    ids = [issue.issue_id for issue in issues]
    if len(set(ids)) != len(ids):
        raise ValueError("issue ids must be unique")
    return issues


#: A report shows at most three priority issues (FR-036, RQ-001).
MAX_PUBLISHED_ISSUES = 3


class IssueSet(_Entity):
    """The issue candidates of one task version."""

    issues: Annotated[list[Issue], Field(max_length=200), AfterValidator(_require_unique_issues)]

    def published(self) -> tuple[Issue, ...]:
        """Publishable issues, safety first, at most three of them.

        Ordering uses safety and the recorded priority; impact, modifiability
        and dependencies stay on each issue for the teaching side to weigh.
        """
        publishable = sorted(
            (issue for issue in self.issues if issue.publishable),
            key=lambda issue: (issue.safety_blocked, issue.priority, issue.issue_id),
        )
        return tuple(publishable[:MAX_PUBLISHED_ISSUES])

    def assert_publishable_against(self, evidence: EvidenceSet) -> None:
        """Block publication when a publishable issue cites missing evidence."""
        for issue in self.published():
            if not evidence.supports(list(issue.evidence_ids)):
                raise DomainViolation(
                    "ISSUE_WITHOUT_EVIDENCE",
                    f"issue {issue.issue_id} cites evidence outside the task version",
                )


__all__ = [
    "DOMAIN_ERROR_PREFIX",
    "MAX_PUBLISHED_ISSUES",
    "PROHIBITED_METRIC_UNITS",
    "RELATIVE_METRIC_UNITS",
    "AgeBand",
    "ConsentPurpose",
    "ConsentSnapshot",
    "ContainerKind",
    "DomainViolation",
    "Evidence",
    "EvidenceKind",
    "EvidenceResourceKind",
    "EvidenceResourceRef",
    "EvidenceSet",
    "ExternalPurpose",
    "Grounded",
    "Handedness",
    "HttpUrl",
    "Id",
    "Issue",
    "IssueSet",
    "LongText",
    "MediaAsset",
    "MediaProbe",
    "MediaRole",
    "Metric",
    "MetricUnit",
    "MimeType",
    "ObservationKind",
    "PanoramaKind",
    "PresenceState",
    "Resource",
    "ResourceKind",
    "ResourceLifecycle",
    "ResourceRef",
    "Route",
    "RotationKind",
    "SafetyFlag",
    "Seconds",
    "ShortText",
    "Sha256Hex",
    "Source",
    "Sufficiency",
    "TargetResourceKind",
    "TargetResourceRef",
    "TargetSkeleton",
    "TargetStroke",
    "Task",
    "TaskStage",
    "TaskState",
    "TaskVersion",
    "TimeInterval",
    "TimePoint",
    "TriState",
    "UnobservableInterval",
    "UserInput",
    "VersionTag",
    "VisibleObject",
    "assert_distinct_from_evidence",
    "assert_original_preserved",
]
