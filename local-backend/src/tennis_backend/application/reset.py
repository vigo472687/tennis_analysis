"""Demo-wide reset with per-item results (BE-026).

The ``all_demo`` scope of deletion (AC-SPEC-051): reset every demo task and
the demo runtime state in one confirmed operation, delegating each task to
the BE-025 stop-then-delete flow so per-item results stay truthful.

- **Confirmation does not extend** (确认不扩用): the reset's ``confirmed``
  authorizes exactly this batch; every delegated task deletion carries its
  own explicit confirmation, and later operations still need theirs.
- **User originals and global components are never touched** (不误删) — the
  delegated deletions remove backend-owned workspace files only, and the
  runtime reset is a *metadata state* move toward ``not_initialized``:
  installed binaries are never uninstalled, downloaded or modified here.
  When the state machine has no legal path back to ``not_initialized``
  (e.g. ``ready``), the runtime item is recorded honestly as not reset
  instead of being forced.
- **Failures are recorded per item** (失败逐项记录): each task reports its
  own completion and item statuses; the reset is ``complete`` only when
  every task completed. A repeated operation id replays the stored result.
- **Enumeration and coverage**: demo task ids come from two reconciled
  sources — the public ``ResourceRegistry.entries()`` over the same internal
  registry file the store uses, plus the metadata ``tasks`` table — so the
  reset covers every surviving demo task, including draft tasks that hold
  no resources (they are deleted as well; their metadata rows do not
  survive a reset).
- **Replay semantics**: the operation id is part of the ledger input, so a
  repeated operation id replays its own batch while every new confirmed
  reset executes. The frozen contract leaves all_demo replay semantics
  undefined — flagged as a CONTRACT_CHANGE_REQUEST for the contract owner.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tennis_backend.application.cancel import CancelService
from tennis_backend.application.delete import DeleteService
from tennis_backend.application.idempotency import OperationLedger
from tennis_backend.application.worker import CancellationToken
from tennis_backend.domain.models import DomainViolation
from tennis_backend.storage.metadata import MetadataStore
from tennis_backend.storage.resources import ResourceRegistry, ResourceStore

RESET_METHOD = "demo.reset"

_UNCONFIRMED = "RESET_UNCONFIRMED"


class ResetService:
    """Confirmed demo-wide reset delegating to per-task deletion."""

    def __init__(
        self,
        *,
        metadata: MetadataStore,
        ledger: OperationLedger,
        resources: ResourceStore,
        delete: DeleteService,
        cancel: CancelService,
    ) -> None:
        self._metadata = metadata
        self._ledger = ledger
        self._resources = resources
        self._delete = delete
        self._cancel = cancel

    def reset_all_demo(
        self,
        *,
        operation_id: str,
        confirmed: bool,
        running_tokens: tuple[CancellationToken, ...] = (),
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Reset all demo tasks and the demo runtime state (all_demo scope)."""
        if not confirmed:
            raise DomainViolation(_UNCONFIRMED, "a demo-wide reset needs confirmed=true")
        stamp = at or datetime.now(UTC)
        # The operation id is part of the ledger input on purpose: a
        # destructive all-demo reset must never be silently replayed across
        # invocations (BE-008's same-input twin rule would otherwise make
        # every reset after the first claim a stale completion). Same
        # operation id still retries a failed attempt and replays its own
        # success; each NEW confirmed reset executes. The frozen contract
        # does not define all_demo replay semantics — this behavior is
        # recorded as a CONTRACT_CHANGE_REQUEST for the contract owner.
        outcome = self._ledger.begin(
            operation_id,
            RESET_METHOD,
            {"scope": "all_demo", "operation_id": operation_id},
            at=stamp,
        )
        if outcome.replayed:
            replayed: dict[str, Any] = json.loads(outcome.record.result_json or "{}")
            return replayed

        # 1. stop in-flight derived work at its next check point
        for token in running_tokens:
            token.cancel()

        # 2. enumerate demo tasks from the public registry (same internal
        #    file the store reads; a second instance is read-only here)
        tasks: list[dict[str, Any]] = []
        processed: set[str] = set()
        refusals: list[str] = []
        # 2. enumerate-and-drain with reconciliation: the registry is a live
        # snapshot and metadata is the authority on surviving tasks, so the
        # drain enumerates BOTH until a full pass adds nothing new (BE-026
        # review blocking finding: a task escaping enumeration while the
        # reset claimed complete=True was a false completion claim). Every
        # enumerated task yields a settled entry — no exception may silently
        # drop one — and a still-changing view refuses completion.
        for _pass_index in range(4):
            registry_ids = self._stable_registry_ids()
            if registry_ids is None:
                refusals.append("RESET_REGISTRY_UNSTABLE")
                break
            surviving = self._surviving_task_ids()
            pending = sorted((registry_ids | surviving) - processed)
            if not pending:
                break
            for task_id in pending:
                try:
                    task_result = self._delete.delete_task(
                        # ledger operation ids allow only [A-Za-z0-9_.-]
                        operation_id=f"{operation_id}.{task_id}",
                        task_id=task_id,
                        version=None,
                        confirmed=confirmed,  # exactly this batch's confirmation
                        scope="task",
                        running_tokens=running_tokens,
                        at=stamp,
                    )
                    self._prune_empty_workspace(task_id)
                    tasks.append(
                        {
                            "task_id": task_id,
                            "complete": task_result["complete"],
                            "items": task_result["items"],
                        }
                    )
                except DomainViolation as violation:
                    tasks.append(
                        {
                            "task_id": task_id,
                            "complete": False,
                            "items": [],
                            "code": violation.code,
                        }
                    )
                except Exception as error:  # noqa: BLE001 - every enumerated
                    # task must yield a settled entry, whatever went wrong
                    # (sqlite contention, filesystem errors): the exception
                    # class name is stable and carries no raw content
                    tasks.append(
                        {
                            "task_id": task_id,
                            "complete": False,
                            "items": [],
                            "code": type(error).__name__,
                        }
                    )
                processed.add(task_id)
        registry_ids = self._stable_registry_ids()
        surviving = self._surviving_task_ids()
        unresolved = ((registry_ids or set()) | surviving) - processed
        if registry_ids is None:
            refusals.append("RESET_REGISTRY_UNSTABLE")
        if unresolved:
            refusals.append("RESET_CONCURRENT_ACTIVITY")

        # 3. demo runtime state back toward not_initialized — metadata only,
        #    never an uninstall of the installed components
        runtime_item: dict[str, Any] = {"item": "runtime_state", "status": "reset"}
        try:
            current = self._metadata.runtime_state()
            if current != "not_initialized":
                self._metadata.record_runtime_transition(
                    "not_initialized", reason="demo_reset", at=stamp
                )
        except DomainViolation as violation:
            runtime_item = {
                "item": "runtime_state",
                "status": "failed",
                "code": violation.code,
                "note": "no legal path to not_initialized; installed components untouched",
            }

        complete = (
            not refusals
            and all(task["complete"] for task in tasks)
            and runtime_item["status"] == "reset"
        )
        concurrent_note = (
            "concurrent task registration detected during the reset;"
            " re-run the reset to cover the remaining tasks"
        )
        notes = (
            "coverage: every surviving demo task — metadata reconciliation"
            " includes draft tasks without resources, which are deleted too",
            *((concurrent_note,) if refusals else ()),
        )
        result = {
            "operation_id": operation_id,
            "scope": "all_demo",
            "tasks": tasks,
            "runtime": runtime_item,
            "complete": complete,
            "refusals": refusals,
            "external_copies_uncontrolled": True,
            "runtime_uninstalled": False,
            "simulation": True,
            "notes": list(notes),
        }
        if complete:
            self._ledger.succeed(operation_id, json.dumps(result), at=stamp)
        else:
            self._ledger.fail(operation_id, "RESET_INCOMPLETE", at=stamp)
        return result

    def _stable_registry_ids(self, *, attempts: int = 5) -> set[str] | None:
        """Task ids from two consecutive identical registry reads.

        The registry file is rewritten atomically, but a read racing a
        rewrite window can still miss a just-registered task; only two
        consecutive identical snapshots count as a stable view. ``None``
        means the view never stabilized and completion must be refused.
        """
        previous: set[str] | None = None
        for _ in range(attempts):
            registry = ResourceRegistry(self._registry_path())
            current = {entry.task_id for entry in registry.entries()}
            if previous is not None and current == previous:
                return current
            previous = current
        return None

    def _surviving_task_ids(self) -> set[str]:
        """Tasks metadata still tracks as not deleted (reconciliation source).

        ``MetadataStore`` has no public enumeration, so this reads the
        backend's own SQLite schema (``task_id``/``current_state`` on the
        ``tasks`` table, stable since B1) so a concurrently created task can
        never escape the reset's reconciliation by being invisible to the
        resource registry.
        """
        connection = self._metadata._connection
        rows = connection.execute("SELECT task_id, current_state FROM tasks").fetchall()
        return {str(row["task_id"]) for row in rows if row["current_state"] != "deleted"}

    def _registry_path(self) -> Path:
        """The internal registry file the store reads (same storage family)."""
        return Path(self._resources.internal_root) / "resource-registry.json"

    def _prune_empty_workspace(self, task_id: str) -> None:
        """Remove the task's workspace directories once they are empty.

        Only empty directories are removed, bottom-up, strictly inside the
        public root: files are never touched here and the removal of a
        non-empty directory is refused by the OS itself.
        """
        import contextlib
        import os

        root = Path(self._resources.public_root)
        workspace = root / "projects" / task_id
        if not workspace.is_dir():
            return
        for current, _dirs, _files in os.walk(workspace, topdown=False):
            # bottom-up: a directory only becomes removable once its children
            # are gone; non-empty directories fail the rmdir and stay
            with contextlib.suppress(OSError):
                os.rmdir(current)
        projects = root / "projects"
        with contextlib.suppress(OSError):
            projects.rmdir()
