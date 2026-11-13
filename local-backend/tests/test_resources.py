"""Resource store tests for path safety, atomic publish and deletion (BE-007).

The cases cover the public workspace boundary: relative refs under authorized
roots, traversal/absolute/drive/backslash/symlink escape rejection, cross-task
paths, the hash/size registry, atomic publish with rollback of half-published
files, enumeration for later deletion, and the rule that user-owned originals
are never deleted or overwritten. Everything runs in a temporary directory with
small synthetic payloads; no media, network or credentials are involved.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.domain.models import DomainViolation, Resource
from tennis_backend.storage.resources import REGISTRY_VERSION, ResourceStore

REPO_ROOT = Path(__file__).resolve().parents[1]
TASK = "task-1"
VERSION = 1


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """A public workspace root and a separate internal store root."""
    public = tmp_path / "public-workspace"
    internal = tmp_path / "internal-store"
    public.mkdir()
    internal.mkdir()
    return public, internal


@pytest.fixture
def store(roots: tuple[Path, Path]) -> ResourceStore:
    return ResourceStore(public_root=roots[0], internal_root=roots[1])


def publish_report(store: ResourceStore, *, name: str = "index.html") -> Resource:
    return store.publish_bytes(
        TASK,
        VERSION,
        f"projects/{TASK}/v{VERSION}/public/report/{name}",
        b"<html>fixture report</html>",
        kind="report",
        mime="text/html",
    )


def original_file(
    tmp_path: Path, name: str = "clip.mp4", payload: bytes = b"original-bytes"
) -> Path:
    """A user-owned original outside any backend root."""
    original = tmp_path / name
    original.write_bytes(payload)
    return original


# ---------------------------------------------------------------------------
# Workspace refs and the absence of a required /public directory
# ---------------------------------------------------------------------------


def test_workspace_ref_is_projects_task_version(store: ResourceStore) -> None:
    assert store.workspace_ref(TASK, VERSION) == f"projects/{TASK}/v{VERSION}"
    assert store.workspace_path(TASK, VERSION) == store.public_root / f"projects/{TASK}/v{VERSION}"


def test_skills_choose_their_own_layout_inside_the_workspace(
    store: ResourceStore,
) -> None:
    report = store.publish_bytes(
        TASK,
        VERSION,
        f"projects/{TASK}/v{VERSION}/public/report/index.html",
        b"<html>ok</html>",
        kind="report",
        mime="text/html",
    )
    record = store.publish_bytes(
        TASK,
        VERSION,
        f"projects/{TASK}/v{VERSION}/records/notes.json",
        b"{}",
        kind="record",
        mime="application/json",
    )
    assert report.path.endswith("/public/report/index.html")
    assert record.path.endswith("/records/notes.json")
    assert (store.public_root / record.path).is_file()


# ---------------------------------------------------------------------------
# Publishing, hashing and integrity
# ---------------------------------------------------------------------------


def test_published_resource_is_registered_with_hash_and_size(
    store: ResourceStore,
) -> None:
    published = publish_report(store)
    assert published.kind == "report"
    assert published.sha256 == hashlib.sha256(b"<html>fixture report</html>").hexdigest()
    assert published.size_bytes == len(b"<html>fixture report</html>")
    assert published.lifecycle == "retained"
    assert store.verify(published.resource_id) == published


def test_publish_file_keeps_the_source_untouched(store: ResourceStore, tmp_path: Path) -> None:
    staged = tmp_path / "staged-frame.jpg"
    staged.write_bytes(b"jpeg-bytes")
    before = staged.read_bytes()
    published = store.publish_file(
        TASK,
        VERSION,
        f"projects/{TASK}/v{VERSION}/public/report/frame.jpg",
        staged,
        kind="evidence_frame",
        mime="image/jpeg",
    )
    assert staged.read_bytes() == before
    assert (store.public_root / published.path).read_bytes() == b"jpeg-bytes"


def test_republishing_the_same_path_is_rejected(store: ResourceStore) -> None:
    publish_report(store)
    with pytest.raises(DomainViolation) as raised:
        publish_report(store, name="index.html")
    assert raised.value.code == "DOMAIN_ALREADY_EXISTS"


def test_tampering_is_detected_by_the_registered_hash(store: ResourceStore) -> None:
    published = publish_report(store)
    (store.public_root / published.path).write_bytes(b"<html>tampered</html>")
    with pytest.raises(DomainViolation) as raised:
        store.verify(published.resource_id)
    assert raised.value.code == "DOMAIN_INTEGRITY_FAILED"


def test_registry_lives_outside_the_public_workspace(
    store: ResourceStore, roots: tuple[Path, Path]
) -> None:
    public, internal = roots
    publish_report(store)
    registry_files = [path for path in internal.iterdir() if path.is_file()]
    assert registry_files, "the registry should exist in internal storage"
    assert all(public not in path.parents and path != public for path in registry_files)
    assert not list(public.rglob("resource-registry.json"))


def test_roots_may_not_contain_each_other(tmp_path: Path) -> None:
    public = tmp_path / "outer" / "public"
    internal = tmp_path / "outer"
    public.mkdir(parents=True)
    internal.mkdir(exist_ok=True)
    with pytest.raises(DomainViolation) as raised:
        ResourceStore(public_root=public, internal_root=internal)
    assert raised.value.code == "DOMAIN_BOUNDARY_OVERLAP"


# ---------------------------------------------------------------------------
# Path safety: traversal, absolute, drive, backslash, symlink, cross-task
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel_path", "expected_code"),
    [
        ("/etc/escape.html", "DOMAIN_PATH_REJECTED"),
        (f"projects/{TASK}/v{VERSION}/../../escape.html", "DOMAIN_PATH_REJECTED"),
        (f"projects/{TASK}/v{VERSION}/../other-v/escape.html", "DOMAIN_PATH_REJECTED"),
        ("C:/escape.html", "DOMAIN_PATH_REJECTED"),
        (f"projects\\{TASK}\\v{VERSION}\\escape.html", "DOMAIN_PATH_REJECTED"),
        ("", "DOMAIN_PATH_REJECTED"),
        ("etc/escape.html", "DOMAIN_PATH_CROSSES_TASK"),
        ("projects/task-2/v1/public/report/index.html", "DOMAIN_PATH_CROSSES_TASK"),
    ],
)
def test_unsafe_paths_are_rejected(store: ResourceStore, rel_path: str, expected_code: str) -> None:
    with pytest.raises(DomainViolation) as raised:
        store.publish_bytes(TASK, VERSION, rel_path, b"x", kind="report", mime="text/html")
    assert raised.value.code == expected_code


def test_symlink_escape_is_rejected(store: ResourceStore, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    workspace = store.ensure_workspace(TASK, VERSION)
    link = workspace / "escape.html"
    link.symlink_to(outside)
    with pytest.raises(DomainViolation) as raised:
        store.publish_bytes(
            TASK,
            VERSION,
            f"projects/{TASK}/v{VERSION}/escape.html",
            b"x",
            kind="report",
            mime="text/html",
        )
    assert raised.value.code == "DOMAIN_PATH_REJECTED"
    assert outside.read_bytes() == b"outside"


def test_symlinked_parent_directory_is_rejected(store: ResourceStore, tmp_path: Path) -> None:
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    workspace = store.ensure_workspace(TASK, VERSION)
    (workspace / "public").mkdir()
    (workspace / "public" / "leaked").symlink_to(outside_dir)
    with pytest.raises(DomainViolation):
        store.publish_bytes(
            TASK,
            VERSION,
            f"projects/{TASK}/v{VERSION}/public/leaked/report.html",
            b"x",
            kind="report",
            mime="text/html",
        )
    assert not (outside_dir / "report.html").exists()


# ---------------------------------------------------------------------------
# Malformed task identifiers never reach the filesystem
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "task_id",
    ["../../evil", "../task-2", "task/1", "", ".hidden", "-leading", "x" * 129],
)
def test_malformed_task_ids_are_rejected(store: ResourceStore, task_id: str) -> None:
    with pytest.raises(DomainViolation):
        store.workspace_ref(task_id, VERSION)
    with pytest.raises(DomainViolation):
        store.ensure_workspace(task_id, VERSION)
    with pytest.raises(DomainViolation):
        store.publish_bytes(
            task_id,
            VERSION,
            f"projects/{task_id}/v{VERSION}/records/x.json",
            b"x",
            kind="record",
            mime="application/json",
        )


@pytest.mark.parametrize("version", [0, -1])
def test_malformed_versions_are_rejected(store: ResourceStore, version: int) -> None:
    with pytest.raises(DomainViolation):
        store.ensure_workspace(TASK, version)


@pytest.mark.parametrize("version", [2.5, True, "1", None])
def test_non_integer_versions_are_rejected(store: ResourceStore, version: Any) -> None:
    with pytest.raises(DomainViolation) as raised:
        store.ensure_workspace(TASK, version)
    assert raised.value.code == "DOMAIN_VERSION_REJECTED"


def test_non_string_task_id_is_rejected_without_a_type_error(store: ResourceStore) -> None:
    with pytest.raises(DomainViolation) as raised:
        store.workspace_ref(123, VERSION)  # type: ignore[arg-type]
    assert raised.value.code == "DOMAIN_TASK_ID_REJECTED"


def test_malformed_task_id_creates_nothing_outside_the_root(
    store: ResourceStore, roots: tuple[Path, Path]
) -> None:
    public, _internal = roots
    with pytest.raises(DomainViolation):
        store.ensure_workspace("../../evil", VERSION)
    with pytest.raises(DomainViolation):
        store.ensure_workspace("../task-9/sub", VERSION)
    assert list(public.iterdir()) == []
    assert not (public.parent / "evil").exists()
    assert not (public.parent / "task-9").exists()


def test_bytes_are_flushed_and_fsynced_before_the_atomic_move(
    store: ResourceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The published content must be durable before the target is replaced.

    Observed through the filesystem, not through internals: a spy on the move
    records the staged bytes at the moment of the move, and a spy on fsync
    records that a durability sync happened before that move.
    """
    rel_path = f"projects/{TASK}/v{VERSION}/records/order.json"
    target = store.public_root / rel_path
    real_fsync = os.fsync
    real_replace = os.replace
    events: list[Any] = []

    def spy_fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    def spy_replace(source: Any, destination: Any) -> None:
        if Path(str(destination)).name == target.name:
            events.append(
                {
                    "staged_bytes": Path(str(source)).read_bytes(),
                    "target_existed": Path(str(destination)).exists(),
                }
            )
        real_replace(source, destination)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    store.publish_bytes(TASK, VERSION, rel_path, b"{}", kind="record", mime="application/json")
    monkeypatch.undo()

    moves = [event for event in events if isinstance(event, dict)]
    assert moves == [{"staged_bytes": b"{}", "target_existed": False}]
    assert events.index("fsync") < events.index(moves[0])
    assert target.read_bytes() == b"{}"


def test_cross_task_paths_are_rejected(store: ResourceStore) -> None:
    with pytest.raises(DomainViolation) as raised:
        store.publish_bytes(
            TASK,
            VERSION,
            "projects/task-2/v1/public/report/index.html",
            b"x",
            kind="report",
            mime="text/html",
        )
    assert raised.value.code == "DOMAIN_PATH_CROSSES_TASK"
    assert store.resources("task-2") == ()
    assert not (store.public_root / "projects/task-2").exists()


def test_unregistered_version_prefix_is_rejected(store: ResourceStore) -> None:
    with pytest.raises(DomainViolation):
        store.publish_bytes(
            TASK,
            VERSION,
            f"projects/{TASK}/v2/public/report/index.html",
            b"x",
            kind="report",
            mime="text/html",
        )


def test_unknown_version_prefix_is_rejected(store: ResourceStore) -> None:
    with pytest.raises(DomainViolation):
        store.publish_bytes(
            TASK,
            VERSION,
            f"projects/{TASK}/v2/public/report/index.html",
            b"x",
            kind="report",
            mime="text/html",
        )


# ---------------------------------------------------------------------------
# Atomic publish: a failed registration leaves nothing behind
# ---------------------------------------------------------------------------


def test_failed_registration_rolls_the_publish_back(
    store: ResourceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    published_path = store.public_root / f"projects/{TASK}/v{VERSION}/public/report/index.html"

    def broken_save(entries: Any) -> None:
        raise OSError("registry write failed")

    monkeypatch.setattr(store._registry, "_save", broken_save)
    with pytest.raises(OSError):
        publish_report(store)
    assert not published_path.exists()
    leftovers = [path.name for path in published_path.parent.iterdir()]
    assert leftovers == [], leftovers
    monkeypatch.undo()
    recovered = publish_report(store)
    assert (store.public_root / recovered.path).is_file()


def test_staged_source_missing_is_rejected(store: ResourceStore, tmp_path: Path) -> None:
    with pytest.raises(DomainViolation):
        store.publish_file(
            TASK,
            VERSION,
            f"projects/{TASK}/v{VERSION}/public/report/missing.jpg",
            tmp_path / "nope.jpg",
            kind="evidence_frame",
            mime="image/jpeg",
        )


# ---------------------------------------------------------------------------
# Staging symlink trap: a planted staging path must never touch an original
# ---------------------------------------------------------------------------


def _plant_staging_symlink(store: ResourceStore, target: str, original: Path) -> Path:
    """Pre-create the predictable staging name as a symlink to an original."""
    workspace = store.ensure_workspace(TASK, VERSION)
    link = workspace / Path(target).name
    staged_link = link.parent / f".{link.name}.partial"
    staged_link.parent.mkdir(parents=True, exist_ok=True)
    staged_link.symlink_to(original)
    return staged_link


def test_publish_bytes_ignores_a_planted_staging_symlink(
    store: ResourceStore, tmp_path: Path
) -> None:
    original = tmp_path / "original.txt"
    original.write_text("KEEP")
    planted = _plant_staging_symlink(
        store, f"projects/{TASK}/v{VERSION}/public/report/frame.png", original
    )
    published = store.publish_bytes(
        TASK,
        VERSION,
        f"projects/{TASK}/v{VERSION}/public/report/frame.png",
        b"CHANGED",
        kind="evidence_frame",
        mime="image/png",
    )
    target = store.public_root / published.path
    assert original.read_text() == "KEEP"
    assert target.is_file() and not target.is_symlink()
    assert target.read_bytes() == b"CHANGED"
    assert stored_link_untouched(planted, original)


def test_publish_file_ignores_a_planted_staging_symlink(
    store: ResourceStore, tmp_path: Path
) -> None:
    original = tmp_path / "original.txt"
    original.write_text("KEEP")
    staged = tmp_path / "frame.jpg"
    staged.write_bytes(b"jpeg-bytes")
    _plant_staging_symlink(store, f"projects/{TASK}/v{VERSION}/public/report/frame.jpg", original)
    published = store.publish_file(
        TASK,
        VERSION,
        f"projects/{TASK}/v{VERSION}/public/report/frame.jpg",
        staged,
        kind="evidence_frame",
        mime="image/jpeg",
    )
    target = store.public_root / published.path
    assert original.read_text() == "KEEP"
    assert target.is_file() and not target.is_symlink()
    assert target.read_bytes() == b"jpeg-bytes"


def test_publish_input_copy_ignores_a_planted_staging_symlink(
    store: ResourceStore, tmp_path: Path
) -> None:
    outside_original = tmp_path / "original.txt"
    outside_original.write_text("KEEP")
    user_original = tmp_path / "clip.mp4"
    user_original.write_bytes(b"original-bytes")
    _plant_staging_symlink(store, f"projects/{TASK}/v{VERSION}/records/clip.mp4", outside_original)
    published = store.publish_input_copy(TASK, VERSION, user_original, mime="video/mp4")
    target = store.public_root / published.path
    assert outside_original.read_text() == "KEEP"
    assert target.is_file() and not target.is_symlink()
    assert target.read_bytes() == b"original-bytes"
    assert user_original.read_bytes() == b"original-bytes"


def stored_link_untouched(planted: Path, original: Path) -> bool:
    """The planted link still points at the intact original."""
    return planted.is_symlink() and Path(os.readlink(planted)) == original and original.exists()


# ---------------------------------------------------------------------------
# Concurrency: one winner per path, all distinct resources enumerated
# ---------------------------------------------------------------------------


def test_concurrent_distinct_publishes_are_all_enumerated(store: ResourceStore) -> None:
    paths = [f"projects/{TASK}/v{VERSION}/records/n{index}.json" for index in range(6)]

    def publish(path: str) -> Resource:
        return store.publish_bytes(
            TASK, VERSION, path, path.encode("utf-8"), kind="record", mime="application/json"
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        published = list(pool.map(publish, paths))
    assert len({item.resource_id for item in published}) == len(paths)
    enumerated = store.resources(TASK, VERSION)
    assert {item.path for item in enumerated} == set(paths)
    for item in published:
        assert item.path.encode("utf-8") == (store.public_root / item.path).read_bytes()
        assert store.verify(item.resource_id) == item


def test_concurrent_same_path_publish_has_exactly_one_winner(store: ResourceStore) -> None:
    rel_path = f"projects/{TASK}/v{VERSION}/records/race.json"
    payloads = [b"from-a", b"from-b"]
    barrier = threading.Barrier(len(payloads))
    results: list[tuple[bytes, Resource]] = []
    errors: list[DomainViolation] = []
    lock = threading.Lock()

    def worker(payload: bytes) -> None:
        barrier.wait()
        try:
            published = store.publish_bytes(
                TASK, VERSION, rel_path, payload, kind="record", mime="application/json"
            )
            with lock:
                results.append((payload, published))
        except DomainViolation as error:
            with lock:
                errors.append(error)

    threads = [threading.Thread(target=worker, args=(payload,)) for payload in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 1, [(item[0], item[1].sha256) for item in results]
    assert len(errors) == 1 and errors[0].code == "DOMAIN_ALREADY_EXISTS"
    winner_payload, winner = results[0]
    target = store.public_root / winner.path
    assert target.read_bytes() == winner_payload
    assert store.verify(winner.resource_id).sha256 == winner.sha256
    assert len(store.resources(TASK, VERSION)) == 1
    # The losing attempt must not have deleted or replaced the winner's file.
    assert winner_payload in (b"from-a", b"from-b")
    assert len(list(target.parent.glob(".*partial"))) == 0


def test_cross_process_publishes_serialize_on_the_same_roots(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    """Two real processes publishing the same path: one winner, one refusal."""
    public, internal = roots
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from tennis_backend.domain.models import DomainViolation\n"
        "from tennis_backend.storage.resources import ResourceStore\n"
        "store = ResourceStore(public_root=Path(sys.argv[1]), internal_root=Path(sys.argv[2]))\n"
        "payload = sys.argv[3].encode()\n"
        "try:\n"
        "    store.publish_bytes('task-1', 1, 'projects/task-1/v1/records/race.json',"
        " payload, kind='record', mime='application/json')\n"
        "    print('WIN')\n"
        "except DomainViolation as error:\n"
        "    print(error.code)\n"
    )
    outcomes: dict[str, str] = {}
    running = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(public), str(internal), payload],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for payload in ("from-a", "from-b")
    ]
    for payload, process in zip(("from-a", "from-b"), running, strict=True):
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr
        outcomes[payload] = stdout.strip()

    assert sorted(outcomes.values()) == ["DOMAIN_ALREADY_EXISTS", "WIN"]
    winner_payload = next(payload for payload, outcome in outcomes.items() if outcome == "WIN")
    store = ResourceStore(public_root=public, internal_root=internal)
    enumerated = store.resources(TASK, VERSION)
    assert len(enumerated) == 1
    target = store.public_root / enumerated[0].path
    assert target.read_bytes() == winner_payload.encode()
    assert store.verify(enumerated[0].resource_id) == enumerated[0]


def test_cross_process_distinct_publishes_are_all_enumerated(
    roots: tuple[Path, Path],
) -> None:
    public, internal = roots
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from tennis_backend.storage.resources import ResourceStore\n"
        "store = ResourceStore(public_root=Path(sys.argv[1]), internal_root=Path(sys.argv[2]))\n"
        "name = sys.argv[3]\n"
        "store.publish_bytes('task-1', 1, f'projects/task-1/v1/records/{name}.json',"
        " name.encode(), kind='record', mime='application/json')\n"
        "print('OK')\n"
    )
    for name in ("alpha", "beta"):
        process = subprocess.run(
            [sys.executable, "-c", script, str(public), str(internal), name],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert process.returncode == 0, process.stderr
        assert process.stdout.strip() == "OK"
    store = ResourceStore(public_root=public, internal_root=internal)
    assert {item.path for item in store.resources(TASK, VERSION)} == {
        f"projects/{TASK}/v1/records/alpha.json",
        f"projects/{TASK}/v1/records/beta.json",
    }


# ---------------------------------------------------------------------------
# Enumeration for later deletion
# ---------------------------------------------------------------------------


def test_resources_are_enumerable_per_task_and_version(store: ResourceStore) -> None:
    publish_report(store)
    store.publish_bytes(
        TASK,
        2,
        f"projects/{TASK}/v2/records/notes.json",
        b"{}",
        kind="record",
        mime="application/json",
    )
    everything = store.resources(TASK)
    assert [resource.path for resource in everything] == [
        f"projects/{TASK}/v{VERSION}/public/report/index.html",
        f"projects/{TASK}/v2/records/notes.json",
    ]
    assert [resource.path for resource in store.resources(TASK, VERSION)] == [
        f"projects/{TASK}/v{VERSION}/public/report/index.html"
    ]
    assert store.resources("task-other") == ()
    assert store.resource(everything[0].resource_id).kind == "report"


def test_unknown_resource_ids_are_rejected(store: ResourceStore) -> None:
    with pytest.raises(DomainViolation) as raised:
        store.resource("res-missing")
    assert raised.value.code == "DOMAIN_RESOURCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Deletion: backend-owned files only, originals never
# ---------------------------------------------------------------------------


def test_derived_resources_delete_and_the_record_lifecycle_updates(
    store: ResourceStore,
) -> None:
    published = publish_report(store)
    path = store.public_root / published.path
    deleted = store.delete(published.resource_id)
    assert deleted.lifecycle == "deleted"
    assert not path.exists()
    assert [item.lifecycle for item in store.resources(TASK)] == ["deleted"]
    with pytest.raises(DomainViolation) as raised:
        store.delete(published.resource_id)
    assert raised.value.code == "DOMAIN_ALREADY_DELETED"


def test_input_copy_needs_an_explicit_user_request(store: ResourceStore, tmp_path: Path) -> None:
    original = original_file(tmp_path)
    published = store.publish_input_copy(TASK, VERSION, original, mime="video/mp4")
    with pytest.raises(DomainViolation) as raised:
        store.delete(published.resource_id)
    assert raised.value.code == "DOMAIN_ORIGINAL_PRESERVED"
    assert original.read_bytes() == b"original-bytes"
    deleted = store.delete(published.resource_id, user_requested=True)
    assert deleted.lifecycle == "deleted"
    assert not (store.public_root / published.path).exists()
    assert original.read_bytes() == b"original-bytes"


def test_originals_are_copied_not_moved_and_live_outside_the_workspace(
    store: ResourceStore, tmp_path: Path
) -> None:
    original = original_file(tmp_path)
    published = store.publish_input_copy(TASK, VERSION, original, mime="video/mp4")
    assert published.kind == "input_copy"
    assert published.path == f"projects/{TASK}/v{VERSION}/records/clip.mp4"
    assert original.is_file()
    assert (
        store.verify(published.resource_id).sha256 == hashlib.sha256(b"original-bytes").hexdigest()
    )


def test_an_original_inside_the_workspace_is_refused(store: ResourceStore) -> None:
    inside = store.ensure_workspace(TASK, VERSION) / "already-inside.mp4"
    inside.write_bytes(b"x")
    with pytest.raises(DomainViolation) as raised:
        store.publish_input_copy(TASK, VERSION, inside, mime="video/mp4")
    assert raised.value.code == "DOMAIN_ORIGINAL_PRESERVED"


def test_registry_entries_outside_the_public_root_are_never_deleted(
    store: ResourceStore, tmp_path: Path
) -> None:
    """A registry path that cannot be proven inside the workspace is refused."""
    published = publish_report(store)
    forged = published.model_copy(update={"path": "elsewhere.html"})
    store._registry.replace(forged)
    with pytest.raises(DomainViolation) as raised:
        store.delete(published.resource_id)
    assert raised.value.code == "DOMAIN_ORIGINAL_PRESERVED"
    assert not (tmp_path / "elsewhere.html").exists()


def test_vanished_files_are_recorded_as_not_found(store: ResourceStore) -> None:
    published = publish_report(store)
    (store.public_root / published.path).unlink()
    deleted = store.delete(published.resource_id)
    assert deleted.lifecycle == "not_found"


def test_failed_removal_is_recorded_as_delete_failed(
    store: ResourceStore,
) -> None:
    published = store.publish_bytes(
        TASK,
        VERSION,
        f"projects/{TASK}/v{VERSION}/records/locked.json",
        b"{}",
        kind="record",
        mime="application/json",
    )
    target = store.public_root / published.path
    target.parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(DomainViolation) as raised:
            store.delete(published.resource_id)
        assert raised.value.code == "DOMAIN_DELETE_FAILED"
    finally:
        target.parent.chmod(stat.S_IRWXU)
    assert [item.lifecycle for item in store.resources(TASK)] == ["delete_failed"]
    target.unlink()


def test_registry_version_is_part_of_the_document(store: ResourceStore) -> None:
    import json

    publish_report(store)
    document = json.loads(
        (store.internal_root / "resource-registry.json").read_text(encoding="utf-8")
    )
    assert document["registry_version"] == REGISTRY_VERSION
    assert len(document["resources"]) == 1


def test_store_survives_reopening_with_the_same_roots(
    roots: tuple[Path, Path],
) -> None:
    first = ResourceStore(public_root=roots[0], internal_root=roots[1])
    published = publish_report(first)
    second = ResourceStore(public_root=roots[0], internal_root=roots[1])
    assert second.resources(TASK) == (published,)
    assert second.verify(published.resource_id) == published
    assert os.path.isfile(second.public_root / published.path)
