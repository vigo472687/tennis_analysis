"""Stop-then-delete with per-item results (BE-025).

Task deletion behind the frozen ``analysis.delete`` method (AC-SPEC-039/040/
042): stop writes first, then delete item by item, reporting every item.

- **Stop before deleting** — running worker tokens are cancelled and an
  active task moves to ``cancelled`` (BE-022) so derived writes are blocked
  before any file is removed; only then does deletion start, and the task
  settles into the absolute terminal ``deleted`` state.
- **Per-item truth** (逐项结果) — every registered resource is deleted
  through the BE-007 store, which records ``deleted``, ``not_found`` or
  ``delete_failed`` per item; a resource outside its workspace raises
  ``ORIGINAL_PRESERVED`` and is reported as ``outside_control``. The result
  is ``complete`` only when nothing was left undeleted; failed items are
  retryable by re-invoking the same operation id (the ledger restarts the
  failed attempt, and items a previous attempt already removed report as
  ``deleted`` again instead of failing the retry).
- **User originals and external copies are never touched** (不误删) — the
  store deletes backend-owned workspace files only; the user's original
  files outside the authorized roots are physically unreachable to this
  flow, and export packages already handed out are reported as uncontrolled
  via the frozen ``external_copies_uncontrolled`` field.
- **Idempotent replay** — the delete operation is ledger-bound (BE-008):
  repeating the same operation id replays the stored per-item result
  without touching files again.
- **``all_demo`` scope** is the separate reset flow (BE-026) and is refused
  here rather than half-implemented.

The caller must have authorization for deletion upstream (consent/final
confirmation per the WorkBuddy layer); the ``confirmed: true`` contract
field is re-checked here.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tennis_backend.application.cancel import CancelService
from tennis_backend.application.idempotency import OperationLedger
from tennis_backend.application.worker import CancellationToken
from tennis_backend.domain.models import DomainViolation
from tennis_backend.storage.metadata import MetadataStore
from tennis_backend.storage.resources import ResourceStore

DELETE_METHOD = "analysis.delete"

_UNCONFIRMED = "DELETE_UNCONFIRMED"
_SCOPE_UNAVAILABLE = "DELETE_SCOPE_UNAVAILABLE"

#: Store lifecycle → frozen delete_result item status.
_STATUS_BY_LIFECYCLE: dict[str, str] = {
    "deleted": "deleted",
    "not_found": "not_found",
    "delete_failed": "failed",
    "outside_control": "outside_control",
    "retained": "failed",  # store refused to delete: retry needed
}


class DeleteService:
    """Stop-then-delete orchestration with per-item reporting."""

    def __init__(
        self,
        *,
        metadata: MetadataStore,
        ledger: OperationLedger,
        resources: ResourceStore,
        cancel: CancelService,
    ) -> None:
        self._metadata = metadata
        self._ledger = ledger
        self._resources = resources
        self._cancel = cancel

    def delete_task(
        self,
        *,
        operation_id: str,
        task_id: str,
        version: int | None,
        confirmed: bool,
        scope: str = "task",
        running_tokens: tuple[CancellationToken, ...] = (),
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Delete one task's backend-owned resources with per-item results."""
        if not confirmed:
            raise DomainViolation(_UNCONFIRMED, "a deletion needs confirmed=true")
        if scope != "task":
            raise DomainViolation(
                _SCOPE_UNAVAILABLE,
                f"scope {scope} is the separate reset flow and is not implemented here",
            )
        stamp = at or datetime.now(UTC)
        params = {"task_id": task_id, "version": version, "scope": scope}
        outcome = self._ledger.begin(operation_id, DELETE_METHOD, params, at=stamp)
        if outcome.replayed:
            replayed: dict[str, Any] = json.loads(outcome.record.result_json or "{}")
            return replayed

        # 1. stop writes: in-flight work stops at its next check point
        for token in running_tokens:
            token.cancel()

        # 2. make the task cancellable/settled-safe, then settle to deleted;
        # a retry re-enters with the task already deleted — nothing to move
        state = self._metadata.task_state(task_id)
        if state is None:
            raise DomainViolation("UNKNOWN_TASK", f"task {task_id} is not stored")
        if state != "deleted":
            if state in (
                "draft",
                "waiting_for_capability",
                "checking_input",
                "analyzing",
                "researching",
                "rendering",
                "failed",
            ):
                self._cancel.cancel(task_id, running_tokens=running_tokens)
                cancelled_state = self._metadata.task_state(task_id)
                if cancelled_state is None:
                    raise DomainViolation("UNKNOWN_TASK", f"task {task_id} disappeared")
                state = cancelled_state
            self._metadata.record_transition(
                task_id, current=state, target="deleted", reason="user_delete", at=stamp
            )

        # 3. per-item deletion through the store (originals never touched)
        items: list[dict[str, Any]] = []
        for resource in self._resources.resources(task_id, version):
            item: dict[str, Any] = {"resource_id": resource.resource_id}
            try:
                deleted = self._resources.delete(resource.resource_id, user_requested=True)
                item["status"] = _STATUS_BY_LIFECYCLE.get(deleted.lifecycle, "failed")
                item["code"] = None
            except DomainViolation as violation:
                if violation.code == "DOMAIN_ORIGINAL_PRESERVED":
                    item.update(status="outside_control", code=None)
                elif violation.code == "DOMAIN_ALREADY_DELETED":
                    # retry re-enumerates everything: an item a previous
                    # attempt already removed is simply deleted (truthful)
                    item.update(status="deleted", code=None)
                else:
                    item.update(status="failed", code=violation.code)
            items.append(item)

        complete = all(item["status"] in ("deleted", "not_found") for item in items)

        # 4. sweep unregistered scratch in the deleted scope's workspace (e.g.
        # the normalize derivative written straight into workspace/media): a
        # COMPLETED delete must not leave media files behind just because
        # they were never registered resources. An incomplete attempt keeps
        # everything so its retry can finish properly. The sweep scope MUST
        # match the items enumeration scope: a version-scoped delete sweeps
        # only that version's directory, never another version's published
        # files (Phase 5 review BLOCKING-1 — a version=9 delete used to
        # silently wipe v1 without any per-item report). Originals live
        # under the caller-managed ingest roots and are never touched.
        if complete:
            projects_root = self._resources.public_root / "projects" / task_id
            sweep_dirs: list[Path]
            if version is None:
                sweep_dirs = [projects_root]
            else:
                sweep_dirs = [projects_root / f"v{version}"]
            failed_sweeps = 0
            for sweep_dir in sweep_dirs:
                if not sweep_dir.exists():
                    continue
                for leftover in sorted(sweep_dir.rglob("*")):
                    if not leftover.is_file():
                        continue
                    try:
                        leftover.unlink()
                    except OSError:
                        # record the failure honestly and mark the operation
                        # incomplete so it can be retried (Phase 5 review D-6)
                        failed_sweeps += 1
            if failed_sweeps:
                items.append(
                    {"resource_id": "workspace-sweep", "status": "failed", "code": "SWEEP_FAILED"}
                )
            # remove now-empty directories the sweep may leave behind
            for sweep_dir in sweep_dirs:
                if sweep_dir.exists():
                    for empty in sorted(sweep_dir.rglob("*"), reverse=True):
                        if empty.is_dir():
                            empty.rmdir()
                    with contextlib.suppress(OSError):
                        sweep_dir.rmdir()

        complete = complete and not any(item.get("code") == "SWEEP_FAILED" for item in items)

        result = {
            "operation_id": operation_id,
            "items": items,
            "complete": complete,
            "external_copies_uncontrolled": True,
            "runtime_uninstalled": False,
            "simulation": True,
        }
        if not complete:
            self._ledger.fail(operation_id, "DELETE_INCOMPLETE", at=stamp)
            return result
        self._ledger.succeed(operation_id, json.dumps(result), at=stamp)
        return result
