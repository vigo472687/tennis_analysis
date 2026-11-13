"""Persistent operation-id idempotency for side-effecting methods (BE-008).

One ledger, one dedicated ``operations`` table inside the same configured
SQLite metadata file (plan §10.2) - no second task or metadata store. Every
side-effecting call binds its ``operation_id`` to the *method plus* the digest
of its canonical input (sorted-key JSON), matching the shared contract's
operation binding and nothing more, so:

* the same method and canonical inputs replay the stored result and the caller
  can look up the current resource record instead of working again (EX-005);
* the same ``operation_id`` with a different method or a different input
  version is a conflict and mutates nothing; a genuinely new input version
  under a fresh operation id starts normally. Handlers keep the input version
  and ``params.operation_id`` inside ``params``, so they stay part of the
  digest when the application layer integrates;
* a call that is still ``running`` or was left ``unknown`` by an interruption
  is never silently re-executed - not even after a restart - and becomes
  actionable only through :meth:`OperationLedger.resolve` with an explicit
  ``confirm_success`` or ``confirm_failure`` (validated at runtime);
* under concurrency exactly one attempt wins; every other attempt sees the
  unresolved operation and must fall back to :meth:`lookup` polling.

Like the storage batch, every mutation runs in one ``BEGIN IMMEDIATE``
transaction with a compare-and-set predicate update, so a stale writer can
never overwrite a newer state. The table stores identifiers, digests and
JSON-safe payloads only - no media blobs - and violations carry identifiers
and state names, never raw inputs, and nothing here logs.

Routing, runtime startup, teaching prose and external delivery are decided
elsewhere; this module only guarantees the once-only bookkeeping.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from tennis_backend.domain.models import DomainViolation

#: Dedicated table inside the configured metadata database.
TABLE = "operations"

#: Lifecycle of one side-effecting operation.
OperationState = Literal["running", "succeeded", "failed", "unknown"]

#: Explicit semantics required before an unresolved operation may continue.
Resolution = Literal["confirm_success", "confirm_failure"]

_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_METHOD = re.compile(r"^[a-z]+\.[a-z_]{1,95}$")

_SCHEMA = (
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        operation_id TEXT PRIMARY KEY,
        method TEXT NOT NULL,
        input_digest TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('running', 'succeeded', 'failed', 'unknown')),
        result_json TEXT,
        error_code TEXT,
        attempt INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        settled_at TEXT,
        UNIQUE (method, input_digest)
    )
    """,
)


@dataclass(frozen=True)
class OperationRecord:
    """The stored lifecycle of one side-effecting operation."""

    operation_id: str
    method: str
    input_digest: str
    state: OperationState
    result_json: str | None
    error_code: str | None
    attempt: int
    started_at: datetime
    settled_at: datetime | None


@dataclass(frozen=True)
class BeginOutcome:
    """What a handler must do after :meth:`OperationLedger.begin`.

    ``replayed`` is true when an earlier, already settled attempt with the same
    operation id or the same canonical inputs produced the result, so the
    handler returns it instead of working again. ``same_inputs`` is true when
    the match came from identical canonical inputs under a different
    operation id (EX-005).
    """

    record: OperationRecord
    replayed: bool = False
    same_inputs: bool = False


def canonical_input(params: Mapping[str, Any]) -> str:
    """Serialize inputs once, independent of key order.

    ``sort_keys`` makes ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` identical,
    nested mappings are canonicalized recursively by ``json.dumps``, list order
    is preserved because it carries meaning, and non-JSON values are rejected
    instead of being silently stringified.
    """
    try:
        return json.dumps(
            params, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise DomainViolation(
            "UNSUPPORTED_INPUT", "operation inputs must be JSON-serializable values"
        ) from error


def input_digest(params: Mapping[str, Any]) -> str:
    """The canonical digest binding an operation id to its input version."""
    return hashlib.sha256(canonical_input(params).encode("utf-8")).hexdigest()


class OperationLedger:
    """Repository over the dedicated ``operations`` table."""

    def __init__(self, path: str | Path) -> None:
        self._connection = sqlite3.connect(path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        try:
            with self._transaction():
                for statement in _SCHEMA:
                    self._connection.execute(statement)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> OperationLedger:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying connection."""
        self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """One all-or-nothing unit; reads and writes share the write lock."""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def _row(self, operation_id: str) -> sqlite3.Row | None:
        return cast(
            "sqlite3.Row | None",
            self._connection.execute(
                f"SELECT * FROM {TABLE} WHERE operation_id = ?", (operation_id,)
            ).fetchone(),
        )

    def _row_for_inputs(self, method: str, digest: str) -> sqlite3.Row | None:
        return cast(
            "sqlite3.Row | None",
            self._connection.execute(
                f"SELECT * FROM {TABLE} WHERE method = ? AND input_digest = ?",
                (method, digest),
            ).fetchone(),
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> OperationRecord:
        return OperationRecord(
            operation_id=row["operation_id"],
            method=row["method"],
            input_digest=row["input_digest"],
            state=row["state"],
            result_json=row["result_json"],
            error_code=row["error_code"],
            attempt=row["attempt"],
            started_at=_from_iso(row["started_at"]),
            settled_at=(_from_iso(row["settled_at"]) if row["settled_at"] is not None else None),
        )

    # ------------------------------------------------------------------
    # Starting an operation
    # ------------------------------------------------------------------

    def begin(
        self,
        operation_id: str,
        method: str,
        params: Mapping[str, Any],
        *,
        at: datetime,
    ) -> BeginOutcome:
        """Start an operation, or replay the outcome it already has.

        The binding is the pair of *method* and canonical input digest, exactly
        as the shared contract binds an operation: the whole decision happens
        inside one write transaction, a concurrent or repeated attempt either
        replays a settled result, retries a known failure, or is refused with
        ``OPERATION_UNRESOLVED`` - it never starts a second execution of the
        same side effect. An operation id already bound to another method or
        another input version is a conflict and mutates nothing, while a
        genuinely new input version under a fresh operation id starts normally.
        """
        self._check_identifiers(operation_id, method)
        digest = input_digest(params)
        stamp = _iso(at)
        with self._transaction():
            row = self._row(operation_id)
            if row is not None:
                if row["method"] != method or row["input_digest"] != digest:
                    raise DomainViolation(
                        "INPUT_CONFLICT",
                        f"operation {operation_id} is bound to method"
                        f" {row['method']} with a different input version",
                    )
                record = self._record(row)
                if record.state == "succeeded":
                    return BeginOutcome(record=record, replayed=True)
                if record.state == "failed":
                    retried = self._connection.execute(
                        f"UPDATE {TABLE} SET attempt = attempt + 1, state = 'running',"
                        " result_json = NULL, error_code = NULL, started_at = ?,"
                        " settled_at = NULL WHERE operation_id = ? AND state = 'failed'",
                        (stamp, operation_id),
                    )
                    if retried.rowcount != 1:
                        raise DomainViolation(
                            "STATE_MISMATCH", f"operation {operation_id} is no longer failed"
                        )
                    fresh = self._row(operation_id)
                    assert fresh is not None
                    return BeginOutcome(record=self._record(fresh))
                raise DomainViolation(
                    "OPERATION_UNRESOLVED",
                    f"operation {operation_id} is {record.state}; resolve it explicitly",
                )
            twin = self._row_for_inputs(method, digest)
            if twin is not None:
                record = self._record(twin)
                if record.state == "succeeded":
                    return BeginOutcome(record=record, replayed=True, same_inputs=True)
                raise DomainViolation(
                    "OPERATION_UNRESOLVED",
                    f"operation {record.operation_id} is {record.state}; resolve it explicitly",
                )
            self._connection.execute(
                f"INSERT INTO {TABLE}"
                " (operation_id, method, input_digest, state, result_json, error_code,"
                "  attempt, started_at, settled_at)"
                " VALUES (?, ?, ?, 'running', NULL, NULL, 1, ?, NULL)",
                (operation_id, method, digest, stamp),
            )
            fresh = self._row(operation_id)
            assert fresh is not None
            return BeginOutcome(record=self._record(fresh))

    # ------------------------------------------------------------------
    # Settling an operation
    # ------------------------------------------------------------------

    def succeed(self, operation_id: str, result_json: str, *, at: datetime) -> None:
        """Record the settled result of the running attempt (compare-and-set)."""
        if not result_json:
            raise DomainViolation("RESULT_REQUIRED", "a settled result must not be empty")
        stamp = _iso(at)
        with self._transaction():
            moved = self._connection.execute(
                f"UPDATE {TABLE} SET state = 'succeeded', result_json = ?,"
                " error_code = NULL, settled_at = ?"
                " WHERE operation_id = ? AND state = 'running'",
                (result_json, stamp, operation_id),
            )
            if moved.rowcount != 1:
                raise DomainViolation("STATE_MISMATCH", f"operation {operation_id} is not running")

    def fail(self, operation_id: str, error_code: str, *, at: datetime) -> None:
        """Record a known failure; the next begin may retry it deliberately."""
        if not error_code:
            raise DomainViolation("ERROR_CODE_REQUIRED", "a failure needs a stable error code")
        stamp = _iso(at)
        with self._transaction():
            moved = self._connection.execute(
                f"UPDATE {TABLE} SET state = 'failed', error_code = ?,"
                " result_json = NULL, settled_at = ?"
                " WHERE operation_id = ? AND state = 'running'",
                (error_code, stamp, operation_id),
            )
            if moved.rowcount != 1:
                raise DomainViolation("STATE_MISMATCH", f"operation {operation_id} is not running")

    def resolve(
        self,
        operation_id: str,
        resolution: Resolution,
        *,
        result_json: str | None = None,
        error_code: str | None = None,
        at: datetime,
    ) -> OperationRecord:
        """Settle an unresolved operation with explicit semantics only.

        This is the sole recovery path after a restart or an undetermined
        outcome: ``confirm_success`` requires the result payload, and
        ``confirm_failure`` requires the stable error code. Anything else is
        rejected before any read or write, so the operation stays running or
        unknown and no side effect is silently repeated. (The ``Literal`` type
        only guides static callers; the value is checked here at runtime.)
        """
        if resolution not in ("confirm_success", "confirm_failure"):
            raise DomainViolation(
                "INVALID_RESOLUTION",
                "resolution must be confirm_success or confirm_failure",
            )
        stamp = _iso(at)
        with self._transaction():
            row = self._row(operation_id)
            if row is None:
                raise DomainViolation(
                    "UNKNOWN_OPERATION", f"operation {operation_id} is not stored"
                )
            if row["state"] not in ("running", "unknown"):
                raise DomainViolation(
                    "ALREADY_SETTLED", f"operation {operation_id} is already {row['state']}"
                )
            if resolution == "confirm_success":
                if not result_json:
                    raise DomainViolation(
                        "RESULT_REQUIRED", "confirm_success needs the settled result"
                    )
                moved = self._connection.execute(
                    f"UPDATE {TABLE} SET state = 'succeeded', result_json = ?,"
                    " error_code = NULL, settled_at = ?"
                    " WHERE operation_id = ? AND state IN ('running', 'unknown')",
                    (result_json, stamp, operation_id),
                )
            else:
                if not error_code:
                    raise DomainViolation(
                        "ERROR_CODE_REQUIRED", "confirm_failure needs a stable error code"
                    )
                moved = self._connection.execute(
                    f"UPDATE {TABLE} SET state = 'failed', error_code = ?,"
                    " result_json = NULL, settled_at = ?"
                    " WHERE operation_id = ? AND state IN ('running', 'unknown')",
                    (error_code, stamp, operation_id),
                )
            if moved.rowcount != 1:
                raise DomainViolation(
                    "STATE_MISMATCH", f"operation {operation_id} changed while resolving"
                )
            fresh = self._row(operation_id)
            assert fresh is not None
            return self._record(fresh)

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def lookup(self, operation_id: str) -> OperationRecord | None:
        """The current record of one operation, or ``None`` if never started."""
        row = self._row(operation_id)
        return self._record(row) if row is not None else None

    def lookup_by_inputs(self, method: str, params: Mapping[str, Any]) -> OperationRecord | None:
        """The operation bound to these canonical inputs, if any."""
        self._check_identifiers("lookup-op", method)
        row = self._row_for_inputs(method, input_digest(params))
        return self._record(row) if row is not None else None

    def unresolved(self) -> tuple[OperationRecord, ...]:
        """Operations that need explicit resolution before they may run again."""
        rows = self._connection.execute(
            f"SELECT * FROM {TABLE} WHERE state IN ('running', 'unknown') ORDER BY started_at"
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_identifiers(operation_id: str, method: str) -> None:
        if _OPERATION_ID.fullmatch(operation_id) is None:
            raise DomainViolation("INVALID_OPERATION_ID", "operation id has an unexpected shape")
        if _METHOD.fullmatch(method) is None:
            raise DomainViolation("INVALID_METHOD", "method has an unexpected shape")

    def column_types(self, table: str) -> dict[str, str]:
        """Declared column types of one table."""
        rows = self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"]: row["type"] for row in rows}


def _iso(at: datetime) -> str:
    """Store timezone-aware UTC stamps only."""
    if at.utcoffset() is None:
        raise DomainViolation("TIMESTAMP_REQUIRED", "timestamps must be timezone aware")
    return at.astimezone(UTC).isoformat()


def _from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


__all__ = [
    "BeginOutcome",
    "OperationLedger",
    "OperationRecord",
    "OperationState",
    "Resolution",
    "TABLE",
    "canonical_input",
    "input_digest",
]
