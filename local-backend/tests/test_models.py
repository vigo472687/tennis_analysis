"""Domain model tests for tasks, consent, media, evidence and metrics (BE-004).

Each case pins one domain invariant from spec §4.2, §7, §9.2 and plan §8.3 and
§10.1: distinct presence states, evidence/target separation, relative-only
metrics, preserved originals, consent scoping, unobservable intervals and the
three-issue publication bound. All samples are synthetic identifiers; no real
media, network or platform binding is touched.
"""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tennis_backend.domain.models import (
    MAX_PUBLISHED_ISSUES,
    PROHIBITED_METRIC_UNITS,
    DomainViolation,
    Evidence,
    EvidenceSet,
    Grounded,
    Issue,
    IssueSet,
    MediaAsset,
    Metric,
    Resource,
    Source,
    TargetResourceRef,
    TargetSkeleton,
    Task,
    TaskVersion,
    TimeInterval,
    UnobservableInterval,
    UserInput,
    assert_distinct_from_evidence,
    assert_original_preserved,
)

DOMAIN_MODELS = (
    Path(__file__).resolve().parents[1] / "src" / "tennis_backend" / "domain" / "models.py"
)


def grounded(value: Any = None, **overrides: Any) -> Grounded[Any]:
    """An observed value unless the test asks for a deliberate absence."""
    if overrides:
        return Grounded[Any](**overrides)
    return Grounded[Any].observed(value)


def absent(state: str, note: str | None = None) -> Grounded[Any]:
    return Grounded[Any].absent(state, note)  # type: ignore[arg-type]


def user_input(**overrides: Any) -> UserInput:
    """A fully declared adult forehand input, with absent states on request."""
    fields: dict[str, Any] = {
        "question": grounded("why does my forehand break under pressure?"),
        "age_band": grounded("adult"),
        "handedness": grounded("right"),
        "target_stroke": grounded("forehand"),
        "goal": grounded("stable contact point"),
        "exercise_background": grounded("two years of recreational play"),
        "safety": grounded("clear"),
    }
    fields.update(overrides)
    return UserInput(**fields)


def task_version(**overrides: Any) -> TaskVersion:
    fields: dict[str, Any] = {
        "task_id": "task-1",
        "version": 1,
        "primary_route": "technical_diagnosis",
        "auxiliary_routes": ["question_answer"],
        "runtime_requirement": "required",
        "contract_version": "2.0.0",
        "config_version": "fixture-config-1",
        "media_id": "media-1",
        "consent_id": "consent-1",
        "simulation": True,
    }
    fields.update(overrides)
    return TaskVersion(**fields)


def task(**overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "task_id": "task-1",
        "versions": [task_version()],
        "state": "analyzing",
        "stage": "analysis",
        "simulation": True,
    }
    fields.update(overrides)
    return Task(**fields)


def consent(**overrides: Any) -> Any:
    from tennis_backend.domain.models import ConsentSnapshot

    fields: dict[str, Any] = {
        "consent_id": "consent-1",
        "task_id": "task-1",
        "version": 1,
        "purposes": ["analysis"],
        "text_version": "consent-text-1",
        "confirmed": True,
        "guardian_required": False,
        "guardian_simulated": False,
        "confirmed_at": dt.datetime(2026, 11, 6, tzinfo=dt.UTC),
    }
    fields.update(overrides)
    return ConsentSnapshot(**fields)


def probe(**overrides: Any) -> Any:
    from tennis_backend.domain.models import MediaProbe

    fields: dict[str, Any] = {
        "container": "mp4",
        "codec": "avc1",
        "duration_s": 12.5,
        "fps": 30.0,
        "width": 1920,
        "height": 1080,
        "rotation": "0",
        "slow_motion": "no",
        "hdr": "no",
        "device_source": "phone-handheld",
        "panorama": "flat",
        "observation_kind": "measured",
    }
    fields.update(overrides)
    return MediaProbe(**fields)


def media_asset(**overrides: Any) -> MediaAsset:
    from tennis_backend.domain.models import TimePoint

    fields: dict[str, Any] = {
        "media_id": "media-1",
        "role": "original",
        "original_ref": "projects/demo/originals/clip.mp4",
        "probe": probe(),
        "time_mapping": [TimePoint(normalized_s=0.0, original_s=0.0)],
        "time_mapping_valid": True,
        "simulation": True,
    }
    fields.update(overrides)
    return MediaAsset(**fields)


def evidence_resource(**overrides: Any) -> Any:
    from tennis_backend.domain.models import EvidenceResourceRef

    fields: dict[str, Any] = {
        "resource_id": "res-frame-1",
        "path": "projects/demo/evidence/frame-0001.jpg",
        "mime": "image/jpeg",
        "sha256": "a" * 64,
        "size_bytes": 2048,
        "kind": "evidence_frame",
        "simulation": True,
    }
    fields.update(overrides)
    return EvidenceResourceRef(**fields)


def target_resource(**overrides: Any) -> TargetResourceRef:
    fields: dict[str, Any] = {
        "resource_id": "res-target-1",
        "path": "projects/demo/targets/skeleton.svg",
        "mime": "image/svg+xml",
        "sha256": "b" * 64,
        "size_bytes": 4096,
        "kind": "target_pose",
        "simulation": True,
    }
    fields.update(overrides)
    return TargetResourceRef(**fields)


def evidence(**overrides: Any) -> Evidence:
    fields: dict[str, Any] = {
        "evidence_id": "ev-1",
        "kind": "video_frame",
        "asset_id": "media-1",
        "subject_id": "subject-1",
        "interval": TimeInterval(start_s=1.0, end_s=1.4),
        "visible_objects": ["body", "feet", "racket"],
        "resource": evidence_resource(),
        "simulation": True,
    }
    fields.update(overrides)
    return Evidence(**fields)


def evidence_set(**overrides: Any) -> EvidenceSet:
    fields: dict[str, Any] = {
        "evidence": [evidence()],
        "unobservable": [],
    }
    fields.update(overrides)
    return EvidenceSet(**fields)


def metric(**overrides: Any) -> Metric:
    fields: dict[str, Any] = {
        "metric_id": "metric-1",
        "definition": "contact height divided by body height",
        "reference": "own body scale at the preparation phase",
        "unit": "normalized",
        "value": grounded(0.62),
        "evidence_ids": ["ev-1"],
        "limitations": ["single fixture sample", "no calibration against a measured model"],
        "simulation": True,
    }
    fields.update(overrides)
    return Metric(**fields)


def issue(**overrides: Any) -> Issue:
    fields: dict[str, Any] = {
        "issue_id": "issue-1",
        "task_id": "task-1",
        "version": 1,
        "statement": "contact point drifts behind the front foot",
        "sufficiency": "sufficient",
        "impact": "late contact reduces directional control",
        "limitations": ["fixture sample only"],
        "alternative_explanations": ["grip change during the rally"],
        "evidence_ids": ["ev-1"],
        "safety_blocked": False,
        "modifiable": True,
        "depends_on": [],
        "priority": 1,
    }
    fields.update(overrides)
    return Issue(**fields)


def source(**overrides: Any) -> Source:
    fields: dict[str, Any] = {
        "source_id": "src-1",
        "title": "Public coaching reference on unit turn timing",
        "author_or_org": "Open coaching collective",
        "url": "https://example.org/unit-turn",
        "accessed_on": dt.date(2026, 11, 6),
        "supports": ["unit turn precedes the forward swing"],
        "limitations": ["not specific to this recording"],
    }
    fields.update(overrides)
    return Source(**fields)


# ---------------------------------------------------------------------------
# Presence states stay distinct
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "value"),
    [
        ("unknown", "late"),
        ("not_provided", "late"),
        ("not_visible", "late"),
        ("not_applicable", "late"),
        ("insufficient_evidence", "late"),
    ],
)
def test_absent_states_never_carry_a_value(state: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Grounded[str](state=state, value=value)  # type: ignore[arg-type]


def test_observed_state_requires_a_value() -> None:
    with pytest.raises(ValidationError):
        Grounded[str](state="observed")


def test_absent_states_are_distinct_and_none_of_them_is_observed() -> None:
    states = ["unknown", "not_provided", "not_visible", "not_applicable", "insufficient_evidence"]
    wrapped = [absent(state) for state in states]
    assert len({absent.state for absent in wrapped}) == len(states)
    for wrapped_state, other in zip(wrapped, wrapped[1:], strict=False):
        assert wrapped_state != other
    assert all(not item.is_observed for item in wrapped)
    assert grounded("x").is_observed is True


def test_absent_rejects_the_observed_state() -> None:
    with pytest.raises(DomainViolation):
        Grounded[str].absent("observed")  # type: ignore[arg-type]


def test_user_input_lists_each_gap_with_its_own_state() -> None:
    partial = user_input(
        age_band=absent("not_provided"),
        safety=absent("unknown", "questionnaire not finished"),
    )
    assert partial.missing_inputs() == ("age_band", "safety")
    assert partial.age_band.state == "not_provided"
    assert partial.safety.state == "unknown"


def test_empty_question_does_not_create_a_task() -> None:
    with pytest.raises(ValidationError):
        user_input(question=grounded(""))


# ---------------------------------------------------------------------------
# Task, version and route invariants
# ---------------------------------------------------------------------------


def test_primary_route_must_not_repeat_as_auxiliary() -> None:
    with pytest.raises(ValidationError):
        task_version(auxiliary_routes=["technical_diagnosis"])


@pytest.mark.parametrize(
    "routes", [["question_answer", "tactics", "style_optimization"], ["tactics", "tactics"]]
)
def test_auxiliary_routes_are_bounded_and_unique(routes: list[str]) -> None:
    with pytest.raises(ValidationError):
        task_version(auxiliary_routes=routes)  # type: ignore[arg-type]


def test_required_runtime_needs_media() -> None:
    with pytest.raises(ValidationError):
        task_version(media_id=None)


def test_task_holds_unique_versions_of_itself_only() -> None:
    other = task_version(task_id="task-2", version=2)
    with pytest.raises(ValidationError):
        task(versions=[task_version(), other])
    with pytest.raises(ValidationError):
        task(versions=[task_version(), task_version(version=1)])


def test_current_version_is_the_highest_submitted() -> None:
    renewed = task(
        versions=[
            task_version(),
            task_version(version=2, primary_route="tactics", auxiliary_routes=[]),
        ]
    )
    assert renewed.current_version.version == 2
    assert renewed.current_version.primary_route == "tactics"


# ---------------------------------------------------------------------------
# Consent semantics
# ---------------------------------------------------------------------------


def test_unconfirmed_consent_grants_nothing() -> None:
    snapshot = consent(confirmed=False, confirmed_at=None)
    assert snapshot.covers("analysis") is False


def test_consent_grants_exactly_its_purposes() -> None:
    snapshot = consent(purposes=["analysis", "save"])
    assert snapshot.covers("analysis") is True
    assert snapshot.covers("save") is True
    for never in ("network", "share", "email", "diagnostics", "improvement", "training_data"):
        assert snapshot.covers(never) is False


def test_external_purposes_need_recipient_and_materials() -> None:
    with pytest.raises(ValidationError):
        consent(purposes=["share"])
    with pytest.raises(ValidationError):
        consent(purposes=["email"], recipient_digest="recipient-a")
    snapshot = consent(
        purposes=["share"],
        recipient_digest="recipient-a",
        materials_digest="materials-a",
    )
    assert snapshot.covers("share", recipient_digest="recipient-a", materials_digest="materials-a")


def test_external_consent_expires_when_recipient_or_materials_change() -> None:
    snapshot = consent(
        purposes=["share"],
        recipient_digest="recipient-a",
        materials_digest="materials-a",
    )
    assert (
        snapshot.covers("share", recipient_digest="recipient-b", materials_digest="materials-a")
        is False
    )
    assert (
        snapshot.covers("share", recipient_digest="recipient-a", materials_digest="materials-b")
        is False
    )
    assert snapshot.covers("share") is False


def test_youth_consent_requires_the_simulated_guardian() -> None:
    with pytest.raises(ValidationError):
        consent(guardian_required=True, guardian_simulated=False)
    with pytest.raises(ValidationError):
        consent(guardian_required=False, guardian_simulated=True)


def test_guardian_missing_blocks_the_grant() -> None:
    snapshot = consent(guardian_required=True, guardian_simulated=True)
    assert snapshot.covers("analysis") is True
    withdrawn = snapshot.model_copy(update={"guardian_simulated": False})
    assert withdrawn.covers("analysis") is False


def test_confirmation_time_is_timezone_aware_and_paired() -> None:
    with pytest.raises(ValidationError):
        consent(confirmed_at=dt.datetime(2026, 11, 6))
    with pytest.raises(ValidationError):
        consent(confirmed=False, confirmed_at=dt.datetime(2026, 11, 6, tzinfo=dt.UTC))


# ---------------------------------------------------------------------------
# Media, time mapping and preserved originals
# ---------------------------------------------------------------------------


def test_probe_metadata_stays_metadata() -> None:
    measured = probe()
    assert measured.observation_kind == "measured"
    simulated = probe(observation_kind="simulated")
    assert simulated.observation_kind == "simulated"
    assert not hasattr(measured, "pose")
    assert not hasattr(measured, "support")


def test_standardized_media_must_derive_from_another_asset() -> None:
    with pytest.raises(ValidationError):
        media_asset(role="standardized", derived_from=None)
    with pytest.raises(ValidationError):
        media_asset(role="standardized", derived_from="media-1")


def test_valid_time_mapping_must_be_non_empty_and_monotonic() -> None:
    from tennis_backend.domain.models import TimePoint

    with pytest.raises(ValidationError):
        media_asset(time_mapping=[])
    with pytest.raises(ValidationError):
        media_asset(
            time_mapping=[
                TimePoint(normalized_s=0.0, original_s=0.0),
                TimePoint(normalized_s=0.0, original_s=0.5),
            ]
        )
    with pytest.raises(ValidationError):
        media_asset(
            time_mapping=[
                TimePoint(normalized_s=0.0, original_s=1.0),
                TimePoint(normalized_s=0.5, original_s=0.5),
            ]
        )


def test_failed_time_mapping_stays_marked_invalid() -> None:
    broken = media_asset(time_mapping=[], time_mapping_valid=False)
    assert broken.time_mapping_valid is False


def test_originals_are_preserved_by_derivatives() -> None:
    origin = media_asset()
    derivative = media_asset(
        media_id="media-2",
        role="standardized",
        derived_from="media-1",
        original_ref="projects/demo/originals/clip.mp4",
    )
    assert_original_preserved(origin, derivative)
    with pytest.raises(DomainViolation):
        assert_original_preserved(
            origin, derivative.model_copy(update={"original_ref": "other/clip.mp4"})
        )
    with pytest.raises(DomainViolation):
        assert_original_preserved(origin, derivative.model_copy(update={"derived_from": "media-9"}))


# ---------------------------------------------------------------------------
# Evidence, target output and their separation
# ---------------------------------------------------------------------------


def test_video_evidence_needs_asset_and_interval() -> None:
    with pytest.raises(ValidationError):
        evidence(asset_id=None)
    with pytest.raises(ValidationError):
        evidence(interval=None)


def test_user_statement_is_not_on_the_video_timeline() -> None:
    statement = evidence(
        evidence_id="ev-say-1",
        kind="user_statement",
        asset_id=None,
        interval=None,
        resource=None,
    )
    assert statement.asset_id is None
    with pytest.raises(ValidationError):
        evidence(kind="user_statement")
    with pytest.raises(ValidationError):
        evidence(
            evidence_id="ev-say-2",
            kind="user_statement",
            asset_id=None,
            interval=TimeInterval(start_s=0.0, end_s=0.2),
        )


def test_ordered_intervals_only() -> None:
    with pytest.raises(ValidationError):
        TimeInterval(start_s=2.0, end_s=1.0)
    assert (
        TimeInterval(start_s=1.0, end_s=1.5).overlaps(TimeInterval(start_s=1.0, end_s=2.0)) is True
    )
    assert (
        TimeInterval(start_s=0.0, end_s=1.0).overlaps(TimeInterval(start_s=1.5, end_s=2.0)) is False
    )
    assert (
        TimeInterval(start_s=1.0, end_s=1.2).overlaps(TimeInterval(start_s=1.2, end_s=2.0)) is False
    )


def test_evidence_resource_can_never_be_target_output() -> None:
    with pytest.raises(ValidationError):
        evidence(resource=target_resource())
    with pytest.raises(DomainViolation):
        assert_distinct_from_evidence(target_resource())
    assert_distinct_from_evidence(evidence_resource())


def test_evidence_must_not_sit_inside_an_unobservable_interval() -> None:
    from tennis_backend.domain.models import UnobservableInterval as Gap

    gaps = [
        Gap(
            asset_id="media-1",
            interval=TimeInterval(start_s=1.2, end_s=1.6),
            reason="racket occluded by the body",
        )
    ]
    with pytest.raises(ValidationError):
        evidence_set(unobservable=gaps)
    outside = evidence_set(
        evidence=[evidence(evidence_id="ev-2", interval=TimeInterval(start_s=2.0, end_s=2.4))],
        unobservable=gaps,
    )
    assert outside.ids() == ("ev-2",)


def test_unobservable_intervals_are_kept_as_their_own_objects() -> None:
    gap = UnobservableInterval(
        asset_id="media-1",
        interval=TimeInterval(start_s=3.0, end_s=3.4),
        reason="track jump",
    )
    assert gap.reason == "track jump"
    assert gap.interval.overlaps(TimeInterval(start_s=3.1, end_s=3.2)) is True


def test_evidence_set_support_checks_cited_ids() -> None:
    known = evidence_set()
    assert known.supports(["ev-1"]) is True
    assert known.supports(["ev-1", "ev-missing"]) is False
    assert known.supports([]) is False


# ---------------------------------------------------------------------------
# Relative metrics only: no force, speed or professional ratings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", ["N", "m/s", "km/h", "watt", "joule", "ntrp", "atp", "rating"])
def test_absolute_and_rating_units_are_inexpressible(unit: str) -> None:
    with pytest.raises(ValidationError):
        metric(unit=unit)


def test_only_relative_metric_families_exist() -> None:
    for unit in ("relative", "normalized", "ratio", "event_order"):
        assert metric(unit=unit).unit == unit
    with pytest.raises(ValidationError):
        metric(unit="meters")


def test_prohibited_unit_families_are_listed_for_reviewers() -> None:
    assert "ntrp" in PROHIBITED_METRIC_UNITS
    assert "force" in PROHIBITED_METRIC_UNITS
    assert "speed" in PROHIBITED_METRIC_UNITS


def test_observed_metric_values_need_evidence() -> None:
    with pytest.raises(ValidationError):
        metric(evidence_ids=[])


def test_unobserved_metric_keeps_its_own_state() -> None:
    unmeasured = metric(value=absent("not_visible", "ball outside the frame"), evidence_ids=[])
    assert unmeasured.value.is_observed is False
    assert unmeasured.value.state == "not_visible"


# ---------------------------------------------------------------------------
# Target skeletons stay concept references
# ---------------------------------------------------------------------------


def test_target_is_always_a_simulated_concept_reference() -> None:
    skeleton = TargetSkeleton(
        target_id="target-1",
        task_id="task-1",
        version=1,
        features=["unit turn before the forward swing"],
        limitations=["generic skeleton, not the recorded player"],
        resource=target_resource(),
        conceptual_reference=True,
        simulation=True,
    )
    assert skeleton.conceptual_reference is True
    assert skeleton.simulation is True
    with pytest.raises(ValidationError):
        TargetSkeleton(
            target_id="target-2",
            task_id="task-1",
            version=1,
            features=["unit turn before the forward swing"],
            limitations=["generic skeleton, not the recorded player"],
            resource=target_resource(),
            conceptual_reference=False,  # type: ignore[arg-type]
            simulation=True,
        )
    with pytest.raises(ValidationError):
        TargetSkeleton(
            target_id="target-3",
            task_id="task-1",
            version=1,
            features=["unit turn before the forward swing"],
            limitations=["generic skeleton, not the recorded player"],
            resource=target_resource(),
            conceptual_reference=True,
            simulation=False,
        )


# ---------------------------------------------------------------------------
# Resources: path safety, preserved input copies, distinct lifecycles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../escape.mp4", "projects/demo/../../escape.mp4", "C:/escape.mp4", ""],
)
def test_resource_paths_stay_inside_authorized_roots(path: str) -> None:
    from tennis_backend.domain.models import ResourceRef

    with pytest.raises(ValidationError):
        ResourceRef(
            resource_id="res-1",
            path=path,
            mime="video/mp4",
            sha256="c" * 64,
            size_bytes=10,
            kind="input_copy",
            simulation=True,
        )


def resource(**overrides: Any) -> Resource:
    fields: dict[str, Any] = {
        "resource_id": "res-in-1",
        "path": "projects/demo/originals/clip.mp4",
        "mime": "video/mp4",
        "sha256": "c" * 64,
        "size_bytes": 10,
        "kind": "input_copy",
        "simulation": True,
        "lifecycle": "retained",
    }
    fields.update(overrides)
    return Resource(**fields)


def test_input_copies_are_deleted_only_on_explicit_user_request() -> None:
    original = resource()
    with pytest.raises(DomainViolation):
        original.with_lifecycle("deleted")
    assert original.with_lifecycle("deleted", user_requested=True).lifecycle == "deleted"


def test_input_copies_never_become_outside_control() -> None:
    with pytest.raises(DomainViolation):
        resource().with_lifecycle("outside_control")


def test_deleted_resources_are_final_and_lifecycles_stay_distinct() -> None:
    deleted = resource().with_lifecycle("deleted", user_requested=True)
    with pytest.raises(DomainViolation):
        deleted.with_lifecycle("retained")
    failed = resource().with_lifecycle("delete_failed")
    external = derived_resource().with_lifecycle("outside_control")
    missing = derived_resource().with_lifecycle("not_found")
    assert len({failed.lifecycle, external.lifecycle, missing.lifecycle, deleted.lifecycle}) == 4


def derived_resource(**overrides: Any) -> Resource:
    fields: dict[str, Any] = {
        "resource_id": "res-frame-2",
        "path": "projects/demo/evidence/frame-0002.jpg",
        "mime": "image/jpeg",
        "sha256": "d" * 64,
        "size_bytes": 2048,
        "kind": "evidence_frame",
        "simulation": True,
        "lifecycle": "retained",
    }
    fields.update(overrides)
    return Resource(**fields)


def test_derived_resources_move_between_lifecycles_without_user_request() -> None:
    derived = derived_resource()
    assert derived.with_lifecycle("deleted").lifecycle == "deleted"
    assert derived.with_lifecycle("outside_control").lifecycle == "outside_control"


# ---------------------------------------------------------------------------
# Sources: external references never replace video evidence
# ---------------------------------------------------------------------------


def test_sources_record_support_limits_and_access_date() -> None:
    reference = source()
    assert reference.supports == ["unit turn precedes the forward swing"]
    assert reference.limitations
    assert reference.accessed_on == dt.date(2026, 11, 6)


def test_sources_need_an_http_url_and_at_least_one_limit() -> None:
    with pytest.raises(ValidationError):
        source(url="ftp://example.org/unit-turn")
    with pytest.raises(ValidationError):
        source(limitations=[])


# ---------------------------------------------------------------------------
# Issues: evidence-bound, safety first, at most three published
# ---------------------------------------------------------------------------


def test_insufficient_or_unsupported_issues_are_not_publishable() -> None:
    assert issue().publishable is True
    assert issue(sufficiency="insufficient").publishable is False
    assert issue(evidence_ids=[]).publishable is False
    assert issue(safety_blocked=True).publishable is False


def test_publication_is_bounded_to_three_and_ordered_safety_first() -> None:
    candidates = [
        issue(issue_id=f"issue-{index}", priority=index, sufficiency="partial")
        for index in range(1, 6)
    ]
    with_blocked_first = IssueSet(
        issues=[issue(issue_id="issue-0", safety_blocked=True, priority=0), *candidates]
    )
    published = with_blocked_first.published()
    assert len(published) == MAX_PUBLISHED_ISSUES
    assert published[0].issue_id == "issue-1"
    assert all(item.sufficiency == "partial" for item in published)


def test_published_issues_must_cite_task_evidence() -> None:
    supported = IssueSet(issues=[issue()])
    known = evidence_set()
    supported.assert_publishable_against(known)
    unsupported = IssueSet(issues=[issue(), issue(issue_id="issue-2", evidence_ids=["ev-404"])])
    with pytest.raises(DomainViolation):
        unsupported.assert_publishable_against(known)


def test_issue_ids_stay_unique() -> None:
    with pytest.raises(ValidationError):
        IssueSet(issues=[issue(), issue()])


# ---------------------------------------------------------------------------
# Domain stays decoupled from transport, contracts and adapters
# ---------------------------------------------------------------------------


def test_domain_module_imports_only_stdlib_and_pydantic() -> None:
    tree = ast.parse(DOMAIN_MODELS.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)
    assert imported <= {
        "__future__",
        "datetime",
        "pydantic",
        "typing",
    }


def test_domain_module_has_no_platform_or_media_names() -> None:
    text = DOMAIN_MODELS.read_text(encoding="utf-8").lower()
    for forbidden in ("workbuddy", "mcp", "cv2", "ffmpeg", "socket", "subprocess", "http.client"):
        assert forbidden not in text


def test_every_entity_is_frozen_and_rejects_unknown_fields() -> None:
    frozen = task()
    with pytest.raises(ValidationError):
        frozen.state = "cancelled"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        task(unexpected_field="nope")  # type: ignore[call-arg]
