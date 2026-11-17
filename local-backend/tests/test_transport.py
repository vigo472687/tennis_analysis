"""Transport tests for the NDJSON protocol entry (BE-010).

Real behavior is proven through real subprocesses: each backend under test is
a fresh ``python -c`` process that loads the frozen contracts, injects a
behavior-controlled handler and serves actual stdin/stdout pipes. In-process
cases only cover parsing/shutdown details that the shared code path already
exercises. All payloads are synthetic fixture-shaped data; no media, network
or user content is involved.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.transport.server import EXIT_IO_ERROR, EXIT_OK, NdjsonTransport

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "local-backend" / "src"
CONTRACTS_DIR = REPO_ROOT / "contracts" / "v1"

SUMMARY_RESULT: dict[str, Any] = {
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

RUNTIME_RESULT: dict[str, Any] = {
    "state": "ready",
    "version": "0.1.0",
    "backend": "python",
    "checks": [],
    "retained": [],
    "simulation": True,
}

# Injected handler profile for the subprocess under test. analysis.create
# counts one invocation into the counter file, blocks on the release file
# (slow analysis) and hosts deliberate contract/exception fault injections.
_BOOTSTRAP = r"""
import json, os, sys, time
from pathlib import Path

from tennis_backend.contracts.validation import ContractRegistry
from tennis_backend.transport.server import NdjsonTransport

SUMMARY = json.loads(sys.argv[2])
RUNTIME = json.loads(sys.argv[3])
RELEASE = os.environ["TB_RELEASE_FILE"]
COUNTER = os.environ["TB_COUNTER_FILE"]


def handler(request):
    method = request.method
    if method == "analysis.create":
        fixture = request.raw_params.get("fixture_id")
        if fixture == "fixture-boom":
            raise RuntimeError("secret internal detail")
        if fixture == "fixture-bad-result":
            return {"state": "nonsense"}
        if fixture == "fixture-wrong-sim":
            wrong = dict(SUMMARY)
            wrong["simulation"] = False
            return wrong
        media = request.raw_params.get("media_ref", "-")
        with open(COUNTER, "a", encoding="utf-8") as fh:
            fh.write("create\t" + media + "\n")
        deadline = time.time() + 20
        while not os.path.exists(RELEASE) and time.time() < deadline:
            time.sleep(0.01)
        return dict(SUMMARY)
    if method == "runtime.status":
        return dict(RUNTIME)
    if method == "analysis.status":
        return dict(SUMMARY)
    if method == "analysis.cancel":
        cancelled = dict(SUMMARY)
        cancelled["state"] = "cancelled"
        return cancelled
    raise AssertionError("unexpected method " + method)


sizes = {
    "TB_WORKERS": "max_workers",
    "TB_PENDING": "max_pending",
    "TB_CONTROL_PENDING": "max_control_pending",
}
options = {}
for env_name, arg_name in sizes.items():
    raw = os.environ.get(env_name)
    if raw:
        options[arg_name] = int(raw)

registry = ContractRegistry.load(Path(sys.argv[1]))
code = NdjsonTransport(registry, handler, **options).serve()
raise SystemExit(code)
"""


class BackendProcess:
    """A real backend subprocess with injected handler behaviors."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        workers: int | None = None,
        pending: int | None = None,
    ) -> None:
        self.release = tmp_path / "release"
        self.counter = tmp_path / "counter"
        env = os.environ.copy()
        env["TB_RELEASE_FILE"] = str(self.release)
        env["TB_COUNTER_FILE"] = str(self.counter)
        env["PYTHONPATH"] = str(SRC_DIR)
        if workers is not None:
            env["TB_WORKERS"] = str(workers)
        if pending is not None:
            env["TB_PENDING"] = str(pending)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _BOOTSTRAP,
                str(CONTRACTS_DIR),
                json.dumps(SUMMARY_RESULT),
                json.dumps(RUNTIME_RESULT),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(REPO_ROOT),
        )
        self.raw_lines: list[bytes] = []
        self._lines: queue.Queue[bytes] = queue.Queue()
        self._stderr_text = ""
        self._pump = threading.Thread(target=self._pump_stdout, daemon=True)
        self._pump.start()
        self._err_pump = threading.Thread(target=self._pump_stderr, daemon=True)
        self._err_pump.start()

    def _pump_stdout(self) -> None:
        stream = self.proc.stdout
        assert stream is not None
        for raw in stream:
            self.raw_lines.append(raw)
            self._lines.put(raw)

    def _pump_stderr(self) -> None:
        stream = self.proc.stderr
        assert stream is not None
        self._stderr_text = stream.read().decode("utf-8", "replace")

    def send_bytes(self, data: bytes) -> None:
        stdin = self.proc.stdin
        assert stdin is not None
        stdin.write(data)
        stdin.flush()

    def send(self, payload: dict[str, Any]) -> None:
        self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")

    def response(self, timeout: float = 10.0) -> dict[str, Any]:
        raw = self._lines.get(timeout=timeout)
        return json.loads(raw.decode("utf-8"))  # every stdout line must parse

    def release_now(self) -> None:
        self.release.write_text("go", encoding="utf-8")

    def counter_text(self) -> str:
        if self.counter.exists():
            return self.counter.read_text(encoding="utf-8")
        return ""

    @property
    def stderr_text(self) -> str:
        return self._stderr_text

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


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[BackendProcess]:
    """A fast backend: the release file exists, handlers complete at once."""
    proc = BackendProcess(tmp_path)
    proc.release_now()
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


def create_params(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operation_id": "op-create-1",
        "fixture_id": "fixture-flat-1",
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
    payload.update(overrides)
    return payload


def status_params(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"task_id": "task-demo", "version": 1}
    payload.update(overrides)
    return payload


def create_request(request_id: str, **overrides: Any) -> dict[str, Any]:
    params = create_params(**overrides)
    return envelope(request_id, "analysis.create", params, operation_id=params["operation_id"])


def wait_no_response(proc: BackendProcess, seconds: float = 0.4) -> None:
    with pytest.raises(queue.Empty):
        proc._lines.get(timeout=seconds)


# ---------------------------------------------------------------------------
# Real subprocess round-trips
# ---------------------------------------------------------------------------


def test_roundtrip_ids_correspond_and_stdout_is_protocol_only(backend: BackendProcess) -> None:
    backend.send(envelope("req-s1", "runtime.status", {"runtime_requirement": "not_required"}))
    backend.send(envelope("req-s2", "analysis.status", status_params()))
    first = backend.response()
    second = backend.response()
    assert first["request_id"] == "req-s1"
    assert second["request_id"] == "req-s2"
    for payload in (first, second):
        assert payload["contract_version"] == "2.0.0"
        assert payload["ok"] is True
        assert payload["error"] is None
        assert payload["simulation"] is True
    assert backend.close() == EXIT_OK
    assert backend.stderr_text == ""


def test_invalid_input_then_valid_request_recovers(backend: BackendProcess) -> None:
    backend.send_bytes(b"[1, 2]\n")
    backend.send_bytes(b"{oops\n")
    backend.send_bytes(
        b'{"request_id":"req-nan","contract_version":"2.0.0","operation_id":null,'
        b'"method":"runtime.status","params":{"runtime_requirement":"not_required"},'
        b'"simulation":NaN}\n'
    )
    backend.send_bytes(b'{"request_id":"req-u","method":"x",\xff"broken":1}\n')
    backend.send(envelope("req-ok", "runtime.status", {"runtime_requirement": "not_required"}))
    not_object = backend.response()
    bad_json_1 = backend.response()
    bad_json_2 = backend.response()
    bad_encoding = backend.response()
    good = backend.response()
    assert not_object["ok"] is False
    assert not_object["request_id"].startswith("transport-")
    assert not_object["error"]["code"] == "TRANSPORT_NOT_OBJECT"
    assert bad_json_1["error"]["code"] == "TRANSPORT_INVALID_JSON"
    assert bad_json_2["error"]["code"] == "TRANSPORT_INVALID_JSON"
    assert bad_encoding["error"]["code"] == "TRANSPORT_INVALID_ENCODING"
    for payload in (not_object, bad_json_1, bad_json_2, bad_encoding):
        assert payload["request_id"].startswith("transport-")
    assert good["request_id"] == "req-ok"
    assert good["ok"] is True
    stdout_bytes = b"".join(backend.raw_lines)
    assert b"oops" not in stdout_bytes
    assert b"\xff" not in stdout_bytes


def test_oversized_line_rejected_drained_then_recovery(backend: BackendProcess) -> None:
    oversized = b'{"x":"' + b"a" * (1048576 + 512) + b'"}\n'
    backend.send_bytes(oversized)
    backend.send(envelope("req-after", "runtime.status", {"runtime_requirement": "not_required"}))
    rejected = backend.response()
    assert rejected["ok"] is False
    assert rejected["request_id"].startswith("transport-")
    assert rejected["error"]["code"] == "TRANSPORT_LINE_TOO_LARGE"
    assert rejected["error"]["retryable"] is True
    after = backend.response()
    assert after["request_id"] == "req-after"
    assert after["ok"] is True


def test_eof_without_trailing_newline_still_served(tmp_path: Path) -> None:
    proc = BackendProcess(tmp_path)
    proc.release_now()
    try:
        proc.send_bytes(
            json.dumps(
                envelope("req-tail", "runtime.status", {"runtime_requirement": "not_required"})
            ).encode("utf-8")
        )
        exit_code = proc.close()
        payload = proc.response()
        assert payload["request_id"] == "req-tail"
        assert payload["ok"] is True
        assert exit_code == EXIT_OK
    finally:
        proc.kill()


def test_chunked_chinese_and_multiple_lines_per_write(backend: BackendProcess) -> None:
    line = (
        json.dumps(
            create_request("req-cn", media_ref="fixtures/media/正手击球.mp4"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )
    follow_up = (
        json.dumps(envelope("req-cn2", "analysis.status", status_params())).encode("utf-8") + b"\n"
    )
    cut1, cut2 = len(line) // 3, 2 * len(line) // 3
    backend.send_bytes(line[:cut1])
    backend.send_bytes(line[cut1:cut2])
    backend.send_bytes(line[cut2:] + follow_up)  # tail plus a full second line
    first = backend.response()
    second = backend.response()
    # Lanes answer concurrently: responses may complete out of order, so
    # match by request id instead of arrival order.
    by_id = {first["request_id"]: first, second["request_id"]: second}
    assert set(by_id) == {"req-cn", "req-cn2"}
    assert all(payload["ok"] is True for payload in by_id.values())
    assert "fixtures/media/正手击球.mp4" in backend.counter_text()


def test_contract_errors_keep_request_id_correspondence(backend: BackendProcess) -> None:
    backend.send(envelope("req-m1", "nope.nope", {}))
    bad_version = envelope("req-m2", "runtime.status", {"runtime_requirement": "not_required"})
    bad_version["contract_version"] = "1.0.1"
    backend.send(bad_version)
    backend.send(envelope("req-m3", "runtime.status", {"runtime_requirement": "bogus"}))
    unknown_method = backend.response()
    unknown_version = backend.response()
    bad_params = backend.response()
    assert unknown_method["request_id"] == "req-m1"
    assert unknown_method["error"]["code"] == "METHOD_UNKNOWN"
    assert unknown_version["request_id"] == "req-m2"
    assert unknown_version["error"]["code"] == "REQUEST_ENVELOPE_INVALID"
    assert bad_params["request_id"] == "req-m3"
    assert bad_params["error"]["code"] == "REQUEST_PARAMS_INVALID"


def test_handler_output_contract_mismatch_becomes_structured_error(
    backend: BackendProcess,
) -> None:
    backend.send(create_request("req-bad", fixture_id="fixture-bad-result"))
    backend.send(create_request("req-sim", fixture_id="fixture-wrong-sim"))
    backend.send(envelope("req-next", "runtime.status", {"runtime_requirement": "not_required"}))
    # Lanes answer concurrently: collect all three, then match by request id.
    by_id = {}
    for _ in range(3):
        payload = backend.response()
        by_id[payload["request_id"]] = payload
    invalid_result = by_id["req-bad"]
    wrong_sim = by_id["req-sim"]
    follower = by_id["req-next"]
    assert invalid_result["ok"] is False
    assert invalid_result["error"]["code"] == "RESULT_INVALID"
    assert wrong_sim["ok"] is False
    assert wrong_sim["error"]["code"] == "RESPONSE_INCONSISTENT"
    assert follower["ok"] is True
    for payload in (invalid_result, wrong_sim):
        assert payload["error"]["retained"] == []
        assert "nonsense" not in json.dumps(payload)


def test_handler_exception_is_sanitized(backend: BackendProcess) -> None:
    backend.send(create_request("req-boom", fixture_id="fixture-boom"))
    payload = backend.response()
    assert payload["request_id"] == "req-boom"
    assert payload["ok"] is False
    assert payload["error"]["code"] == "HANDLER_FAILED"
    assert payload["error"]["retryable"] is True
    assert payload["error"]["user_action"] == "retry"
    backend.close()  # let the process exit so the stderr pump sees EOF
    stdout_bytes = b"".join(backend.raw_lines)
    assert b"secret" not in stdout_bytes
    assert "secret" not in backend.stderr_text
    assert "RuntimeError" not in backend.stderr_text
    assert "rule=handler_failed" in backend.stderr_text
    assert "ref=req-boom" in backend.stderr_text


def test_slow_analysis_does_not_block_status_and_cancel(tmp_path: Path) -> None:
    proc = BackendProcess(tmp_path)  # no release file: analysis.create blocks
    try:
        proc.send(create_request("req-slow"))
        time.sleep(0.5)
        proc.send(envelope("req-st", "analysis.status", status_params()))
        proc.send(
            envelope(
                "req-cancel",
                "analysis.cancel",
                {"operation_id": "op-cancel-1", "task_id": "task-demo", "version": 1},
                operation_id="op-cancel-1",
            )
        )
        first = proc.response(timeout=5.0)
        second = proc.response(timeout=5.0)
        assert {first["request_id"], second["request_id"]} == {"req-st", "req-cancel"}
        assert first["ok"] is True and second["ok"] is True
        wait_no_response(proc)  # the slow analysis is still running
        proc.release_now()
        slow = proc.response(timeout=10.0)
        assert slow["request_id"] == "req-slow"
        assert slow["ok"] is True
    finally:
        proc.kill()


def test_saturated_work_lane_still_answers_control_requests(tmp_path: Path) -> None:
    proc = BackendProcess(tmp_path, workers=1, pending=1)
    try:
        proc.send(create_request("req-w1"))
        proc.send(create_request("req-w2", operation_id="op-create-2"))
        proc.send(create_request("req-w3", operation_id="op-create-3"))
        proc.send(envelope("req-w4", "runtime.status", {"runtime_requirement": "not_required"}))
        busy = proc.response(timeout=5.0)
        status = proc.response(timeout=5.0)
        assert busy["request_id"] == "req-w3"
        assert busy["error"]["code"] == "REQUEST_BUSY"
        assert busy["error"]["retryable"] is True
        assert status["request_id"] == "req-w4"
        assert status["ok"] is True
        proc.release_now()
        done1 = proc.response(timeout=10.0)
        done2 = proc.response(timeout=10.0)
        assert {done1["request_id"], done2["request_id"]} == {"req-w1", "req-w2"}
    finally:
        proc.kill()


def test_duplicate_request_id_does_not_execute_twice(tmp_path: Path) -> None:
    proc = BackendProcess(tmp_path)
    try:
        duplicate = create_request("req-dup")
        proc.send(duplicate)
        proc.send(duplicate)
        reused = proc.response(timeout=5.0)
        assert reused["request_id"] == "req-dup"
        assert reused["ok"] is False
        assert reused["error"]["code"] == "REQUEST_ID_REUSED"
        assert reused["error"]["retryable"] is False
        proc.release_now()
        executed = proc.response(timeout=10.0)
        assert executed["request_id"] == "req-dup"
        assert executed["ok"] is True
        assert len(proc.counter_text().strip().splitlines()) == 1
    finally:
        proc.kill()


# ---------------------------------------------------------------------------
# In-process coverage of parsing and shutdown details
# ---------------------------------------------------------------------------


class _CollectOut:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.chunks.append(data)
        return len(data)

    def flush(self) -> None:
        return None

    @property
    def value(self) -> bytes:
        return b"".join(self.chunks)


class _FailingOut:
    def write(self, data: bytes) -> int:
        raise OSError("pipe closed")

    def flush(self) -> None:
        return None


@pytest.fixture(scope="module")
def registry() -> ContractRegistry:
    return ContractRegistry.load(CONTRACTS_DIR)


def _serve_lines(
    registry: ContractRegistry, lines: list[bytes]
) -> tuple[list[dict[str, Any]], str]:
    out = _CollectOut()
    err = StringIO()
    handler = lambda request: dict(SUMMARY_RESULT)  # noqa: E731
    transport = NdjsonTransport(
        registry,
        handler,
        stdin=BytesIO(b"".join(lines)),
        stdout=out,  # type: ignore[arg-type]
        stderr=err,
    )
    code = transport.serve()
    assert code == EXIT_OK
    payloads = [json.loads(raw.decode("utf-8")) for raw in out.value.splitlines()]
    return payloads, err.getvalue()


def test_parse_rules_duplicate_keys_nan_and_encoding(registry: ContractRegistry) -> None:
    duplicate = (
        b'{"request_id":"req-d","request_id":"req-d2","contract_version":"2.0.0",'
        b'"operation_id":null,"method":"runtime.status",'
        b'"params":{"runtime_requirement":"not_required"},"simulation":true}\n'
    )
    payloads, _ = _serve_lines(registry, [duplicate])
    assert len(payloads) == 1
    assert payloads[0]["error"]["code"] == "TRANSPORT_INVALID_JSON"
    assert payloads[0]["request_id"].startswith("transport-")


def test_broken_stdout_stops_server_safely(registry: ContractRegistry) -> None:
    err = StringIO()
    transport = NdjsonTransport(
        registry,
        lambda request: dict(SUMMARY_RESULT),
        stdin=BytesIO(
            json.dumps(envelope("req-pipe", "analysis.status", status_params())).encode("utf-8")
            + b"\n"
        ),
        stdout=_FailingOut(),  # type: ignore[arg-type]
        stderr=err,
    )
    assert transport.serve() == EXIT_IO_ERROR
    assert "rule=stdout_closed" in err.getvalue()


def test_eof_shutdown_waits_for_inflight_handler(registry: ContractRegistry) -> None:
    release = threading.Event()
    out = _CollectOut()
    err = StringIO()

    def slow_handler(request: Any) -> dict[str, Any]:
        release.wait(timeout=5)
        return dict(SUMMARY_RESULT)

    transport = NdjsonTransport(
        registry,
        slow_handler,
        stdin=BytesIO(json.dumps(create_request("req-eof")).encode("utf-8") + b"\n"),
        stdout=out,  # type: ignore[arg-type]
        stderr=err,
    )
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(transport.serve()))
    thread.start()
    time.sleep(0.4)
    assert out.value == b""  # EOF alone does not answer while handler runs
    release.set()
    thread.join(timeout=10)
    assert result == [EXIT_OK]
    payloads = [json.loads(raw.decode("utf-8")) for raw in out.value.splitlines()]
    assert [payload["request_id"] for payload in payloads] == ["req-eof"]
