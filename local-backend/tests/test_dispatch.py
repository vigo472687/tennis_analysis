"""Full frozen-method dispatch tests (BE-027).

Every new dispatch path is exercised through the real ``--stdio``
subprocess against a real data root, with task-config-driven fixture
addressing. Heavy media flows (render) are covered by their honest refusal
here plus the BE-020 unit suite; export uses the durable task-config record
as a real selected resource.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.storage.resources import ResourceStore
from test_cli import BackendCli, envelope

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "v1"


def create_params(operation_id: str, fixture_id: str = "iphone-flat") -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "fixture_id": fixture_id,
        "media_ref": "fixtures/media/flat-forehand.mp4",
        "user": {
            "age_band": "adult",
            "handedness": "right",
            "target_stroke": "forehand",
            "goal": "stabilise the contact point",
            "exercise_background": "two years of recreational play",
            "safety": "clear",
        },
        "consent": {
            "consent_id": "consent-1",
            "task_version": 1,
            "purposes": ["analysis", "diagnostics"],
            "text_version": "consent-text-1",
            "confirmed": True,
            "guardian_simulated": True,
            "recipient_digest": None,
            "materials_digest": None,
        },
        "primary_route": "technical_diagnosis",
        "auxiliary_routes": ["tactics"],
        "subject_id": None,
        "mirror_confirmed": False,
    }


def lookup_params(task_id: str, version: int = 1) -> dict[str, Any]:
    return {"task_id": task_id, "version": version}


@pytest.fixture
def backend(tmp_path: Path) -> Any:
    proc = BackendCli(tmp_path)
    try:
        yield proc
    finally:
        proc.kill()


def created_task(
    backend: BackendCli, operation_id: str = "op-d-1", fixture_id: str = "iphone-flat"
) -> str:
    backend.send(
        envelope(
            f"req-{operation_id}",
            "analysis.create",
            create_params(operation_id, fixture_id=fixture_id),
            operation_id=operation_id,
        )
    )
    created = backend.response()
    assert created["ok"] is True
    return str(created["result"]["task_id"])


# ---------------------------------------------------------------------------
# Fixture-stage dispatch: quality / strokes / timing / metrics
# ---------------------------------------------------------------------------


def test_quality_dispatch_returns_frozen_media_quality(backend: BackendCli) -> None:
    task_id = created_task(backend)
    backend.send(envelope("req-q", "analysis.quality", lookup_params(task_id)))
    payload = backend.response()
    assert payload["ok"] is True
    quality = payload["result"]
    assert quality["status"] == "pass"
    assert quality["simulation"] is True
    assert quality["media"]["support"] == "supported"


def test_stage_dispatch_returns_contract_results(backend: BackendCli) -> None:
    task_id = created_task(backend)
    expected = {
        "analysis.strokes": ("events",),
        "analysis.timing": ("events", "limitations"),
        "analysis.metrics": ("metrics",),
    }
    for index, (method, keys) in enumerate(expected.items()):
        backend.send(envelope(f"req-s{index}", method, lookup_params(task_id)))
        payload = backend.response()
        assert payload["ok"] is True, (method, payload)
        assert set(keys) <= set(payload["result"])
        assert payload["result"]["simulation"] is True


def test_stage_dispatch_for_unknown_task_refused(backend: BackendCli) -> None:
    backend.send(envelope("req-u", "analysis.metrics", lookup_params("task-missing")))
    payload = backend.response()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "DOMAIN_TASK_CONFIG_MISSING"


# ---------------------------------------------------------------------------
# Cancel / delete / reset dispatch
# ---------------------------------------------------------------------------


def test_cancel_dispatch_answers_with_cancelled_summary(backend: BackendCli) -> None:
    task_id = created_task(backend)
    backend.send(
        envelope(
            "req-c",
            "analysis.cancel",
            {"operation_id": "op-cancel", "task_id": task_id, "version": 1},
            operation_id="op-cancel",
        )
    )
    payload = backend.response()
    assert payload["ok"] is True
    assert payload["result"]["state"] == "cancelled"


def test_delete_dispatch_reports_per_item(backend: BackendCli) -> None:
    task_id = created_task(backend)
    backend.send(
        envelope(
            "req-d",
            "analysis.delete",
            {
                "operation_id": "op-del",
                "task_id": task_id,
                "version": 1,
                "scope": "task",
                "confirmed": True,
            },
            operation_id="op-del",
        )
    )
    payload = backend.response()
    assert payload["ok"] is True
    assert payload["result"]["complete"] is True  # the task-config record is deleted
    assert payload["result"]["runtime_uninstalled"] is False
    statuses = {item["status"] for item in payload["result"]["items"]}
    assert statuses <= {"deleted", "not_found"}


def test_all_demo_delete_dispatch_routes_to_reset(backend: BackendCli) -> None:
    created_task(backend, "op-d-a")
    backend.send(
        envelope(
            "req-r",
            "analysis.delete",
            {
                "operation_id": "op-reset-wire",
                "task_id": None,
                "version": None,
                "scope": "all_demo",
                "confirmed": True,
            },
            operation_id="op-reset-wire",
        )
    )
    payload = backend.response()
    assert payload["ok"] is True
    assert payload["result"]["complete"] is True
    assert payload["result"]["runtime_uninstalled"] is False


# ---------------------------------------------------------------------------
# Compare / target / render / export
# ---------------------------------------------------------------------------


def test_compare_dispatch_on_same_fixture_is_comparable(backend: BackendCli) -> None:
    first = created_task(backend, "op-d-c1")
    second = created_task(backend, "op-d-c2")
    backend.send(
        envelope(
            "req-cmp",
            "sessions.compare",
            {"task_id": first, "version": 1, "other_task_id": second, "other_version": 1},
        )
    )
    payload = backend.response()
    assert payload["ok"] is True
    assert payload["result"]["comparable"] is True
    assert payload["result"]["simulation"] is True


def test_compare_dispatch_across_fixtures_lists_differences(backend: BackendCli) -> None:
    first = created_task(backend, "op-d-f1")
    second = created_task(backend, operation_id="op-d-f2", fixture_id="gopro-flat")
    backend.send(
        envelope(
            "req-cmp2",
            "sessions.compare",
            {"task_id": first, "version": 1, "other_task_id": second, "other_version": 1},
        )
    )
    payload = backend.response()
    assert payload["result"]["comparable"] is False
    assert payload["result"]["changes"] == []
    assert payload["result"]["differences"]


def test_target_generate_returns_watermarked_skeleton(backend: BackendCli) -> None:
    task_id = created_task(backend)
    backend.send(
        envelope(
            "req-t",
            "target.generate",
            {
                "operation_id": "op-t",
                "task_id": task_id,
                "version": 1,
                "features": ["contact reference"],
            },
            operation_id="op-t",
        )
    )
    payload = backend.response()
    assert payload["ok"] is True
    assert payload["result"]["kind"] == "target_pose"
    assert payload["result"]["simulation"] is True
    assert any(
        "NOT MEASURED" in item or "not a detected pose" in item
        for item in payload["result"]["limitations"]
    )


def test_render_without_input_copy_refused_honestly(backend: BackendCli) -> None:
    task_id = created_task(backend)
    backend.send(
        envelope(
            "req-r",
            "evidence.render",
            {
                "operation_id": "op-r",
                "task_id": task_id,
                "version": 1,
                "evidence_ids": ["ev-task-demo-v1-stroke"],
                "mode": "frame",
            },
            operation_id="op-r",
        )
    )
    payload = backend.response()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "DOMAIN_INPUT_COPY_MISSING"


def test_export_dispatch_packages_selected_record(backend: BackendCli, tmp_path: Path) -> None:
    task_id = created_task(backend)
    # the durable task-config record is a real published resource
    store = ResourceStore(tmp_path / "data" / "public", tmp_path / "data" / "internal")
    record = [r for r in store.resources(task_id) if r.path.endswith("task-config.json")][0]
    backend.send(
        envelope(
            "req-e",
            "review.export",
            {
                "operation_id": "op-exp",
                "task_id": task_id,
                "version": 1,
                "selected_resource_ids": [record.resource_id],
                "consent": create_params("op-d-1")["consent"],
            },
            operation_id="op-exp",
        )
    )
    payload = backend.response()
    assert payload["ok"] is True
    assert payload["result"]["redacted"] is True
    assert any(item["resource_id"] == record.resource_id for item in payload["result"]["resources"])
    registry = ContractRegistry.load(CONTRACTS_DIR)
    assert (
        registry.validate(registry.definition_ref("deletion", "export_result"), payload["result"])
        == ()
    )


def test_runtime_methods_stay_bridge_owned(backend: BackendCli) -> None:
    for index, method in enumerate(
        ("runtime.prepare", "runtime.start", "runtime.repair", "runtime.uninstall")
    ):
        params: dict[str, Any] = {
            "operation_id": f"op-rt-{index}",
            "runtime_requirement": "required",
        }
        if method == "runtime.prepare":
            params = {
                "operation_id": f"op-rt-{index}",
                "consent_confirmed": True,
                "target_version": "0.1.0",
                "intent": "prepare",
                "scope_digest": "digest-scope-0",
            }
        if method == "runtime.repair":
            params = {
                "operation_id": f"op-rt-{index}",
                "consent_confirmed": True,
                "scope_digest": "digest-scope-0",
            }
        if method == "runtime.uninstall":
            params = {
                "operation_id": f"op-rt-{index}",
                "confirmed": True,
                "keep_project_data": True,
            }
        backend.send(
            envelope(f"req-rt-{index}", method, params, operation_id=params.get("operation_id"))
        )
        payload = backend.response()
        assert payload["ok"] is False
        assert payload["error"]["code"] == "RUNTIME_BRIDGE_OWNED", (method, payload)
    # the process keeps serving
    backend.send(
        envelope(
            "req-after", "runtime.status", {"runtime_requirement": "not_required"}, simulation=False
        )
    )
    assert backend.response()["ok"] is True
