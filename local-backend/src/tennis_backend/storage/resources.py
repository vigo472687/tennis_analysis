"""Public workspace resource store: paths, hashes and deletion (BE-007).

A small, explicit store for the resources the backend publishes and must be
able to enumerate for later deletion (plan §8.3, §10.1; contracts README):

* The public workspace root holds one directory per task version, referenced as
  ``projects/<task_id>/v<version>``. That ref is authoritative; nothing in this
  module requires a ``public`` subdirectory - Skills choose layout such as
  ``public/report/...`` or ``records/...`` inside the workspace.
* The registry (hash, size, mime, lifecycle per resource) lives in backend
  internal storage, which must be a separate root from the public workspace;
  the two roots may not contain each other.
* Paths are relative to the authorized root: absolute paths, drive letters,
  ``..`` traversal, backslash separators and any symlink that leaves the root
  or passes through one are rejected, and a path may never name another task's
  version.
* Publishing is atomic: content is written to a unique staging file created
  exclusively with ``mkstemp`` next to the target (never following or
  truncating a pre-existing path), fsynced, moved into place with
  ``os.replace`` and only then registered. If registration fails, this
  attempt's own published file is removed again, so a half-published resource
  never survives and a planted staging symlink cannot redirect the write into
  a user original.
* Every mutation (publish, delete) holds an exclusive ``flock`` on a lock file
  in internal storage for its whole read-check-write sequence, serializing
  registry read-modify-write and target claiming across threads and
  processes; a same-path race has exactly one winner and a losing attempt
  never deletes the winner's file.
* Originals are user-owned. They live outside the public workspace and this
  store never deletes or overwrites them; publishing an ``input_copy`` copies
  bytes out of the original and deletion of such a copy requires the explicit
  user request already enforced by the domain ``Resource``.

Every violation raises :class:`~tennis_backend.domain.models.DomainViolation`
with a stable code and a message carrying identifiers and paths only - never
raw media or credential material - and nothing here logs.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

from tennis_backend.domain.models import DomainViolation, Resource, ResourceKind

#: Registry document version, bumped only by a deliberate migration.
REGISTRY_VERSION = 1

#: The frozen contract ``relative_path`` pattern (look-around needs ``re``).
_RELATIVE_PATH = re.compile(r"^(?!/)(?![A-Za-z]:)(?!.*(?:^|/)\.\.(?:/|$))[^\x00]+$")

#: The frozen contract ``id`` pattern, used for task identifiers.
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

_CHUNK = 1024 * 1024


def _validated_relative_path(value: str) -> str:
    """Apply the frozen relative-path shape plus separator strictness."""
    if not value or _RELATIVE_PATH.fullmatch(value) is None:
        raise DomainViolation(
            "PATH_REJECTED", "path must be a relative path inside an authorized root"
        )
    if "\\" in value:
        raise DomainViolation("PATH_REJECTED", "path must use '/' separators")
    return value


def _validated_task_id(task_id: str) -> str:
    """Apply the frozen contract ``id`` shape to a task identifier."""
    if not isinstance(task_id, str):
        raise DomainViolation("TASK_ID_REJECTED", "task_id must be a string id")
    if len(task_id) > 128 or _TASK_ID.fullmatch(task_id) is None:
        raise DomainViolation("TASK_ID_REJECTED", "task_id must match the contract id shape")
    return task_id


def _validated_version(version: int) -> int:
    """Versions are positive integers; bools and non-integers are rejected."""
    if isinstance(version, bool) or not isinstance(version, int):
        raise DomainViolation("VERSION_REJECTED", "version must be a positive integer")
    if version < 1:
        raise DomainViolation("VERSION_REJECTED", "version must be a positive integer")
    return version


def _assert_disjoint(first: Path, second: Path, first_label: str, second_label: str) -> None:
    """Two roots must be separate: neither may contain the other."""
    if first == second or first in second.parents or second in first.parents:
        raise DomainViolation(
            "BOUNDARY_OVERLAP",
            f"{first_label} and {second_label} must be separate roots",
        )


def _copy_hashed_into(source: Path, writer: BinaryIO) -> tuple[str, int]:
    """Copy ``source`` into an open stream while computing digest and size.

    Durability (flush and fsync) is handled once by the caller for both
    publication paths.
    """
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader:
        for chunk in iter(lambda: reader.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
            writer.write(chunk)
    return digest.hexdigest(), size


def _digest_of(path: Path) -> tuple[str, int]:
    """Read a file and return its digest and size without writing anything."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as reader:
        for chunk in iter(lambda: reader.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _resource_id_for(rel_path: str) -> str:
    """A stable id derived from the published path (paths are unique)."""
    return "res-" + hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class ResourceEntry:
    """One registry entry: the domain resource plus its owning task version."""

    resource: Resource
    task_id: str
    version: int


class ResourceRegistry:
    """Hash/size registry in backend-internal storage, rewritten atomically."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def entries(self) -> tuple[ResourceEntry, ...]:
        """All entries, in registry order."""
        if not self._path.exists():
            return ()
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        return tuple(
            ResourceEntry(
                resource=Resource.model_validate(item["resource"]),
                task_id=item["task_id"],
                version=item["version"],
            )
            for item in payload["resources"]
        )

    def get(self, resource_id: str) -> ResourceEntry:
        """One entry, or ``RESOURCE_NOT_FOUND``."""
        for entry in self.entries():
            if entry.resource.resource_id == resource_id:
                return entry
        raise DomainViolation("RESOURCE_NOT_FOUND", f"resource {resource_id} is not registered")

    def add(self, entry: ResourceEntry) -> Resource:
        """Register one new resource; duplicate ids are a conflict."""
        entries = list(self.entries())
        if any(item.resource.resource_id == entry.resource.resource_id for item in entries):
            raise DomainViolation(
                "ALREADY_EXISTS", f"resource {entry.resource.resource_id} is already registered"
            )
        entries.append(entry)
        self._save(entries)
        return entry.resource

    def replace(self, resource: Resource) -> Resource:
        """Write back an updated lifecycle for an existing entry."""
        entries = list(self.entries())
        for index, entry in enumerate(entries):
            if entry.resource.resource_id == resource.resource_id:
                entries[index] = ResourceEntry(
                    resource=resource, task_id=entry.task_id, version=entry.version
                )
                self._save(entries)
                return resource
        raise DomainViolation(
            "RESOURCE_NOT_FOUND", f"resource {resource.resource_id} is not registered"
        )

    def _save(self, entries: list[ResourceEntry]) -> None:
        payload = {
            "registry_version": REGISTRY_VERSION,
            "resources": [
                {
                    "task_id": entry.task_id,
                    "version": entry.version,
                    "resource": entry.resource.model_dump(mode="json"),
                }
                for entry in entries
            ],
        }
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self._path.parent, prefix="registry-", delete=False
        )
        try:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, self._path)


class ResourceStore:
    """Publishes, verifies and enumerates resources of one backend instance.

    All mutating operations (publish, delete) hold an exclusive advisory lock
    on a file in backend-internal storage for their whole read-check-write
    sequence, so registry read-modify-write and target claiming are serialized
    across threads *and* processes; the atomic rename of the registry document
    alone would not provide that.
    """

    def __init__(self, public_root: Path, internal_root: Path) -> None:
        self._public_root = public_root.expanduser().resolve()
        self._internal_root = internal_root.expanduser().resolve()
        _assert_disjoint(
            self._public_root, self._internal_root, "public workspace", "internal store"
        )
        self._internal_root.mkdir(parents=True, exist_ok=True)
        self._registry = ResourceRegistry(self._internal_root / "resource-registry.json")
        self._lock_path = self._internal_root / "resource-store.lock"

    @property
    def public_root(self) -> Path:
        """The resolved public workspace root."""
        return self._public_root

    @property
    def internal_root(self) -> Path:
        """The resolved backend-internal storage root."""
        return self._internal_root

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Hold the store-wide exclusive lock for one whole mutation.

        ``flock`` works between processes and between threads that open the
        lock file separately, so every mutating sequence - including another
        ``ResourceStore`` instance over the same roots - is serialized.
        """
        handle = self._lock_path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    # ------------------------------------------------------------------
    # Workspace boundaries
    # ------------------------------------------------------------------

    @staticmethod
    def workspace_ref(task_id: str, version: int) -> str:
        """The authoritative public ref of one task version.

        Task id and version are validated against the contract shapes before
        any path is built, so a malformed identifier can never reach the
        filesystem.
        """
        return f"projects/{_validated_task_id(task_id)}/v{_validated_version(version)}"

    def workspace_path(self, task_id: str, version: int) -> Path:
        """The absolute workspace directory of one task version."""
        return self._public_root / self.workspace_ref(task_id, version)

    def ensure_workspace(self, task_id: str, version: int) -> Path:
        """Create the workspace directory if needed, without symlink tricks.

        Identifiers are validated before the first filesystem call, so a
        traversal-shaped task id never creates a directory outside the
        authorized root.
        """
        workspace = self.workspace_path(task_id, version)
        workspace.mkdir(parents=True, exist_ok=True)
        if workspace.resolve() != workspace:
            raise DomainViolation(
                "PATH_REJECTED",
                f"workspace {self.workspace_ref(task_id, version)} must not be a symlink",
            )
        return workspace

    def _prepare_target(self, task_id: str, version: int, rel_path: str) -> Path:
        """Validate and resolve a publish target inside its own workspace."""
        rel = _validated_relative_path(rel_path)
        prefix = f"{self.workspace_ref(task_id, version)}/"
        if not rel.startswith(prefix):
            raise DomainViolation("PATH_CROSSES_TASK", f"path must stay inside {prefix[:-1]}")
        candidate = self._public_root / rel
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self._public_root):
            raise DomainViolation("PATH_REJECTED", f"path {rel} leaves the public workspace")
        if resolved != candidate:
            raise DomainViolation("PATH_REJECTED", f"path {rel} must not pass through symlinks")
        if candidate.exists():
            raise DomainViolation("ALREADY_EXISTS", f"path {rel} is already published")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if candidate.resolve() != candidate:
            raise DomainViolation("PATH_REJECTED", f"path {rel} must not pass through symlinks")
        return candidate

    # ------------------------------------------------------------------
    # Publishing (atomic)
    # ------------------------------------------------------------------

    def publish_bytes(
        self,
        task_id: str,
        version: int,
        rel_path: str,
        data: bytes,
        *,
        kind: ResourceKind,
        mime: str,
        simulation: bool = True,
    ) -> Resource:
        """Publish in-memory content, e.g. a frame, trajectory or report file."""
        return self._publish(
            task_id, version, rel_path, kind=kind, mime=mime, simulation=simulation, data=data
        )

    def publish_file(
        self,
        task_id: str,
        version: int,
        rel_path: str,
        source: Path,
        *,
        kind: ResourceKind,
        mime: str,
        simulation: bool = True,
    ) -> Resource:
        """Publish a staged file; the source file itself is left untouched."""
        if not source.is_file():
            raise DomainViolation("PUBLISH_SOURCE", f"staged file {source.name} does not exist")
        return self._publish(
            task_id,
            version,
            rel_path,
            kind=kind,
            mime=mime,
            simulation=simulation,
            copy_from=source,
        )

    def publish_input_copy(
        self,
        task_id: str,
        version: int,
        original: Path,
        *,
        mime: str,
        rel_path: str | None = None,
        simulation: bool = True,
    ) -> Resource:
        """Register a task copy of a user original, leaving the original alone.

        The original must live outside the public workspace; only the copy is
        published and only the copy is ever deletable.
        """
        resolved = original.expanduser().resolve()
        if not resolved.is_file():
            raise DomainViolation("PUBLISH_SOURCE", f"original {original.name} does not exist")
        if resolved.is_relative_to(self._public_root):
            raise DomainViolation(
                "ORIGINAL_PRESERVED", "an original must live outside the public workspace"
            )
        target = rel_path or f"{self.workspace_ref(task_id, version)}/records/{original.name}"
        return self._publish(
            task_id,
            version,
            target,
            kind="input_copy",
            mime=mime,
            simulation=simulation,
            copy_from=resolved,
        )

    def _publish(
        self,
        task_id: str,
        version: int,
        rel_path: str,
        *,
        kind: ResourceKind,
        mime: str,
        simulation: bool,
        data: bytes | None = None,
        copy_from: Path | None = None,
    ) -> Resource:
        """One atomic, locked publication attempt.

        The whole sequence runs under the store-wide exclusive lock: target
        claim, staging, move and registry write are one unit. Staging uses
        ``mkstemp``, which creates a new file exclusively and never follows or
        truncates a pre-existing path, so a planted staging symlink cannot
        redirect the write into a user original. Cleanup only removes files
        this attempt created: the unique staging file while it still exists,
        or the target after this attempt moved its own file there - never a
        file another publication may have won.
        """
        if (data is None) == (copy_from is None):
            raise DomainViolation("PUBLISH_SOURCE", "publish needs exactly one source")
        with self._exclusive():
            candidate = self._prepare_target(task_id, version, rel_path)
            descriptor, staging_name = tempfile.mkstemp(
                dir=candidate.parent, prefix=f".{candidate.name}.", suffix=".partial"
            )
            staging = Path(staging_name)
            replaced = False
            try:
                os.chmod(staging, 0o644)
                with os.fdopen(descriptor, "wb") as writer:
                    if data is not None:
                        digest = hashlib.sha256(data)
                        writer.write(data)
                        sha, size = digest.hexdigest(), len(data)
                    else:
                        sha, size = _copy_hashed_into(cast(Path, copy_from), writer)
                    # Both publication paths are durable before the move.
                    writer.flush()
                    os.fsync(writer.fileno())
                os.replace(staging, candidate)
                replaced = True
                resource = Resource(
                    resource_id=_resource_id_for(rel_path),
                    path=rel_path,
                    mime=mime,
                    sha256=sha,
                    size_bytes=size,
                    kind=kind,
                    simulation=simulation,
                )
                self._registry.add(
                    ResourceEntry(resource=resource, task_id=task_id, version=version)
                )
                return resource
            except BaseException:
                if replaced:
                    candidate.unlink(missing_ok=True)
                else:
                    staging.unlink(missing_ok=True)
                raise

    # ------------------------------------------------------------------
    # Enumeration and integrity
    # ------------------------------------------------------------------

    def resource(self, resource_id: str) -> Resource:
        """One registered resource."""
        return self._registry.get(resource_id).resource

    def resources(self, task_id: str, version: int | None = None) -> tuple[Resource, ...]:
        """All registered resources of a task, optionally one version only.

        This is the enumeration a later delete/export handler consumes.
        """
        return tuple(
            entry.resource
            for entry in self._registry.entries()
            if entry.task_id == task_id and (version is None or entry.version == version)
        )

    def resource_path(self, resource: Resource) -> Path:
        """The absolute path of a resource, resolved without symlink escapes."""
        candidate = self._public_root / _validated_relative_path(resource.path)
        resolved = candidate.resolve()
        if resolved != candidate or not resolved.is_relative_to(self._public_root):
            raise DomainViolation(
                "PATH_REJECTED",
                f"resource {resource.resource_id} no longer resolves inside the workspace",
            )
        return candidate

    def verify(self, resource_id: str) -> Resource:
        """Recompute hash and size; a mismatch refuses the resource."""
        resource = self.resource(resource_id)
        path = self.resource_path(resource)
        if not path.is_file():
            raise DomainViolation("RESOURCE_MISSING", f"resource {resource_id} has no file")
        actual, size = _digest_of(path)
        if actual != resource.sha256 or size != resource.size_bytes:
            raise DomainViolation(
                "INTEGRITY_FAILED", f"resource {resource_id} does not match its registered hash"
            )
        return resource

    # ------------------------------------------------------------------
    # Deletion: backend-owned files only, originals never
    # ------------------------------------------------------------------

    def delete(self, resource_id: str, *, user_requested: bool = False) -> Resource:
        """Remove one resource file and record its lifecycle truthfully.

        Files outside the public workspace are user-owned originals and are
        never deleted here; ``input_copy`` files inside the workspace still need
        the explicit user request enforced by the domain model. A file that
        already vanished is recorded as ``not_found``, and a failed removal is
        recorded as ``delete_failed`` so a delete handler can report per-item
        results (EX-098).
        """
        with self._exclusive():
            entry = self._registry.get(resource_id)
            inside = False
            try:
                rel = _validated_relative_path(entry.resource.path)
                candidate = self._public_root / rel
                resolved = candidate.resolve()
                workspace_prefix = f"{self.workspace_ref(entry.task_id, entry.version)}/"
                inside = (
                    resolved == candidate
                    and resolved.is_relative_to(self._public_root)
                    and rel.startswith(workspace_prefix)
                )
            except DomainViolation:
                inside = False
            if not inside:
                raise DomainViolation(
                    "ORIGINAL_PRESERVED",
                    f"resource {resource_id} is not inside its own public workspace; "
                    "it is never deleted here",
                )
            resource = entry.resource.with_lifecycle("deleted", user_requested=user_requested)
            if not candidate.exists():
                return self._registry.replace(
                    resource.model_copy(update={"lifecycle": "not_found"})
                )
            try:
                candidate.unlink()
            except OSError as error:
                failed = resource.model_copy(update={"lifecycle": "delete_failed"})
                self._registry.replace(failed)
                raise DomainViolation(
                    "DELETE_FAILED", f"resource {resource_id} could not be removed"
                ) from error
            return self._registry.replace(resource)


__all__ = [
    "REGISTRY_VERSION",
    "ResourceEntry",
    "ResourceRegistry",
    "ResourceStore",
]
