"""Redacted diagnostic package export tests (BE-024, AC-SPEC-036).

Covers: only selected materials enter, JSON diagnostics are redacted
(identity/conversation keys and absolute paths), original video refused,
failed materials recorded but not attached, manifest completeness (hashes
match the packaged files), the frozen export_result contract shape, and the
absence of any send capability in the module.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.export.package import SelectedMaterial, build_diagnostic_package

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "v1"


@pytest.fixture(scope="module")
def registry() -> ContractRegistry:
    return ContractRegistry.load(CONTRACTS_DIR)


@pytest.fixture
def env(tmp_path: Path) -> tuple[Path, dict[str, SelectedMaterial]]:
    root = tmp_path / "public"
    root.mkdir()
    source = tmp_path / "source"
    source.mkdir()

    diagnostics = {
        "task_id": "task-demo",
        "user_name": "王小明",
        "email": "ming@example.com",
        "chat": ["教练你好", "我的正手总打框"],
        "video_path": "/Users/vigo/Private/ Vidéos/正手.mp4",
        "state_history": ["draft", "analyzing"],
        "metrics": {"preparation-order": 1.25},
    }
    (source / "diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False), encoding="utf-8"
    )
    frame = source / "frame.png"
    frame.write_bytes(b"\x89PNG-fake-image-bytes")

    selected = {
        "diag": SelectedMaterial(
            resource_id="diag",
            path=source / "diagnostics.json",
            relative="projects/task-demo/v1/diagnostics.json",
            kind="diagnostics",
            mime="application/json",
        ),
        "frame": SelectedMaterial(
            resource_id="frame",
            path=frame,
            relative="projects/task-demo/v1/evidence/frame.png",
            kind="evidence_frame",
            mime="image/png",
        ),
        "original": SelectedMaterial(
            resource_id="original",
            path=source / "original.mp4",
            relative="projects/task-demo/v1/media/original.mp4",
            kind="input_copy",
            mime="video/mp4",
        ),
        "missing": SelectedMaterial(
            resource_id="missing",
            path=source / "does-not-exist.json",
            relative="projects/task-demo/v1/missing.json",
            kind="diagnostics",
            mime="application/json",
        ),
    }
    return root, selected


def test_package_contains_only_selected_redacted_materials(
    env: tuple[Path, dict[str, SelectedMaterial]], registry: ContractRegistry, tmp_path: Path
) -> None:
    root, selected = env
    outcome = build_diagnostic_package(
        package_id="pkg-1",
        root=root,
        relative_dir="projects/task-demo/v1/export",
        task_id="task-demo",
        version=1,
        selected=tuple(selected.values()),
    )
    result = outcome.result
    assert result["redacted"] is True and result["simulation"] is True
    # original video and missing file are ledgered but NOT attached
    included_ids = {r["resource_id"] for r in result["resources"]}
    assert included_ids == {"diag", "frame"}
    ledger = {item["resource_id"]: item for item in outcome.items}
    assert ledger["original"]["status"] == "refused"
    assert ledger["original"]["reason"] == "original_video_excluded"
    assert ledger["missing"]["status"] == "failed"
    assert ledger["missing"]["reason"] == "source_file_missing"
    # the package directory never contains the original video
    packaged = list((root / "projects/task-demo/v1/export/pkg-1").rglob("*"))
    assert all("original" not in p.name for p in packaged)
    # the frozen export_result contract holds as returned
    assert registry.validate(registry.definition_ref("deletion", "export_result"), result) == ()


def test_json_diagnostics_are_redacted(env: tuple[Path, dict[str, SelectedMaterial]]) -> None:
    root, selected = env
    build_diagnostic_package(
        package_id="pkg-2",
        root=root,
        relative_dir="projects/task-demo/v1/export",
        task_id="task-demo",
        version=1,
        selected=(selected["diag"],),
    )
    packaged = root / "projects/task-demo/v1/export/pkg-2/files/diag-diagnostics.json"
    text = packaged.read_text(encoding="utf-8")
    assert "王小明" not in text
    assert "ming@example.com" not in text
    assert "教练你好" not in text
    assert "/Users/vigo" not in text
    assert "[REDACTED]" in text
    assert "[REDACTED_PATH]" in text
    # non-sensitive content survives
    assert "preparation-order" in text
    payload = json.loads(text)
    assert payload["state_history"] == ["draft", "analyzing"]


def test_name_variants_and_youth_identity_are_redacted(
    tmp_path: Path, registry: ContractRegistry
) -> None:
    """BE-024 review round 1 found real_name/nickname and youth fields
    leaking past the vocabulary; these regressions stay closed."""
    root = tmp_path / "public2"
    root.mkdir()
    source = tmp_path / "source2"
    source.mkdir()
    diagnostics = {
        "user": {
            "real_name": "李大明",
            "nickname": "大明",
            "player_name": "Player Daming",
            "age_band": "youth",
            "guardian_simulated": True,
        },
        "nested": {"profile": {"display_name": "DM", "city": "上海"}},
        "note": "普通诊断内容保留",
    }
    diagnostics_file = source / "diag2.json"
    diagnostics_file.write_text(json.dumps(diagnostics, ensure_ascii=False), encoding="utf-8")
    outcome = build_diagnostic_package(
        package_id="pkg-youth",
        root=root,
        relative_dir="projects/task-demo/v1/export",
        task_id="task-demo",
        version=1,
        selected=(
            SelectedMaterial(
                resource_id="diag2",
                path=diagnostics_file,
                relative="projects/task-demo/v1/diag2.json",
                kind="diagnostics",
                mime="application/json",
            ),
        ),
    )
    assert outcome.result["redacted"] is True
    packaged = root / "projects/task-demo/v1/export/pkg-youth/files/diag2-diag2.json"
    text = packaged.read_text(encoding="utf-8")
    assert "李大明" not in text
    assert "大明" not in text
    assert "Player Daming" not in text
    assert "DM" not in text  # display_name carries the *name* pattern
    assert "youth" not in text  # age_band value removed with the identity context
    assert "上海" in text and "普通诊断内容保留" in text  # non-sensitive survives
    manifest = json.loads((root / outcome.result["manifest"]["path"]).read_text(encoding="utf-8"))
    assert manifest["redacted"] is True


def test_space_and_non_ascii_paths_fully_redacted(tmp_path: Path) -> None:
    """BE-024 review round 1: the old pattern truncated at the first space
    and leaked filename segments like '/ Vidéos/正手.mp4'."""
    from tennis_backend.export.package import _redact_text

    redacted = _redact_text("video at /Users/vigo/Private/ Vidéos/正手.mp4 kept")
    assert "/Users" not in redacted
    assert "Vidéos" not in redacted
    assert "正手.mp4" not in redacted
    assert "kept" in redacted


def test_manifest_is_complete_and_hashes_match(
    env: tuple[Path, dict[str, SelectedMaterial]], registry: ContractRegistry
) -> None:
    root, selected = env
    outcome = build_diagnostic_package(
        package_id="pkg-3",
        root=root,
        relative_dir="projects/task-demo/v1/export",
        task_id="task-demo",
        version=1,
        selected=(selected["diag"], selected["frame"]),
    )
    manifest_file = root / result_manifest_path(outcome)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest["redacted"] is True
    assert manifest["package_id"] == "pkg-3"
    included = [i for i in manifest["items"] if i["status"] == "included"]
    assert len(included) == 2
    for item in included:
        file = root / item["path"]
        assert file.exists()
        assert hashlib.sha256(file.read_bytes()).hexdigest() == item["sha256"]
        assert item["size_bytes"] == file.stat().st_size
    # the manifest resource hash matches the manifest file itself
    manifest_resource = outcome.result["manifest"]
    assert hashlib.sha256(manifest_file.read_bytes()).hexdigest() == manifest_resource["sha256"]
    # and the manifest resource is a valid common:resource shape
    assert registry.validate(registry.definition_ref("common", "resource"), manifest_resource) == ()


def result_manifest_path(outcome: Any) -> str:
    return outcome.result["manifest"]["path"]


def test_empty_selection_refused(tmp_path: Path) -> None:
    from tennis_backend.domain.models import DomainViolation

    root = tmp_path / "public"
    root.mkdir()
    with pytest.raises(DomainViolation) as error:
        build_diagnostic_package(
            package_id="pkg-4",
            root=root,
            relative_dir="x",
            task_id="t",
            version=1,
            selected=(),
        )
    assert error.value.code == "DOMAIN_EXPORT_EMPTY"


def test_no_send_capability_in_module() -> None:
    from tennis_backend.export import package

    source = inspect.getsource(package)
    for banned in ("smtplib", "requests", "urllib.request", "http.client", "socket"):
        assert banned not in source, f"send capability leaked: {banned}"
    public_api = [name for name in dir(package) if not name.startswith("_")]
    assert not any(word in " ".join(public_api).lower() for word in ("send", "mail", "post"))
