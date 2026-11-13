"""Contract validation tests for the frozen Codex-Claude contracts (BE-003).

Positive and negative cases cover the envelope, cross-version and unknown
method handling, method-specific params/results, operation id and simulation
consistency, evidence/target exclusion, and purely local ``$ref`` resolution.
All samples are synthetic fixture-shaped payloads; no real media or network is
used.
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from referencing.exceptions import Unresolvable

from tennis_backend.contracts.validation import (
    BASE_URI,
    CONTRACT_VERSION,
    ContractRegistry,
    ContractViolation,
    CreateInput,
    Lookup,
    RuntimeResult,
    TaskSummary,
    build_error,
    build_error_response,
    build_response,
    check_simulation_consistency,
    contract_violation_response,
    validate_request,
    validate_response,
    validate_result,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "v1"
BASELINE_PATH = REPO_ROOT / "integration" / "reports" / "contract-baseline.json"
PROTOCOL_SAMPLES = REPO_ROOT / "fixtures" / "expected" / "protocol.json"


@pytest.fixture(scope="module")
def registry() -> ContractRegistry:
    """One registry shared by the module, loaded from the frozen contracts."""
    return ContractRegistry.load(CONTRACTS_DIR)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if anything tries to reach the network."""

    def _deny(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny)


def consent_payload(**overrides: Any) -> dict[str, Any]:
    """A fresh consent snapshot for one task version."""
    payload: dict[str, Any] = {
        "consent_id": "consent-1",
        "task_version": 1,
        "purposes": ["analysis"],
        "text_version": "consent-text-1",
        "confirmed": True,
        "guardian_simulated": True,
        "recipient_digest": None,
        "materials_digest": None,
    }
    payload.update(overrides)
    return payload


def user_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "age_band": "adult",
        "handedness": "right",
        "target_stroke": "forehand",
        "goal": "stabilise the contact point",
        "exercise_background": "two years of recreational play",
        "safety": "clear",
    }
    payload.update(overrides)
    return payload


def create_params(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operation_id": "op-create-1",
        "fixture_id": "fixture-flat-1",
        "media_ref": "fixtures/media/flat-forehand.mp4",
        "user": user_payload(),
        "consent": consent_payload(),
        "primary_route": "technical_diagnosis",
        "auxiliary_routes": ["tactics"],
        "subject_id": None,
        "mirror_confirmed": False,
    }
    payload.update(overrides)
    return payload


def lookup_params(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"task_id": "task-demo", "version": 1}
    payload.update(overrides)
    return payload


def summary_result(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": "task-demo",
        "version": 1,
        "state": "analyzing",
        "stage": "analysis",
        "checkpoints": ["checking_input", "analyzing"],
        "retained": ["media", "evidence"],
        "recovery_actions": ["none"],
        "workspace_ref": "projects/task-demo/v1",
        "simulation": True,
    }
    payload.update(overrides)
    return payload


def metrics_result(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "metrics": [
            {
                "metric_id": "metric-hip-rotation",
                "definition": "relative hip rotation at contact, normalized to frame width",
                "value": 0.42,
                "value_state": "observed",
                "unit": "relative",
                "reference": "fixture-config-1 baseline",
                "evidence_ids": ["ev-frame-1"],
                "limitations": ["monocular fixture observation, no physical force claim"],
            }
        ],
        "simulation": True,
    }
    payload.update(overrides)
    return payload


def evidence_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "evidence_id": "ev-frame-1",
        "kind": "video_frame",
        "asset_id": "asset-1",
        "subject_id": "subject-1",
        "original_start_s": 1.5,
        "original_end_s": 2.5,
        "visible_objects": ["body", "racket"],
        "resource": {
            "resource_id": "res-frame-1",
            "path": "projects/task-demo/v1/evidence/frame-1.png",
            "mime": "image/png",
            "sha256": "a" * 64,
            "size_bytes": 2048,
            "kind": "evidence_frame",
            "simulation": True,
        },
        "simulation": True,
    }
    payload.update(overrides)
    return payload


def render_result_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "resources": [
            {
                "resource_id": "res-overlay-1",
                "path": "projects/task-demo/v1/evidence/overlay-1.mp4",
                "mime": "video/mp4",
                "sha256": "b" * 64,
                "size_bytes": 4096,
                "kind": "evidence_clip",
                "simulation": True,
            }
        ],
        "evidence": [evidence_payload()],
        "simulation": True,
    }
    payload.update(overrides)
    return payload


def request_payload(
    method: str,
    params: dict[str, Any],
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "request_id": "req-1",
        "operation_id": params.get("operation_id"),
        "method": method,
        "params": params,
        "simulation": True,
    }
    payload.update(overrides)
    return payload


def response_payload(result: dict[str, Any] | None, **overrides: Any) -> dict[str, Any]:
    error = (
        None
        if result is not None
        else {
            "code": "TASK_NOT_FOUND",
            "stage": "input",
            "retryable": False,
            "user_action": "none",
            "retained": [],
            "details_ref": None,
            "simulation": True,
        }
    )
    payload: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "request_id": "req-1",
        "ok": result is not None,
        "result": result,
        "error": error,
        "simulation": True,
    }
    payload.update(overrides)
    return payload


def violations(exc: ContractViolation) -> str:
    return " | ".join(exc.violations)


# ---------------------------------------------------------------------------
# Frozen baseline
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not BASELINE_PATH.exists(), reason="frozen contract baseline not present")
def test_frozen_schemas_match_baseline_hashes() -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert baseline["contract_version"] == CONTRACT_VERSION
    for relative, expected_hash in baseline["files"].items():
        if not relative.startswith("contracts/v1/"):
            continue
        digest = hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest()
        assert digest == expected_hash, relative


def test_registry_loads_all_nine_schemas(registry: ContractRegistry) -> None:
    assert registry.contract_version == CONTRACT_VERSION
    for name in (
        "analysis",
        "common",
        "deletion",
        "error",
        "feedback",
        "media",
        "report-input",
        "runtime",
        "task",
    ):
        assert registry.schema(name)["$id"] == f"{BASE_URI}{name}.schema.json"


@pytest.mark.skipif(not BASELINE_PATH.exists(), reason="frozen contract baseline not present")
def test_x_tools_registry_matches_frozen_baseline(registry: ContractRegistry) -> None:
    """``x-tools`` is the only tool registry; it must equal the frozen list."""
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    expected = {tool["method"]: tool for tool in baseline["tools"]}
    actual = {tool.method: tool for tool in registry.tools}
    assert len(actual) == 17
    assert set(actual) == set(expected)
    for method, tool in actual.items():
        want = expected[method]
        assert tool.name == want["name"]
        assert tool.input_ref == want["input_ref"]
        assert tool.output_ref == want["output_ref"]
        assert tool.side_effect == want["side_effect"]
        assert tool.purposes == tuple(want["purposes"])


def test_unknown_definition_and_file_are_rejected(registry: ContractRegistry) -> None:
    with pytest.raises(ContractViolation) as file_error:
        registry.schema("nonexistent")
    assert file_error.value.code == "CONTRACT_FILE_UNKNOWN"
    with pytest.raises(ContractViolation) as definition_error:
        registry.definition_ref("task", "not_a_definition")
    assert definition_error.value.code == "CONTRACT_DEFINITION_UNKNOWN"
    with pytest.raises(Unresolvable):
        registry.validate(f"{BASE_URI}task.schema.json#/definitions/not_a_definition", {})


# ---------------------------------------------------------------------------
# Local reference resolution
# ---------------------------------------------------------------------------


def test_cross_file_refs_resolve_offline(registry: ContractRegistry, no_network: None) -> None:
    """Relative refs resolve inside the frozen file set; never over network."""
    error_def = registry.schema("error")["definitions"]["error"]
    assert error_def["properties"]["details_ref"]["anyOf"][0]["$ref"] == (
        "common.schema.json#/definitions/relative_path"
    )
    registry.require_valid(
        f"{BASE_URI}error.schema.json#/definitions/error",
        build_error(code="TASK_NOT_FOUND", stage="input", details_ref=None),
    )


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def test_valid_envelopes_are_accepted(registry: ContractRegistry) -> None:
    validated = validate_request(registry, request_payload("analysis.status", lookup_params()))
    assert validated.method == "analysis.status"
    assert validated.operation_id is None
    assert validated.typed_params(Lookup).task_id == "task-demo"

    created = validate_request(registry, request_payload("analysis.create", create_params()))
    assert created.tool.side_effect is True
    assert created.operation_id == "op-create-1"
    typed = created.typed_params(CreateInput)
    assert typed.user.age_band == "adult"
    assert typed.consent.purposes == ["analysis"]


def test_contract_version_mismatch_is_rejected(registry: ContractRegistry) -> None:
    for version in ("1.0.0", "2.0.0", "1"):
        with pytest.raises(ContractViolation) as envelope_error:
            validate_request(
                registry,
                request_payload("analysis.status", lookup_params(), contract_version=version),
            )
        assert envelope_error.value.code == "REQUEST_ENVELOPE_INVALID"
        assert "contract_version" in violations(envelope_error.value)


def test_missing_and_extra_envelope_fields_are_rejected(registry: ContractRegistry) -> None:
    payload = request_payload("analysis.status", lookup_params())
    del payload["request_id"]
    with pytest.raises(ContractViolation) as missing:
        validate_request(registry, payload)
    assert "request_id" in violations(missing.value)

    with pytest.raises(ContractViolation) as extra:
        validate_request(
            registry,
            request_payload("analysis.status", lookup_params(), trace="should-not-exist"),
        )
    assert "trace" in violations(extra.value)
    assert extra.value.code == "REQUEST_ENVELOPE_INVALID"


def test_unknown_method_is_rejected(registry: ContractRegistry) -> None:
    with pytest.raises(ContractViolation) as unknown:
        validate_request(registry, request_payload("analysis.snapshot", lookup_params()))
    assert unknown.value.code == "METHOD_UNKNOWN"
    with pytest.raises(ContractViolation) as malformed:
        validate_request(registry, request_payload("Analysis.Status", lookup_params()))
    assert malformed.value.code == "REQUEST_ENVELOPE_INVALID"


def test_response_envelope_pairing_rules(registry: ContractRegistry) -> None:
    validate_response(registry, response_payload(summary_result()), method="analysis.status")
    validate_response(registry, response_payload(None), method="analysis.status")

    with pytest.raises(ContractViolation) as ok_with_error:
        validate_response(
            registry,
            response_payload(
                None, ok=True, error=build_error(code="TASK_NOT_FOUND", stage="input")
            ),
        )
    assert ok_with_error.value.code == "RESPONSE_ENVELOPE_INVALID"
    assert "error" in violations(ok_with_error.value)

    with pytest.raises(ContractViolation) as failed_with_result:
        validate_response(registry, response_payload(summary_result(), ok=False))
    assert "result" in violations(failed_with_result.value)

    with pytest.raises(ContractViolation) as ok_without_error:
        validate_response(registry, response_payload(None, ok=False, error=None))
    assert "error" in violations(ok_without_error.value)

    lowercase = build_error(code="TASK_NOT_FOUND", stage="input")
    lowercase["code"] = "task_not_found"
    with pytest.raises(ContractViolation) as bad_code:
        validate_response(registry, response_payload(None, error=lowercase))
    assert "code" in violations(bad_code.value)


# ---------------------------------------------------------------------------
# Method-specific params and results
# ---------------------------------------------------------------------------


def test_method_specific_input_shapes_are_enforced(registry: ContractRegistry) -> None:
    with pytest.raises(ContractViolation) as wrong_shape:
        validate_request(registry, request_payload("analysis.create", lookup_params()))
    assert wrong_shape.value.code == "REQUEST_PARAMS_INVALID"

    with pytest.raises(ContractViolation) as bad_route:
        validate_request(
            registry,
            request_payload(
                "analysis.create",
                create_params(primary_route="professional_scouting"),
            ),
        )
    assert "primary_route" in violations(bad_route.value)

    with pytest.raises(ContractViolation) as bad_id:
        validate_request(
            registry,
            request_payload("analysis.status", lookup_params(task_id="-task demo")),
        )
    assert "task_id" in violations(bad_id.value)

    with pytest.raises(ContractViolation) as unconfirmed:
        validate_request(
            registry,
            request_payload(
                "runtime.prepare",
                {
                    "operation_id": "op-prepare-1",
                    "consent_confirmed": False,
                    "target_version": "1.1.0",
                    "intent": "prepare",
                    "scope_digest": "scope-1",
                },
            ),
        )
    assert "consent_confirmed" in violations(unconfirmed.value)


def test_method_specific_output_shapes_are_enforced(registry: ContractRegistry) -> None:
    validate_result(registry, "analysis.status", summary_result())
    validate_result(registry, "analysis.metrics", metrics_result())
    validate_result(
        registry,
        "runtime.status",
        {
            "state": "ready",
            "version": "1.1.0",
            "backend": "fixture",
            "checks": [{"code": "ffmpeg-present", "passed": True}],
            "retained": [],
            "simulation": True,
        },
    )

    with pytest.raises(ContractViolation) as bad_state:
        validate_result(registry, "analysis.status", summary_result(state="publishing"))
    assert "state" in violations(bad_state.value)

    with pytest.raises(ContractViolation) as bad_unit:
        validate_result(
            registry,
            "analysis.metrics",
            metrics_result(
                metrics=[
                    {
                        "metric_id": "metric-force",
                        "definition": "absolute racket speed",
                        "value": 12.5,
                        "value_state": "observed",
                        "unit": "newton_meters",
                        "reference": "none",
                        "evidence_ids": [],
                        "limitations": [],
                    }
                ]
            ),
        )
    assert "unit" in violations(bad_unit.value)

    typed = validate_result(registry, "analysis.status", summary_result()).result
    assert isinstance(typed, TaskSummary)
    assert typed.state == "analyzing"


def test_compare_result_must_not_carry_changes_when_not_comparable(
    registry: ContractRegistry,
) -> None:
    comparable = {
        "comparable": True,
        "differences": [],
        "changes": metrics_result()["metrics"],
        "simulation": True,
    }
    validate_result(registry, "sessions.compare", comparable)

    not_comparable = {
        "comparable": False,
        "differences": ["different fixture scenario and subject"],
        "changes": metrics_result()["metrics"],
        "simulation": True,
    }
    with pytest.raises(ContractViolation) as mismatch:
        validate_result(registry, "sessions.compare", not_comparable)
    assert mismatch.value.code == "RESULT_INVALID"
    assert "changes" in violations(mismatch.value)


def test_result_of_the_wrong_method_is_rejected(registry: ContractRegistry) -> None:
    with pytest.raises(ContractViolation) as wrong_method:
        validate_result(registry, "runtime.status", summary_result())
    assert wrong_method.value.code == "RESULT_INVALID"


# ---------------------------------------------------------------------------
# Operation id consistency
# ---------------------------------------------------------------------------


def test_operation_id_mismatch_is_rejected(registry: ContractRegistry) -> None:
    with pytest.raises(ContractViolation) as mismatch:
        validate_request(
            registry,
            request_payload(
                "analysis.create",
                create_params(),
                operation_id="op-other-attempt",
            ),
        )
    assert mismatch.value.code == "REQUEST_INCONSISTENT"
    assert "does not match params.operation_id" in violations(mismatch.value)


def test_side_effect_without_operation_id_is_rejected(registry: ContractRegistry) -> None:
    params = create_params()
    del params["operation_id"]
    with pytest.raises(ContractViolation) as missing:
        validate_request(registry, request_payload("analysis.create", params))
    assert missing.value.code == "REQUEST_PARAMS_INVALID"
    assert "operation_id" in violations(missing.value)

    with pytest.raises(ContractViolation) as envelope:
        validate_request(
            registry,
            request_payload("analysis.create", create_params(), operation_id=None),
        )
    assert envelope.value.code == "REQUEST_INCONSISTENT"
    assert "must repeat params.operation_id" in violations(envelope.value)


# ---------------------------------------------------------------------------
# Unknown fields
# ---------------------------------------------------------------------------


def test_unknown_params_and_result_fields_are_rejected(registry: ContractRegistry) -> None:
    with pytest.raises(ContractViolation) as params_extra:
        validate_request(
            registry,
            request_payload("analysis.status", lookup_params(force=True)),
        )
    assert "force" in violations(params_extra.value)
    assert params_extra.value.code == "REQUEST_PARAMS_INVALID"

    with pytest.raises(ContractViolation) as result_extra:
        validate_result(registry, "analysis.status", summary_result(prognosis="great"))
    assert "prognosis" in violations(result_extra.value)


def test_unknown_resource_fields_are_rejected(registry: ContractRegistry) -> None:
    evidence = evidence_payload()
    evidence["resource"]["bytes_preview"] = "raw-bytes-not-allowed"  # type: ignore[index]
    with pytest.raises(ContractViolation) as raw:
        validate_result(registry, "evidence.render", render_result_payload(evidence=[evidence]))
    assert "bytes_preview" in violations(raw.value)


# ---------------------------------------------------------------------------
# Simulation consistency
# ---------------------------------------------------------------------------


def test_simulation_flag_mismatch_is_rejected(registry: ContractRegistry) -> None:
    """A self-consistent result passes alone but fails inside a mixed envelope."""
    standalone = validate_result(registry, "analysis.status", summary_result(simulation=False))
    assert standalone.simulation is False

    with pytest.raises(ContractViolation) as envelope_flags:
        validate_response(
            registry,
            response_payload(summary_result(), simulation=False),
            method="analysis.status",
        )
    assert envelope_flags.value.code == "RESPONSE_INCONSISTENT"
    assert "simulation" in violations(envelope_flags.value)

    with pytest.raises(ContractViolation) as nested_flags:
        validate_result(
            registry,
            "evidence.render",
            render_result_payload(
                evidence=[evidence_payload(simulation=False)],
            ),
        )
    assert nested_flags.value.code == "RESULT_INCONSISTENT"
    assert "simulation" in violations(nested_flags.value)

    assert (
        check_simulation_consistency(
            {"a": [{"simulation": True}], "b": {"simulation": True}},
            expected=True,
        )
        == ()
    )
    assert "contradicts" in check_simulation_consistency({"simulation": False}, expected=True)[0]


def test_simulation_key_inside_params_is_an_unknown_field(registry: ContractRegistry) -> None:
    params = create_params()
    params["simulation"] = False
    with pytest.raises(ContractViolation) as rejected:
        validate_request(registry, request_payload("analysis.create", params))
    assert rejected.value.code == "REQUEST_PARAMS_INVALID"
    assert "simulation" in violations(rejected.value)


def test_build_response_refuses_mixed_simulation_flags(registry: ContractRegistry) -> None:
    with pytest.raises(ContractViolation) as mixed:
        build_response(
            request_id="req-1",
            method="analysis.status",
            registry=registry,
            result=summary_result(simulation=False),
            simulation=True,
        )
    assert mixed.value.code == "RESPONSE_INCONSISTENT"


# ---------------------------------------------------------------------------
# Target pose exclusion
# ---------------------------------------------------------------------------


def test_target_pose_resource_is_not_evidence(registry: ContractRegistry) -> None:
    evidence = evidence_payload()
    evidence["resource"]["kind"] = "target_pose"  # type: ignore[index]
    with pytest.raises(ContractViolation) as schema_rejects:
        validate_result(registry, "evidence.render", render_result_payload(evidence=[evidence]))
    assert "kind" in violations(schema_rejects.value)


def test_target_pose_resource_is_not_render_output(registry: ContractRegistry) -> None:
    resources = render_result_payload()["resources"]
    resources[0]["kind"] = "target_pose"  # type: ignore[index]
    with pytest.raises(ContractViolation) as render_rejects:
        validate_result(registry, "evidence.render", render_result_payload(resources=resources))
    assert "target_pose" in violations(render_rejects.value)


def test_target_result_keeps_its_own_resource_kind(registry: ContractRegistry) -> None:
    target = {
        "kind": "target_pose",
        "resource": {
            "resource_id": "res-target-1",
            "path": "projects/task-demo/v1/target/pose.svg",
            "mime": "image/svg+xml",
            "sha256": "c" * 64,
            "size_bytes": 1024,
            "kind": "evidence_frame",
            "simulation": True,
        },
        "features": ["contact height"],
        "limitations": ["generic simulated skeleton, not a measured pose"],
        "simulation": True,
    }
    with pytest.raises(ContractViolation) as wrong_kind:
        validate_result(registry, "target.generate", target)
    assert "kind" in violations(wrong_kind.value)

    target["resource"]["kind"] = "target_pose"  # type: ignore[index]
    validate_result(registry, "target.generate", target)


def test_evidence_intervals_must_be_ordered(registry: ContractRegistry) -> None:
    evidence = evidence_payload(original_start_s=3.0, original_end_s=1.0)
    with pytest.raises(ContractViolation) as unordered:
        validate_result(registry, "evidence.render", render_result_payload(evidence=[evidence]))
    assert "original_end_s" in violations(unordered.value)


# ---------------------------------------------------------------------------
# Path boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("/etc/passwd", "absolute path"),
        ("C:/media/clip.mp4", "drive letter"),
        ("projects/../secrets/keys.txt", "traversal segment"),
        ("projects/task/../..", "trailing traversal"),
    ],
)
def test_relative_path_rejects_escape_attempts(
    registry: ContractRegistry,
    path: str,
    reason: str,
) -> None:
    with pytest.raises(ContractViolation) as rejected:
        validate_request(
            registry, request_payload("analysis.create", create_params(media_ref=path))
        )
    assert rejected.value.code == "REQUEST_PARAMS_INVALID"
    assert "media_ref" in violations(rejected.value), reason

    with pytest.raises(ContractViolation) as workspace_rejected:
        validate_result(
            registry,
            "analysis.status",
            summary_result(workspace_ref=path),
        )
    assert "workspace_ref" in violations(workspace_rejected.value), reason


# ---------------------------------------------------------------------------
# Builders and fixture protocol samples
# ---------------------------------------------------------------------------


def test_build_helpers_round_trip(registry: ContractRegistry) -> None:
    payload = build_response(
        request_id="req-1",
        method="analysis.status",
        registry=registry,
        result=summary_result(),
    )
    assert validate_response(registry, payload, method="analysis.status").ok is True

    error = build_error(
        code="TASK_NOT_FOUND",
        stage="input",
        user_action="none",
        details_ref=None,
    )
    failed = build_error_response(request_id="req-2", registry=registry, error=error)
    assert validate_response(registry, failed).ok is False

    with pytest.raises(ContractViolation) as invalid:
        validate_request(
            registry,
            request_payload("analysis.status", lookup_params(task_id="bad id")),
        )
    mapped = contract_violation_response(registry, "req-1", invalid.value)
    assert mapped["error"]["code"] == invalid.value.code
    assert validate_response(registry, mapped).ok is False

    with pytest.raises(ContractViolation) as bad_stage:
        build_error_response(
            request_id="req-3",
            registry=registry,
            error=build_error(code="TASK_NOT_FOUND", stage="orbit"),
        )
    assert "stage" in violations(bad_stage.value)


@pytest.mark.skipif(not PROTOCOL_SAMPLES.exists(), reason="fixture protocol samples not present")
def test_frozen_protocol_samples_validate(registry: ContractRegistry) -> None:
    """Every frozen protocol sample round-trips through this validation layer."""
    cases = json.loads(PROTOCOL_SAMPLES.read_text(encoding="utf-8"))["cases"]
    assert cases, "protocol samples must not be empty"
    for case in cases:
        params = case["params"]
        request = {
            "contract_version": CONTRACT_VERSION,
            "request_id": case["response"]["request_id"],
            "operation_id": params.get("operation_id"),
            "method": case["method"],
            "params": params,
            "simulation": True,
        }
        validated = validate_request(registry, request)
        assert validated.method == case["method"]

        response = dict(case["response"])
        if response["ok"]:
            assert validate_result(registry, case["method"], response["result"]) is not None
        validate_response(registry, response, method=case["method"] if response["ok"] else None)


def test_typed_shapes_stay_frozen(registry: ContractRegistry) -> None:
    """Typed bindings exist for every tool definition and reject unknown data."""
    for tool in registry.tools:
        file_name, definition = tool.input_ref.partition("#/definitions/")[::2]
        assert (
            registry.typed_definition(
                file_name.rsplit("/", 1)[-1].removesuffix(".schema.json"),
                definition,
            )
            is not None
        )
    with pytest.raises(ContractViolation) as missing:
        registry.typed_definition("task", "states")
    assert missing.value.code == "CONTRACT_SHAPE_MISSING"
    with pytest.raises(ContractViolation) as not_a_registry_file:
        registry.typed_definition("report-input", "report")
    assert not_a_registry_file.value.code == "CONTRACT_SHAPE_MISSING"


def test_runtime_result_backend_is_bounded(registry: ContractRegistry) -> None:
    result = {
        "state": "ready",
        "version": "1.1.0",
        "backend": "workbuddy-cloud",
        "checks": [],
        "retained": [],
        "simulation": True,
    }
    with pytest.raises(ContractViolation) as rejected:
        validate_result(registry, "runtime.status", result)
    assert "backend" in violations(rejected.value)
    typed = validate_result(registry, "runtime.status", {**result, "backend": "fixture"}).result
    assert isinstance(typed, RuntimeResult)
    assert typed.backend == "fixture"
