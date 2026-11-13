"""NDJSON subprocess transport with an injectable application handler (BE-010).

Protocol: one UTF-8 JSON object per line on stdin, one ``common:response``
per line on stdout. Every request and every handler result is validated
against the frozen v1 contracts (``tennis_backend.contracts.validation``);
business idempotency stays in the application layer — this layer only
guarantees protocol behavior.

Handler contract
----------------

A handler is ``Callable[[ValidatedRequest], Mapping[str, Any]]``. It receives
the validated request and returns the plain result payload for
``request.method``: the mapping must satisfy the method's frozen output
definition and carry the same ``simulation`` flag as the request envelope.
A handler may raise :class:`ContractViolation` (mapped to a structured
contract error) or any other exception (mapped to ``HANDLER_FAILED``);
exception text, raw params, paths, chat or credentials never reach stdout,
protocol errors or logs — logs carry a rule name and a correlation reference
only. Handlers must not write to stdout themselves; responses are emitted by
the transport under a lock as single complete lines (no global ``sys.stdout``
redirection, no interleaving).

Lane behavior
-------------

Side-effect methods run on a bounded worker lane (``max_workers`` concurrent,
``max_pending`` queued; saturated lanes answer ``REQUEST_BUSY``).
``analysis.status``, ``analysis.cancel`` and ``runtime.status`` are control
methods: they keep a separate always-available lane so status and cancel stay
responsive while a slow analysis handler is running or the work lane is full.

Failure behavior
----------------

- Undecodable, invalid-JSON (incl. duplicate keys and NaN/Infinity tokens),
  non-object and oversized lines are rejected with a safe
  ``transport-NNNNNN`` correlation id (raw input is never echoed) and the
  connection keeps serving later requests; oversized input is drained to the
  next newline without unbounded buffering.
- A syntactically valid ``request_id`` is served at most once; reuse answers
  ``REQUEST_ID_REUSED`` without re-running the handler.
- EOF waits for in-flight handlers (threads cannot be forcibly killed and are
  not pretended to be); a broken stdout pipe stops the server with
  ``EXIT_IO_ERROR``. Empty lines are ignored.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, BinaryIO

from tennis_backend.contracts.validation import (
    ContractRegistry,
    ContractViolation,
    ValidatedRequest,
    build_error,
    build_error_response,
    build_response,
    contract_violation_response,
    validate_request,
)

#: Single-line input cap in bytes; matches the Bridge ``maxLineBytes`` guard.
DEFAULT_MAX_LINE_BYTES = 1_048_576

#: Control methods answered on a lane that slow work cannot block (BE-010:
#: status/cancel must stay responsive during long analysis).
CONTROL_METHODS = frozenset({"analysis.status", "analysis.cancel", "runtime.status"})

#: Clean EOF exit code; stdin/stdout OS failure exit code.
EXIT_OK = 0
EXIT_IO_ERROR = 3

#: Same shape as the frozen ``common:id``; used to accept or reject a raw
#: ``request_id`` before schema validation can attribute an error to it.
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

_READ_CHUNK = 65536

Handler = Callable[[ValidatedRequest], Mapping[str, Any]]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that rejects ambiguous repeated object keys."""
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError("duplicate object key")
        seen.add(key)
    return dict(pairs)


def _reject_non_finite(token: str) -> float:
    """``parse_constant`` that rejects NaN/Infinity instead of accepting them."""
    raise ValueError(f"non-finite number token {token}")


class NdjsonTransport:
    """Serve frozen-contract NDJSON requests with an injectable handler."""

    def __init__(
        self,
        registry: ContractRegistry,
        handler: Handler,
        *,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
        stderr: Any = None,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        max_workers: int = 4,
        max_pending: int = 64,
        max_control_pending: int = 256,
    ) -> None:
        self._registry = registry
        self._handler = handler
        self._stdin = stdin if stdin is not None else sys.stdin.buffer
        self._stdout = stdout if stdout is not None else sys.stdout.buffer
        self._stderr = stderr if stderr is not None else sys.stderr
        self._max_line_bytes = max_line_bytes
        self._write_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._seen_lock = threading.Lock()
        self._seen_ids: set[str] = set()
        self._correlation_lock = threading.Lock()
        self._correlation = 0
        self._stop = threading.Event()
        self._pipe_broken = False
        self._work_slots = threading.BoundedSemaphore(max_workers + max_pending)
        self._work_pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="work")
        self._control_slots = threading.BoundedSemaphore(max_control_pending)
        self._control_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="control")

    # ------------------------------------------------------------------
    # Serving loop
    # ------------------------------------------------------------------

    def serve(self) -> int:
        """Read request lines until EOF or pipe failure; return an exit code."""
        status = EXIT_OK
        try:
            self._read_loop()
        except OSError:
            self._stop.set()
            self._log("io_error")
            status = EXIT_IO_ERROR
        self._work_pool.shutdown(wait=True)
        self._control_pool.shutdown(wait=True)
        if self._pipe_broken:
            status = EXIT_IO_ERROR
        return status

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            part = self._stdin.readline(self._max_line_bytes + 1)
            if not part:
                break  # EOF: in-flight handlers are waited for in serve().
            if part.endswith(b"\n"):
                self._dispatch_line(part[:-1])
            elif len(part) > self._max_line_bytes:
                # The line continues past the cap: reject and drain to the
                # next newline without buffering the rest.
                self._reject("line_too_large", "TRANSPORT_LINE_TOO_LARGE", retryable=True)
                self._drain_to_newline()
            else:
                # BufferedReader.readline blocks until a newline or EOF, so a
                # short tail without a newline can only be the final line.
                self._dispatch_line(part)

    def _drain_to_newline(self) -> None:
        while not self._stop.is_set():
            part = self._stdin.readline(self._max_line_bytes + 1)
            if not part or part.endswith(b"\n"):
                return

    # ------------------------------------------------------------------
    # Line handling
    # ------------------------------------------------------------------

    def _dispatch_line(self, raw: bytes) -> None:
        if not raw.strip():
            return  # empty line: no request, no response
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            self._reject("invalid_utf8", "TRANSPORT_INVALID_ENCODING", retryable=True)
            return
        try:
            payload = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite,
            )
        except ValueError:
            self._reject("invalid_json", "TRANSPORT_INVALID_JSON", retryable=True)
            return
        if not isinstance(payload, dict):
            self._reject("not_object", "TRANSPORT_NOT_OBJECT", retryable=True)
            return
        request_id = self._resolve_request_id(payload)
        if request_id is None:
            self._reject("request_id_invalid", "TRANSPORT_REQUEST_ID_INVALID", retryable=False)
            return
        if not self._claim_request_id(request_id):
            self._emit_error(
                request_id,
                build_error(code="REQUEST_ID_REUSED", stage="input", retryable=False),
            )
            return
        try:
            validated = validate_request(self._registry, payload)
        except ContractViolation as violation:
            self._emit_payload(contract_violation_response(self._registry, request_id, violation))
            return
        self._enqueue(validated)

    def _resolve_request_id(self, payload: Mapping[str, Any]) -> str | None:
        value = payload.get("request_id")
        if isinstance(value, str) and _REQUEST_ID_PATTERN.fullmatch(value):
            return value
        return None

    def _claim_request_id(self, request_id: str) -> bool:
        # Protocol rule: request ids are never reused, so a repeated id must
        # not run the handler (and its side effects) a second time.
        with self._seen_lock:
            if request_id in self._seen_ids:
                return False
            self._seen_ids.add(request_id)
            return True

    # ------------------------------------------------------------------
    # Lane dispatch
    # ------------------------------------------------------------------

    def _enqueue(self, validated: ValidatedRequest) -> None:
        if validated.method in CONTROL_METHODS or not validated.tool.side_effect:
            slots, pool, busy_code = self._control_slots, self._control_pool, "CONTROL_LANE_BUSY"
        else:
            slots, pool, busy_code = self._work_slots, self._work_pool, "REQUEST_BUSY"
        if not slots.acquire(blocking=False):
            self._emit_error(
                validated.request_id,
                build_error(code=busy_code, stage="runtime", retryable=True, user_action="retry"),
            )
            return
        pool.submit(self._run_handler, validated, slots)

    def _run_handler(self, validated: ValidatedRequest, slots: threading.BoundedSemaphore) -> None:
        try:
            result = self._handler(validated)
            payload = build_response(
                request_id=validated.request_id,
                method=validated.method,
                registry=self._registry,
                result=dict(result),
                simulation=validated.simulation,
            )
        except ContractViolation as violation:
            payload = contract_violation_response(self._registry, validated.request_id, violation)
        except Exception:
            # Sanitized: the exception text, params and paths stay out of the
            # protocol and the logs; only the rule and reference are recorded.
            self._log("handler_failed", validated.request_id)
            payload = build_error_response(
                request_id=validated.request_id,
                registry=self._registry,
                error=build_error(
                    code="HANDLER_FAILED", stage="runtime", retryable=True, user_action="retry"
                ),
            )
        finally:
            slots.release()
        self._emit_payload(payload)

    # ------------------------------------------------------------------
    # Output and diagnostics
    # ------------------------------------------------------------------

    def _emit_error(self, request_id: str, error: Mapping[str, Any]) -> None:
        self._emit_payload(
            build_error_response(request_id=request_id, registry=self._registry, error=error)
        )

    def _emit_payload(self, payload: Mapping[str, Any]) -> None:
        request_id = str(payload.get("request_id", "-"))
        try:
            line = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError):
            self._log("response_encode_failed", request_id)
            payload = build_error_response(
                request_id=request_id,
                registry=self._registry,
                error=build_error(
                    code="HANDLER_FAILED", stage="runtime", retryable=True, user_action="retry"
                ),
            )
            line = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        self._write_line(line)

    def _write_line(self, line: str) -> None:
        if self._stop.is_set():
            return
        try:
            with self._write_lock:
                self._stdout.write((line + "\n").encode("utf-8"))
                self._stdout.flush()
        except OSError:
            # Safe shutdown: stop accepting work; running handlers finish and
            # their writes are dropped. Threads are never claimed killed.
            self._pipe_broken = True
            self._stop.set()
            self._log("stdout_closed")

    def _reject(self, rule: str, code: str, *, retryable: bool) -> None:
        correlation = self._next_correlation()
        self._log(rule, correlation)
        self._emit_error(
            correlation,
            build_error(code=code, stage="input", retryable=retryable),
        )

    def _next_correlation(self) -> str:
        with self._correlation_lock:
            self._correlation += 1
            return f"transport-{self._correlation:06d}"

    def _log(self, rule: str, reference: str = "-") -> None:
        # stderr carries sanitized one-line diagnostics only: a stable rule
        # name plus a correlation or request reference. Never raw input,
        # exception text, paths, chat or credentials.
        with self._log_lock:
            self._stderr.write(f"tennis-backend transport: rule={rule} ref={reference}\n")
            self._stderr.flush()


def serve_stdio(
    registry: ContractRegistry,
    handler: Handler,
    **options: Any,
) -> int:
    """Serve the NDJSON protocol on the process stdin/stdout streams.

    The wiring into ``cli.py --stdio`` is a later batch (BE-018); this helper
    only bundles stream defaults so the entry point stays one call.
    """
    return NdjsonTransport(registry, handler, **options).serve()
