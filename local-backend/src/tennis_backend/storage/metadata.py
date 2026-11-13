"""SQLite metadata, state events and completed checkpoints (BE-006).

A small repository for the metadata the backend must survive an interruption
with (plan §10.2, AC-SPEC-045):

* ``tasks``, ``task_versions`` and append-only ``task_state_events`` carry the
  task domain.
* ``task_checkpoints`` holds only *completed and validated* stage outputs, each
  with a digest of what the checkpoint preserves.
* ``runtime_state`` and ``runtime_state_events`` keep the local capability
  state in its own table, never in the task state column.
* ``media_assets`` stores probe metadata, references and sizes only - media is
  never written into the database as a blob.

Every mutation runs in one transaction (explicit ``BEGIN IMMEDIATE``), so an
interrupted write rolls back completely: a state change and the checkpoint
recorded with it either both survive or neither does, and no half checkpoint is
ever readable. State moves are compare-and-set: the stored state is read and
checked inside the write transaction and the row update carries the expected
state as a predicate, so a stale request from another connection is rejected
instead of overwriting a newer state. Public output resource bookkeeping lives
in the resources batch, not here.

Only identifiers, state names, digests and probe numbers are stored; messages
raised from this module carry identifiers and state names but never raw media
or credential material, and nothing here logs. Actual runtime prepare/start/
repair execution stays on the Bridge side; this module only records the state
the Python backend is told about.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from tennis_backend.domain.models import DomainViolation, MediaAsset, MediaProbe, Task, TaskVersion
from tennis_backend.domain.states import (
    PIPELINE_STATES,
    TASK_STAGES,
    CheckpointName,
    RuntimeState,
    TaskStage,
    TaskState,
    assert_checkpoints_allowed,
    assert_runtime_transition,
    assert_task_transition,
)

#: Storage schema version, bumped only by a deliberate migration.
SCHEMA_VERSION = 1

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        current_state TEXT NOT NULL,
        current_stage TEXT NOT NULL,
        simulation INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_versions (
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        version INTEGER NOT NULL,
        primary_route TEXT NOT NULL,
        auxiliary_routes TEXT NOT NULL,
        runtime_requirement TEXT NOT NULL,
        contract_version TEXT NOT NULL,
        config_version TEXT NOT NULL,
        media_id TEXT,
        consent_id TEXT,
        subject_id TEXT,
        input_digest TEXT,
        simulation INTEGER NOT NULL,
        PRIMARY KEY (task_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_state_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        from_state TEXT NOT NULL,
        to_state TEXT NOT NULL,
        reason TEXT,
        occurred_at TEXT NOT NULL,
        simulation INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_checkpoints (
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        name TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        simulation INTEGER NOT NULL,
        PRIMARY KEY (task_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS media_assets (
        media_id TEXT PRIMARY KEY,
        role TEXT NOT NULL,
        original_ref TEXT NOT NULL,
        derived_from TEXT,
        probe TEXT NOT NULL,
        time_mapping TEXT NOT NULL,
        time_mapping_valid INTEGER NOT NULL,
        simulation INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        state TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_state_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        from_state TEXT NOT NULL,
        to_state TEXT NOT NULL,
        reason TEXT,
        occurred_at TEXT NOT NULL
    )
    """,
)


@dataclass(frozen=True)
class StateEvent:
    """One append-only task state event."""

    seq: int
    task_id: str
    from_state: TaskState
    to_state: TaskState
    reason: str | None
    occurred_at: datetime
    simulation: bool


@dataclass(frozen=True)
class CheckpointRecord:
    """One completed and validated stage output."""

    name: CheckpointName
    payload_digest: str
    completed_at: datetime
    simulation: bool


@dataclass(frozen=True)
class TaskRecord:
    """Everything recovery needs for one task, without media blobs."""

    task_id: str
    state: TaskState
    stage: TaskStage
    versions: tuple[TaskVersion, ...]
    checkpoints: tuple[CheckpointRecord, ...]
    simulation: bool
    created_at: datetime
    updated_at: datetime


def _iso(at: datetime) -> str:
    """Store timezone-aware UTC stamps only."""
    if at.utcoffset() is None:
        raise DomainViolation("TIMESTAMP_REQUIRED", "timestamps must be timezone aware")
    return at.astimezone(UTC).isoformat()


def _from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _bool(value: int) -> bool:
    return bool(value)


class MetadataStore:
    """Repository over one SQLite metadata database."""

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

    def __enter__(self) -> MetadataStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying connection."""
        self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Run one all-or-nothing unit; any failure rolls everything back.

        ``BEGIN IMMEDIATE`` takes the database write lock up front, so reads
        and writes inside the block are serialized against every other
        connection; a commit failure is rolled back before it propagates.
        """
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def _conflict(self, table: str, identity: str) -> DomainViolation:
        return DomainViolation("ALREADY_EXISTS", f"{table} already holds a row for {identity}")

    # ------------------------------------------------------------------
    # Task metadata
    # ------------------------------------------------------------------

    def create_task(self, task: Task, *, at: datetime) -> None:
        """Insert a task with its versions and its first state event."""
        stamp = _iso(at)
        try:
            with self._transaction():
                self._connection.execute(
                    "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        task.task_id,
                        task.state,
                        task.stage,
                        int(task.simulation),
                        stamp,
                        stamp,
                    ),
                )
                for version in task.versions:
                    self._insert_version(version)
                self._connection.execute(
                    "INSERT INTO task_state_events"
                    " (task_id, from_state, to_state, reason, occurred_at, simulation)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (task.task_id, task.state, task.state, "created", stamp, int(task.simulation)),
                )
        except sqlite3.IntegrityError as error:
            raise self._conflict("tasks", task.task_id) from error

    def add_task_version(self, version: TaskVersion, *, at: datetime) -> None:
        """Append one frozen version to an existing task."""
        stamp = _iso(at)
        try:
            with self._transaction():
                if self._task_row(version.task_id) is None:
                    raise DomainViolation("UNKNOWN_TASK", f"task {version.task_id} is not stored")
                self._insert_version(version)
                self._connection.execute(
                    "UPDATE tasks SET updated_at = ? WHERE task_id = ?",
                    (stamp, version.task_id),
                )
        except sqlite3.IntegrityError as error:
            raise self._conflict(
                "task_versions", f"{version.task_id} v{version.version}"
            ) from error

    def _insert_version(self, version: TaskVersion) -> None:
        payload = version.model_dump(mode="json")
        self._connection.execute(
            "INSERT INTO task_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version.task_id,
                version.version,
                version.primary_route,
                json.dumps(payload["auxiliary_routes"]),
                version.runtime_requirement,
                version.contract_version,
                version.config_version,
                version.media_id,
                version.consent_id,
                version.subject_id,
                version.input_digest,
                int(version.simulation),
            ),
        )

    def _load_versions(self, task_id: str) -> tuple[TaskVersion, ...]:
        rows = self._connection.execute(
            "SELECT * FROM task_versions WHERE task_id = ? ORDER BY version", (task_id,)
        ).fetchall()
        versions = []
        for row in rows:
            routes = json.loads(row["auxiliary_routes"])
            versions.append(
                TaskVersion.model_validate(
                    {
                        "task_id": row["task_id"],
                        "version": row["version"],
                        "primary_route": row["primary_route"],
                        "auxiliary_routes": routes,
                        "runtime_requirement": row["runtime_requirement"],
                        "contract_version": row["contract_version"],
                        "config_version": row["config_version"],
                        "media_id": row["media_id"],
                        "consent_id": row["consent_id"],
                        "subject_id": row["subject_id"],
                        "input_digest": row["input_digest"],
                        "simulation": _bool(row["simulation"]),
                    }
                )
            )
        return tuple(versions)

    def task_state(self, task_id: str) -> TaskState | None:
        """The stored current task state, or ``None`` for an unknown task."""
        row = self._task_row(task_id)
        if row is None:
            return None
        return cast(TaskState, row["current_state"])

    def task_stage(self, task_id: str) -> TaskStage | None:
        """The stored current stage, or ``None`` for an unknown task."""
        row = self._task_row(task_id)
        if row is None:
            return None
        return cast(TaskStage, row["current_stage"])

    def _task_row(self, task_id: str) -> sqlite3.Row | None:
        return cast(
            "sqlite3.Row | None",
            self._connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone(),
        )

    def task_record(self, task_id: str) -> TaskRecord:
        """Load the full metadata record of one task."""
        row = self._task_row(task_id)
        if row is None:
            raise DomainViolation("UNKNOWN_TASK", f"task {task_id} is not stored")
        return TaskRecord(
            task_id=row["task_id"],
            state=row["current_state"],
            stage=row["current_stage"],
            versions=self._load_versions(task_id),
            checkpoints=self.checkpoints(task_id),
            simulation=_bool(row["simulation"]),
            created_at=_from_iso(row["created_at"]),
            updated_at=_from_iso(row["updated_at"]),
        )

    # ------------------------------------------------------------------
    # State events and checkpoints, written atomically together
    # ------------------------------------------------------------------

    def record_transition(
        self,
        task_id: str,
        *,
        current: TaskState,
        target: TaskState,
        reason: str | None = None,
        checkpoint: CheckpointName | None = None,
        checkpoint_digest: str | None = None,
        at: datetime,
    ) -> None:
        """Record one legal task state move, optionally with its checkpoint.

        ``checkpoint`` names the stage that just finished and was validated, so
        it is stored with the move into the next stage in a single transaction:
        an interruption leaves either both or neither, never a half checkpoint.

        The stored state is read, compared and written *inside* one
        ``BEGIN IMMEDIATE`` transaction and the row update carries the expected
        state as a predicate, so this is an atomic compare-and-set: another
        connection that commits a different transition first makes a stale
        request fail with ``STATE_MISMATCH`` instead of overwriting it.
        """
        stamp = _iso(at)
        try:
            with self._transaction():
                row = self._task_row(task_id)
                if row is None:
                    raise DomainViolation("UNKNOWN_TASK", f"task {task_id} is not stored")
                stored: TaskState = row["current_state"]
                if stored != current:
                    raise DomainViolation(
                        "STATE_MISMATCH", f"task {task_id} is in {stored}, not {current}"
                    )
                assert_task_transition(current, target)
                if checkpoint is not None:
                    if checkpoint_digest is None:
                        raise DomainViolation(
                            "CHECKPOINT_DIGEST_REQUIRED",
                            "a checkpoint needs the digest of its validated stage output",
                        )
                    assert_checkpoints_allowed(target, [checkpoint])
                simulation = int(_bool(row["simulation"]))
                self._connection.execute(
                    "INSERT INTO task_state_events"
                    " (task_id, from_state, to_state, reason, occurred_at, simulation)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (task_id, current, target, reason, stamp, simulation),
                )
                if checkpoint is not None:
                    self._connection.execute(
                        "INSERT INTO task_checkpoints VALUES (?, ?, ?, ?, ?)",
                        (task_id, checkpoint, checkpoint_digest, stamp, simulation),
                    )
                moved = self._connection.execute(
                    "UPDATE tasks SET current_state = ?, current_stage = ?, updated_at = ?"
                    " WHERE task_id = ? AND current_state = ?",
                    (target, TASK_STAGES[target], stamp, task_id, current),
                )
                if moved.rowcount != 1:
                    raise DomainViolation(
                        "STATE_MISMATCH", f"task {task_id} is no longer in {current}"
                    )
        except sqlite3.IntegrityError as error:
            raise self._conflict("task_checkpoints", f"{task_id} {checkpoint}") from error

    def state_events(self, task_id: str) -> tuple[StateEvent, ...]:
        """All recorded state events of one task, in order."""
        rows = self._connection.execute(
            "SELECT * FROM task_state_events WHERE task_id = ? ORDER BY seq", (task_id,)
        ).fetchall()
        return tuple(
            StateEvent(
                seq=row["seq"],
                task_id=row["task_id"],
                from_state=row["from_state"],
                to_state=row["to_state"],
                reason=row["reason"],
                occurred_at=_from_iso(row["occurred_at"]),
                simulation=_bool(row["simulation"]),
            )
            for row in rows
        )

    def checkpoints(self, task_id: str) -> tuple[CheckpointRecord, ...]:
        """Completed checkpoints only; an interrupted write never shows up here."""
        rows = self._connection.execute(
            "SELECT * FROM task_checkpoints WHERE task_id = ? ORDER BY completed_at, name",
            (task_id,),
        ).fetchall()
        return tuple(
            CheckpointRecord(
                name=row["name"],
                payload_digest=row["payload_digest"],
                completed_at=_from_iso(row["completed_at"]),
                simulation=_bool(row["simulation"]),
            )
            for row in rows
        )

    def incomplete_stages(self, task_id: str) -> tuple[CheckpointName, ...]:
        """Pipeline stages without a completed checkpoint (AC-SPEC-045).

        Recovery resumes from the last complete stage and redoes exactly the
        stages listed here; completed stages are never repeated.
        """
        done = {checkpoint.name for checkpoint in self.checkpoints(task_id)}
        if self.task_state(task_id) is None:
            raise DomainViolation("UNKNOWN_TASK", f"task {task_id} is not stored")
        return tuple(cast(CheckpointName, stage) for stage in PIPELINE_STATES if stage not in done)

    # ------------------------------------------------------------------
    # Media metadata: references and numbers, never blobs
    # ------------------------------------------------------------------

    def record_media_asset(self, asset: MediaAsset) -> None:
        """Store media metadata only: refs, probe numbers and mapping."""
        if asset.derived_from is not None:
            known = self._connection.execute(
                "SELECT 1 FROM media_assets WHERE media_id = ?", (asset.derived_from,)
            ).fetchone()
            if known is None:
                raise DomainViolation(
                    "UNKNOWN_MEDIA", f"origin media {asset.derived_from} is not stored"
                )
        payload = asset.model_dump(mode="json")
        try:
            with self._transaction():
                self._connection.execute(
                    "INSERT INTO media_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        asset.media_id,
                        asset.role,
                        asset.original_ref,
                        asset.derived_from,
                        json.dumps(payload["probe"]),
                        json.dumps(payload["time_mapping"]),
                        int(asset.time_mapping_valid),
                        int(asset.simulation),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise self._conflict("media_assets", asset.media_id) from error

    def media_asset(self, media_id: str) -> MediaAsset:
        """Load back one media metadata record."""
        row = self._connection.execute(
            "SELECT * FROM media_assets WHERE media_id = ?", (media_id,)
        ).fetchone()
        if row is None:
            raise DomainViolation("UNKNOWN_MEDIA", f"media {media_id} is not stored")
        return MediaAsset.model_validate(
            {
                "media_id": row["media_id"],
                "role": row["role"],
                "original_ref": row["original_ref"],
                "derived_from": row["derived_from"],
                "probe": json.loads(row["probe"]),
                "time_mapping": json.loads(row["time_mapping"]),
                "time_mapping_valid": _bool(row["time_mapping_valid"]),
                "simulation": _bool(row["simulation"]),
            }
        )

    def media_probe(self, media_id: str) -> MediaProbe:
        """The stored probe metadata of one media asset."""
        return self.media_asset(media_id).probe

    # ------------------------------------------------------------------
    # Runtime domain, kept in its own tables
    # ------------------------------------------------------------------

    def runtime_state(self) -> RuntimeState:
        """The stored local capability state; unset means ``not_initialized``."""
        row = cast(
            "sqlite3.Row | None",
            self._connection.execute("SELECT state FROM runtime_state WHERE id = 1").fetchone(),
        )
        if row is None:
            return "not_initialized"
        return cast(RuntimeState, row["state"])

    def record_runtime_transition(
        self,
        target: RuntimeState,
        *,
        current: RuntimeState | None = None,
        reason: str | None = None,
        at: datetime,
    ) -> None:
        """Record one legal runtime move; execution itself stays on the Bridge.

        Like the task transition, the stored runtime state is read and compared
        inside the same write transaction and the update carries the expected
        state as a predicate, so a stale request cannot overwrite a newer one.
        Passing ``current`` states the state the caller believes holds; a stale
        expectation fails with ``STATE_MISMATCH`` instead of being applied.
        """
        stamp = _iso(at)
        with self._transaction():
            row = cast(
                "sqlite3.Row | None",
                self._connection.execute("SELECT state FROM runtime_state WHERE id = 1").fetchone(),
            )
            stored = cast(RuntimeState, row["state"]) if row is not None else "not_initialized"
            if current is not None and stored != current:
                raise DomainViolation("STATE_MISMATCH", f"runtime state is {stored}, not {current}")
            assert_runtime_transition(stored, target)
            self._connection.execute(
                "INSERT INTO runtime_state_events (from_state, to_state, reason, occurred_at)"
                " VALUES (?, ?, ?, ?)",
                (stored, target, reason, stamp),
            )
            if row is None:
                self._connection.execute(
                    "INSERT INTO runtime_state (id, state, updated_at) VALUES (1, ?, ?)",
                    (target, stamp),
                )
                return
            moved = self._connection.execute(
                "UPDATE runtime_state SET state = ?, updated_at = ? WHERE id = 1 AND state = ?",
                (target, stamp, stored),
            )
            if moved.rowcount != 1:
                raise DomainViolation("STATE_MISMATCH", f"runtime state is no longer {stored}")

    def runtime_events(self) -> tuple[tuple[RuntimeState, RuntimeState, str | None, datetime], ...]:
        """Recorded runtime moves, in order."""
        rows = self._connection.execute(
            "SELECT * FROM runtime_state_events ORDER BY seq"
        ).fetchall()
        return tuple(
            (
                row["from_state"],
                row["to_state"],
                row["reason"],
                _from_iso(row["occurred_at"]),
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Introspection for tests and diagnostics
    # ------------------------------------------------------------------

    def column_types(self, table: str) -> dict[str, str]:
        """Declared column types of one table."""
        rows = self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"]: row["type"] for row in rows}


__all__ = [
    "SCHEMA_VERSION",
    "CheckpointRecord",
    "MetadataStore",
    "StateEvent",
    "TaskRecord",
]
