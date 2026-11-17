"""Read-only runtime health tests (BE-009).

The cases cover the frozen ``runtime:result`` shape, the stable check codes,
ready-only-when-verified, missing-binary and bad-version probes, missing or
unwritable roots, mocked probe failures, and the read-only guarantee: repeated
health checks return the same result and create or modify nothing. Real
subprocess probes run only against the actual environment as a smoke test, so
availability itself is never asserted. No install, download, network or shell
is involved.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.contracts.validation import RuntimeResult
from tennis_backend.runtime import health
from tennis_backend.runtime.health import (
    CHECK_CONTRACT_VERSION,
    CHECK_PACKAGE_VERSION,
    CHECK_PYTHON_VERSION,
    HealthConfig,
    missing_checks,
    missing_weights,
    runtime_health,
)

FFMPEG_OK = "ffmpeg version 6.1.1 Copyright\nbuilt with gcc\n"
FFPROBE_OK = "ffprobe version 6.1.1 Copyright\nbuilt with gcc\n"
FFMPEG_OLD = "ffmpeg version 3.2.4 Copyright\n"
FFPROBE_OLD = "ffprobe version 3.2.4 Copyright\n"


def runner_with(stdout_ffmpeg: str, stdout_ffprobe: str) -> Any:
    """A probe runner standing in for installs reporting these version lines."""

    def run(argv: Any) -> subprocess.CompletedProcess[str]:
        stdout = stdout_ffmpeg if "ffmpeg" in str(argv[0]) else stdout_ffprobe
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout=stdout, stderr="")

    return run


def ok_runner(argv: Any) -> subprocess.CompletedProcess[str]:
    return runner_with(FFMPEG_OK, FFPROBE_OK)(argv)


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """Real authorized roots; the health layer itself never creates them."""
    public = tmp_path / "public-workspace"
    internal = tmp_path / "internal-store"
    public.mkdir()
    internal.mkdir()
    return public, internal


@pytest.fixture
def config(roots: tuple[Path, Path]) -> HealthConfig:
    public, internal = roots
    return HealthConfig(
        backend_version="0.1.0",
        public_root=public,
        internal_root=internal,
    )


def failing_runner(_: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")


def missing_runner(_: Any) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError("binary not installed")


def timeout_runner(_: Any) -> subprocess.CompletedProcess[str]:
    raise subprocess.TimeoutExpired(cmd="ffmpeg -version", timeout=5.0)


def codes(result: RuntimeResult) -> dict[str, bool]:
    return {check.code: check.passed for check in result.checks}


# ---------------------------------------------------------------------------
# Shape, stable codes and the frozen runtime:result compatibility
# ---------------------------------------------------------------------------


def test_result_follows_the_frozen_runtime_result_shape(config: HealthConfig) -> None:
    result = runtime_health(config, runner=ok_runner)
    assert isinstance(result, RuntimeResult)
    assert set(result.model_dump()) == {
        "state",
        "version",
        "backend",
        "checks",
        "retained",
        "simulation",
    }
    assert result.backend == "python"
    assert result.simulation is False
    assert result.version == "0.1.0"
    assert result.retained == []
    assert len(result.checks) <= 200


def test_stable_check_codes_are_always_reported(config: HealthConfig) -> None:
    result = runtime_health(config, runner=missing_runner)
    reported = set(codes(result))
    assert {
        CHECK_PACKAGE_VERSION,
        CHECK_CONTRACT_VERSION,
        CHECK_PYTHON_VERSION,
        "public_root_exists",
        "public_root_writable",
        "public_root_space_free",
        "internal_root_exists",
        "internal_root_writable",
        "internal_root_space_free",
        "ffmpeg_available",
        "ffmpeg_version_supported",
        "ffprobe_available",
        "ffprobe_version_supported",
    } <= reported


def test_all_checks_passing_reports_ready(config: HealthConfig) -> None:
    result = runtime_health(config, runner=ok_runner)
    assert result.state == "ready"
    assert missing_checks(result) == ()
    assert all(check.passed for check in result.checks)


def test_ready_is_only_reported_when_every_check_is_verified(config: HealthConfig) -> None:
    result = runtime_health(config, runner=runner_with(FFMPEG_OLD, FFPROBE_OK))
    assert result.state == "repair_required"
    assert missing_checks(result) == ("ffmpeg_version_supported",)
    assert codes(result)["ffmpeg_available"] is True
    assert codes(result)["ffmpeg_version_supported"] is False


# ---------------------------------------------------------------------------
# Tool probes: missing binaries, bad versions, probe errors
# ---------------------------------------------------------------------------


def test_missing_binary_reports_unavailable_and_unsupported(config: HealthConfig) -> None:
    result = runtime_health(config, runner=missing_runner)
    reported = codes(result)
    assert reported["ffmpeg_available"] is False
    assert reported["ffmpeg_version_supported"] is False
    assert reported["ffprobe_available"] is False
    assert reported["ffprobe_version_supported"] is False
    assert result.state == "repair_required"
    assert set(missing_checks(result)) >= {"ffmpeg_available", "ffprobe_available"}


def test_failing_probe_is_reported_without_raising(config: HealthConfig) -> None:
    result = runtime_health(config, runner=failing_runner)
    assert codes(result)["ffmpeg_available"] is False
    assert result.state == "repair_required"


def test_probe_timeout_is_treated_like_a_missing_tool(config: HealthConfig) -> None:
    result = runtime_health(config, runner=timeout_runner)
    assert set(missing_checks(result)) >= {"ffmpeg_available", "ffprobe_available"}


def test_bad_tool_versions_fail_only_the_version_check(config: HealthConfig) -> None:
    result = runtime_health(config, runner=runner_with(FFMPEG_OLD, FFPROBE_OLD))
    reported = codes(result)
    assert reported["ffmpeg_available"] is True
    assert reported["ffmpeg_version_supported"] is False
    assert reported["ffprobe_available"] is True
    assert reported["ffprobe_version_supported"] is False
    assert missing_checks(result) == (
        "ffmpeg_version_supported",
        "ffprobe_version_supported",
    )


def test_unparsable_version_output_is_rejected(config: HealthConfig) -> None:
    result = runtime_health(
        config, runner=runner_with("ffmpeg something odd\n", "ffprobe version 6.1\n")
    )
    assert codes(result)["ffmpeg_version_supported"] is False
    assert codes(result)["ffprobe_version_supported"] is True


def test_probe_argv_has_no_shell_metacharacters(config: HealthConfig) -> None:
    captured: list[list[str]] = []

    def recording_runner(argv: Any) -> subprocess.CompletedProcess[str]:
        captured.append(list(argv))
        return ok_runner(argv)

    runtime_health(config, runner=recording_runner)
    assert captured == [["ffmpeg", "-version"], ["ffprobe", "-version"]]


# ---------------------------------------------------------------------------
# Roots: existence, permissions, space
# ---------------------------------------------------------------------------


def test_missing_root_fails_its_checks_without_creating_it(config: HealthConfig) -> None:
    missing = HealthConfig(
        backend_version="0.1.0",
        public_root=config.public_root / "not-created",
        internal_root=config.internal_root,
    )
    result = runtime_health(missing, runner=ok_runner)
    reported = codes(result)
    assert reported["public_root_exists"] is False
    assert reported["public_root_writable"] is False
    assert reported["public_root_space_free"] is False
    assert not (config.public_root / "not-created").exists()
    assert result.state == "repair_required"


def test_unwritable_root_fails_the_permission_check(
    config: HealthConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health.os, "access", lambda *_args, **_: False)
    result = runtime_health(config, runner=ok_runner)
    reported = codes(result)
    assert reported["public_root_writable"] is False
    assert reported["internal_root_writable"] is False
    assert reported["public_root_exists"] is True


def test_low_free_space_fails_the_space_check(
    config: HealthConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections import namedtuple

    tight_disk = namedtuple("usage", "total used free")(1024**4, 1024**4, 1024)
    monkeypatch.setattr(health.shutil, "disk_usage", lambda _path: tight_disk)
    tight = HealthConfig(
        backend_version="0.1.0",
        public_root=config.public_root,
        internal_root=config.internal_root,
        minimum_free_bytes=10 * 1024 * 1024,
    )
    result = runtime_health(tight, runner=ok_runner)
    reported = codes(result)
    assert reported["public_root_space_free"] is False
    assert reported["internal_root_space_free"] is False
    assert result.state == "repair_required"


def test_disk_usage_errors_are_treated_as_failed_space(
    config: HealthConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_: Any) -> None:
        raise OSError("statfs failed")

    monkeypatch.setattr(health.shutil, "disk_usage", broken)
    result = runtime_health(config, runner=ok_runner)
    assert missing_checks(result) == ("public_root_space_free", "internal_root_space_free")


# ---------------------------------------------------------------------------
# Versions: package, contract, Python
# ---------------------------------------------------------------------------


def test_bad_contract_version_fails_its_check(config: HealthConfig) -> None:
    drifted = HealthConfig(
        backend_version="0.1.0",
        public_root=config.public_root,
        internal_root=config.internal_root,
        contract_version="2.0.0",
    )
    result = runtime_health(drifted, runner=ok_runner)
    assert codes(result)[CHECK_CONTRACT_VERSION] is False
    assert result.state == "repair_required"


@pytest.mark.parametrize("backend_version", ["", "x" * 33])
def test_bad_package_version_clears_the_version_slot(
    config: HealthConfig, backend_version: str
) -> None:
    drifted = HealthConfig(
        backend_version=backend_version,
        public_root=config.public_root,
        internal_root=config.internal_root,
    )
    result = runtime_health(drifted, runner=ok_runner)
    assert codes(result)[CHECK_PACKAGE_VERSION] is False
    assert result.version is None


def test_unsupported_python_version_fails_its_check(
    config: HealthConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health.sys, "version_info", (3, 10, 9))
    result = runtime_health(config, runner=ok_runner)
    assert codes(result)[CHECK_PYTHON_VERSION] is False
    assert result.state == "repair_required"


def test_supported_python_version_passes(
    config: HealthConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health.sys, "version_info", (3, 11, 5))
    result = runtime_health(config, runner=ok_runner)
    assert codes(result)[CHECK_PYTHON_VERSION] is True
    assert result.state == "ready"


# ---------------------------------------------------------------------------
# Read-only guarantee: repeated checks change nothing
# ---------------------------------------------------------------------------


def test_repeated_health_checks_are_stable_and_change_nothing(
    config: HealthConfig, roots: tuple[Path, Path]
) -> None:
    public, internal = roots
    snapshot_before = sorted(
        (path.name, path.stat().st_mtime_ns) for path in public.rglob("*")
    ) + sorted((path.name, path.stat().st_mtime_ns) for path in internal.rglob("*"))
    first = runtime_health(config, runner=ok_runner)
    second = runtime_health(config, runner=missing_runner)
    assert first.model_dump() == runtime_health(config, runner=ok_runner).model_dump()
    snapshot_after = sorted(
        (path.name, path.stat().st_mtime_ns) for path in public.rglob("*")
    ) + sorted((path.name, path.stat().st_mtime_ns) for path in internal.rglob("*"))
    assert snapshot_before == snapshot_after
    assert second.state == "repair_required"


def test_result_carries_no_private_root_paths(
    config: HealthConfig, roots: tuple[Path, Path]
) -> None:
    public, internal = roots
    result = runtime_health(config, runner=missing_runner)
    encoded = json.dumps(result.model_dump())
    assert str(public) not in encoded
    assert str(internal) not in encoded
    assert str(public.name) not in encoded
    assert str(internal.name) not in encoded


# ---------------------------------------------------------------------------
# Real read-only smoke against the actual environment
# ---------------------------------------------------------------------------


def test_real_environment_smoke_is_read_only_and_valid(config: HealthConfig) -> None:
    """No injected runner: real subprocess probes, read-only, valid result.

    Availability itself is environment-dependent and never asserted here; only
    the shape, the flags and the state rules are.
    """
    result = runtime_health(config)
    assert result.backend == "python"
    assert result.simulation is False
    assert result.state in {"ready", "repair_required"}
    assert (result.state == "ready") == all(check.passed for check in result.checks)
    assert all(len(check.code) <= 128 for check in result.checks)
    again = runtime_health(config)
    assert again.model_dump() == result.model_dump()


# ----------------------------------------------------------------------
# model weights check (opt-in: weight_paths empty keeps the frozen 13 checks)
# ----------------------------------------------------------------------


def _ready_config(tmp_path: Path, weights: tuple[Path, ...]) -> HealthConfig:
    public, internal = tmp_path / "public", tmp_path / "internal"
    public.mkdir()
    internal.mkdir()
    return HealthConfig(
        backend_version="0.1.0",
        public_root=public,
        internal_root=internal,
        weight_paths=weights,
    )


_fake_tools = runner_with(FFMPEG_OK, FFPROBE_OK)


def test_default_health_has_no_weight_check(tmp_path: Path) -> None:
    result = runtime_health(_ready_config(tmp_path, ()), runner=_fake_tools)
    assert all(check.code != "model_weights_ready" for check in result.checks)


def test_a_present_weight_passes(tmp_path: Path) -> None:
    weight = tmp_path / "w.pth"
    weight.write_bytes(b"PK\x03\x04" + b"0" * 100)
    result = runtime_health(_ready_config(tmp_path, (weight,)), runner=_fake_tools)
    assert result.state == "ready"
    assert any(c.code == "model_weights_ready" and c.passed for c in result.checks)


def test_a_missing_weight_makes_the_runtime_repair_required(tmp_path: Path) -> None:
    result = runtime_health(_ready_config(tmp_path, (tmp_path / "no.pth",)), runner=_fake_tools)
    assert result.state == "repair_required"
    assert any(c.code == "model_weights_ready" and not c.passed for c in result.checks)
    assert missing_weights((tmp_path / "no.pth",)) == ["no.pth"]


def test_an_lfs_pointer_is_not_a_usable_weight(tmp_path: Path) -> None:
    pointer = tmp_path / "w.pth"
    pointer.write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:x\nsize 1\n")
    result = runtime_health(_ready_config(tmp_path, (pointer,)), runner=_fake_tools)
    assert result.state == "repair_required"
    assert missing_weights((pointer,)) == ["w.pth"]
