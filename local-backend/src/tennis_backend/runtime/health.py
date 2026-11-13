"""Read-only local capability health checks (BE-009).

One reusable function, :func:`runtime_health`, verifies the Python backend's
own ability to run: package version, frozen contract version, Python version,
the two authorized roots (existence, permission, free space) and FFmpeg/ffprobe
availability and version. It returns the frozen ``runtime:result`` shape
(``tennis_backend.contracts.validation.RuntimeResult``) so the later transport
batch can send it directly for ``runtime.status``; ``backend`` is always
``python`` and ``simulation`` is always ``False`` because these are real
process checks, unlike fixture analysis outputs.

Strictly read-only: no install, download, network, mkdir, chmod or any other
system change, and nothing is executed through a shell - tool probes run a
fixed argv (``<tool> -version``) with a bounded timeout. Actual prepare, start,
repair and uninstall stay owned by the Node Bridge; this layer only verifies
and reports. All checks passed means ``ready``; any failed check means
``repair_required`` (spec §8.1 ``verifying``). Failed check codes are the
missing items, available through :func:`missing_checks`.

Check codes are stable identifiers; no absolute root paths, media or credential
material is placed in the result or in ordinary logs, and nothing here logs.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tennis_backend.contracts.validation import (
    CONTRACT_VERSION,
    RuntimeCheck,
    RuntimeResult,
    RuntimeState,
)

#: The frozen shared contract version this backend implements.
_FROZEN_CONTRACT_VERSION = "1.1.0"

#: Minimum Python and tool versions this prototype needs.
_MINIMUM_PYTHON = (3, 11)
_MINIMUM_TOOL = (4, 0)

#: Bounded probe duration; probes never hang the health check.
_PROBE_TIMEOUT_SECONDS = 5.0

#: Free space a workspace root should offer before analysis runs.
_DEFAULT_MINIMUM_FREE_BYTES = 64 * 1024 * 1024

_TOOL_VERSION = re.compile(r"version\s+(\d+)\.(\d+)")

#: Callers may inject a probe runner; the default runs a real subprocess.
ProbeRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

#: Stable check codes reported by :func:`runtime_health`.
CHECK_PACKAGE_VERSION = "package_version_present"
CHECK_CONTRACT_VERSION = "contract_version_1_0_0"
CHECK_PYTHON_VERSION = "python_version_3_11_plus"


@dataclass(frozen=True)
class HealthConfig:
    """What to check and where; roots are never created or modified."""

    backend_version: str
    public_root: Path
    internal_root: Path
    contract_version: str = CONTRACT_VERSION
    minimum_free_bytes: int = _DEFAULT_MINIMUM_FREE_BYTES
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one fixed argv probe without a shell and with a bounded timeout."""
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_SECONDS,
        check=False,
        shell=False,
    )


def _probe_tool(
    runner: ProbeRunner, argv: Sequence[str], tool: str, minimum: tuple[int, int]
) -> tuple[bool, bool]:
    """Return ``(available, version_supported)`` for one external tool."""
    try:
        outcome = runner(argv)
    except (OSError, subprocess.SubprocessError):
        return False, False
    first_line = outcome.stdout.splitlines()[0] if outcome.stdout else ""
    if outcome.returncode != 0 or not first_line.startswith(f"{tool} version"):
        return False, False
    match = _TOOL_VERSION.search(first_line)
    if match is None:
        return True, False
    return True, (int(match.group(1)), int(match.group(2))) >= minimum


def _root_checks(config: HealthConfig, label: str, root: Path) -> list[tuple[str, bool]]:
    """Existence, permission and space checks for one authorized root."""
    exists = root.is_dir()
    writable = exists and os.access(root, os.W_OK)
    try:
        enough_space = exists and shutil.disk_usage(root).free >= config.minimum_free_bytes
    except OSError:
        enough_space = False
    return [
        (f"{label}_root_exists", exists),
        (f"{label}_root_writable", writable),
        (f"{label}_root_space_free", enough_space),
    ]


def runtime_health(config: HealthConfig, *, runner: ProbeRunner | None = None) -> RuntimeResult:
    """Verify the backend's own runtime ability, changing nothing.

    The result follows the frozen ``runtime:result`` shape; ``state`` is
    ``ready`` only when every check passed, otherwise ``repair_required``.
    """
    probe = runner or _default_runner

    package_version_present = 0 < len(config.backend_version) <= 32
    contract_version_ok = config.contract_version == _FROZEN_CONTRACT_VERSION
    python_version_ok = sys.version_info >= _MINIMUM_PYTHON

    ffmpeg_available, ffmpeg_version = _probe_tool(
        probe, (config.ffmpeg, "-version"), "ffmpeg", _MINIMUM_TOOL
    )
    ffprobe_available, ffprobe_version = _probe_tool(
        probe, (config.ffprobe, "-version"), "ffprobe", _MINIMUM_TOOL
    )

    outcomes: list[tuple[str, bool]] = [
        (CHECK_PACKAGE_VERSION, package_version_present),
        (CHECK_CONTRACT_VERSION, contract_version_ok),
        (CHECK_PYTHON_VERSION, python_version_ok),
        *_root_checks(config, "public", config.public_root),
        *_root_checks(config, "internal", config.internal_root),
        ("ffmpeg_available", ffmpeg_available),
        ("ffmpeg_version_supported", ffmpeg_version),
        ("ffprobe_available", ffprobe_available),
        ("ffprobe_version_supported", ffprobe_version),
    ]
    checks = [RuntimeCheck(code=code, passed=passed) for code, passed in outcomes]
    state: RuntimeState = "ready" if all(passed for _, passed in outcomes) else "repair_required"
    return RuntimeResult(
        state=state,
        version=config.backend_version if package_version_present else None,
        backend="python",
        checks=checks,
        retained=[],
        simulation=False,
    )


def missing_checks(result: RuntimeResult) -> tuple[str, ...]:
    """The stable codes of the failed checks, i.e. the missing items."""
    return tuple(check.code for check in result.checks if not check.passed)


__all__ = [
    "CHECK_CONTRACT_VERSION",
    "CHECK_PACKAGE_VERSION",
    "CHECK_PYTHON_VERSION",
    "HealthConfig",
    "ProbeRunner",
    "missing_checks",
    "runtime_health",
]
