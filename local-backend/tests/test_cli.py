"""CLI stdio wiring tests (BE-018).

Real behavior is proven through a real ``tennis-backend --stdio`` subprocess:
frozen-contract requests go in on stdin, frozen responses come out on stdout,
with the data root placed in a temp directory and the process started from a
different cwd (controlled locating). Covers health (simulation=false),
minimal create/status (no analysis on start or create), stable error codes,
recovery after a bad line, and the controlled-root refusal.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.contracts.validation import ContractRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "local-backend" / "src"
CONTRACTS_DIR = REPO_ROOT / "contracts" / "v1"


class BackendCli:
    """A real backend subprocess serving the NDJSON protocol."""

    def __init__(
        self, tmp_path: Path, *, cwd: Path | None = None, env_extra: dict[str, str] | None = None
    ) -> None:
        env = os.environ.copy()
        env["TENNIS_DATA_ROOT"] = str(tmp_path / "data")
        env["PYTHONPATH"] = str(SRC_DIR)
        env.update(env_extra or {})
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "tennis_backend.cli", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(cwd) if cwd else str(tmp_path),  # never the repo cwd
        )
        self._lines: queue.Queue[bytes] = queue.Queue()
        self._stderr = ""
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self.data_root = tmp_path / "data"

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            self._lines.put(raw)

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        self._stderr = self.proc.stderr.read().decode("utf-8", "replace")

    def send(self, payload: dict[str, Any]) -> None:
        stdin = self.proc.stdin
        assert stdin is not None
        stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        stdin.flush()

    def response(self, timeout: float = 15.0) -> dict[str, Any]:
        raw = self._lines.get(timeout=timeout)
        return json.loads(raw.decode("utf-8"))

    def close(self, timeout: float = 15.0) -> int:
        stdin = self.proc.stdin
        if stdin is not None and not stdin.closed:
            try:
                stdin.close()
            except OSError:
                pass
        return self.proc.wait(timeout=timeout)

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)

    @property
    def stderr_text(self) -> str:
        return self._stderr


@pytest.fixture
def backend(tmp_path: Path) -> Any:
    proc = BackendCli(tmp_path)
    try:
        yield proc
    finally:
        proc.kill()


def envelope(
    request_id: str,
    method: str,
    params: dict[str, Any],
    *,
    operation_id: str | None = None,
    simulation: bool = True,
) -> dict[str, Any]:
    return {
        "contract_version": "2.0.0",
        "request_id": request_id,
        "operation_id": operation_id,
        "method": method,
        "params": params,
        "simulation": simulation,
    }


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
            "purposes": ["analysis"],
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


# ---------------------------------------------------------------------------
# Real subprocess round-trips
# ---------------------------------------------------------------------------


def test_health_is_real_and_not_simulated(backend: BackendCli) -> None:
    backend.send(
        envelope(
            "req-h1", "runtime.status", {"runtime_requirement": "not_required"}, simulation=False
        )
    )
    payload = backend.response()
    assert payload["request_id"] == "req-h1"
    assert payload["ok"] is True
    assert payload["simulation"] is False
    result = payload["result"]
    assert result["backend"] == "python"
    assert result["simulation"] is False
    assert result["state"] in ("ready", "repair_required")
    codes = {check["code"] for check in result["checks"]}
    assert "package_version_present" in codes


def test_health_with_simulated_envelope_is_refused_loudly(backend: BackendCli) -> None:
    # real health results are never simulated: a simulation=true envelope
    # collides with the frozen consistency check and must fail loudly
    backend.send(
        envelope(
            "req-h2", "runtime.status", {"runtime_requirement": "not_required"}, simulation=True
        )
    )
    payload = backend.response()
    assert payload["request_id"] == "req-h2"
    assert payload["ok"] is False
    assert payload["error"]["code"] == "RESPONSE_INCONSISTENT"
    backend.send(
        envelope(
            "req-h3", "runtime.status", {"runtime_requirement": "not_required"}, simulation=False
        )
    )
    after = backend.response()
    assert after["ok"] is True  # the process keeps serving


def test_create_and_status_roundtrip_with_no_analysis(backend: BackendCli) -> None:
    backend.send(
        envelope(
            "req-c1",
            "analysis.create",
            create_params("op-cli-1"),
            operation_id="op-cli-1",
        )
    )
    created = backend.response()
    assert created["request_id"] == "req-c1"
    assert created["ok"] is True
    summary = created["result"]
    assert summary["state"] == "draft"  # create never starts analysis
    assert summary["stage"] == "input"
    assert summary["checkpoints"] == []
    assert summary["simulation"] is True

    backend.send(
        envelope("req-s1", "analysis.status", {"task_id": summary["task_id"], "version": 1})
    )
    looked = backend.response()
    assert looked["request_id"] == "req-s1"
    assert looked["ok"] is True
    assert looked["result"]["task_id"] == summary["task_id"]
    assert looked["result"]["state"] == "draft"

    # data lives inside the controlled root only
    assert (backend.data_root / "internal" / "metadata.sqlite").exists()
    assert (backend.data_root / "public").is_dir()


def test_create_is_idempotent_across_restarts(backend: BackendCli) -> None:
    backend.send(
        envelope(
            "req-c1",
            "analysis.create",
            create_params("op-cli-idem"),
            operation_id="op-cli-idem",
        )
    )
    first = backend.response()["result"]
    backend.close()
    assert backend.proc.returncode == 0

    again = BackendCli(backend.data_root.parent, cwd=backend.data_root.parent)
    try:
        again.send(
            envelope(
                "req-c2",
                "analysis.create",
                create_params("op-cli-idem"),
                operation_id="op-cli-idem",
            )
        )
        replay = again.response()
        assert replay["ok"] is True
        assert replay["result"]["task_id"] == first["task_id"]
    finally:
        again.kill()


def test_stable_error_codes_keeps_serving(backend: BackendCli) -> None:
    backend.send(
        envelope(
            "req-uf",
            "analysis.create",
            create_params("op-cli-uf", fixture_id="no-such-fixture"),
            operation_id="op-cli-uf",
        )
    )
    unknown_fixture = backend.response()
    assert unknown_fixture["ok"] is False
    assert unknown_fixture["error"]["code"] == "DOMAIN_FIXTURE_UNKNOWN"

    backend.send(envelope("req-ut", "analysis.status", {"task_id": "task-missing", "version": 1}))
    unknown_task = backend.response()
    assert unknown_task["error"]["code"] == "DOMAIN_UNKNOWN_TASK"

    backend.send(
        envelope(
            "req-um",
            "runtime.prepare",
            {
                "operation_id": "op-x",
                "consent_confirmed": True,
                "target_version": "0.1.0",
                "intent": "prepare",
                "scope_digest": "digest-scope-0",
            },
            operation_id="op-x",
        )
    )
    unimplemented = backend.response()
    # runtime install flows are Bridge-owned (BE-009/BE-027): stable refusal
    assert unimplemented["error"]["code"] == "RUNTIME_BRIDGE_OWNED"

    backend.send(
        envelope(
            "req-ok", "runtime.status", {"runtime_requirement": "not_required"}, simulation=False
        )
    )
    after = backend.response()
    assert after["request_id"] == "req-ok" and after["ok"] is True  # still serving


def test_invalid_line_recovers(backend: BackendCli) -> None:
    stdin = backend.proc.stdin
    assert stdin is not None
    stdin.write(b'{"request_id":"req-bad","contract_version":"9.9.9"}\n')
    stdin.flush()
    bad = backend.response()
    assert bad["ok"] is False
    assert bad["error"]["code"] == "REQUEST_ENVELOPE_INVALID"
    backend.send(
        envelope(
            "req-good", "runtime.status", {"runtime_requirement": "not_required"}, simulation=False
        )
    )
    good = backend.response()
    assert good["request_id"] == "req-good" and good["ok"] is True


def test_contract_registry_still_validates_summary_shape(backend: BackendCli) -> None:
    backend.send(
        envelope(
            "req-cv",
            "analysis.create",
            create_params("op-cli-cv"),
            operation_id="op-cli-cv",
        )
    )
    summary = backend.response()["result"]
    registry = ContractRegistry.load(CONTRACTS_DIR)
    assert registry.validate(registry.definition_ref("task", "summary"), summary) == ()


# ---------------------------------------------------------------------------
# Controlled locating
# ---------------------------------------------------------------------------


def test_stdio_refuses_without_data_root(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("TENNIS_DATA_ROOT", None)
    env["PYTHONPATH"] = str(SRC_DIR)
    proc = subprocess.Popen(
        [sys.executable, "-m", "tennis_backend.cli", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
    )
    stdout = proc.stdout.read() if proc.stdout else b""
    stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    code = proc.wait(timeout=15)
    assert code == 2
    assert stdout == b""  # no protocol lines without an authorized root
    assert "TENNIS_DATA_ROOT" in stderr


def test_cli_help_and_version_unchanged(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    version = subprocess.run(
        [sys.executable, "-m", "tennis_backend.cli", "--version"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        check=True,
    )
    assert "tennis-backend" in version.stdout
    assert "contract 2.0.0" in version.stdout
