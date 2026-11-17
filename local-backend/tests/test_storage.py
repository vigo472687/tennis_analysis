"""SQLite metadata, state event and checkpoint transaction tests (BE-006).

The cases cover the persistence invariants of plan §10.2 and AC-SPEC-045: task
metadata with versions and append-only state events, atomic transition plus
checkpoint writes where an interruption rolls back completely, checkpoint
bounds inherited from the state domain, separate runtime tables, media stored
as metadata without blobs, and a recovery plan that resumes from the last
complete stage without redoing finished work. Everything runs against a
temporary SQLite file; no media bytes, network or credentials are involved.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from tennis_backend.domain.models import (
    DomainViolation,
    MediaAsset,
    MediaProbe,
    Task,
    TaskVersion,
    TimePoint,
)
from tennis_backend.domain.states import (
    MAX_CHECKPOINTS,
    PIPELINE_STATES,
    IllegalTransition,
)
from tennis_backend.storage.metadata import (
    CheckpointRecord,
    MetadataStore,
    TaskRecord,
)

UTC = dt.UTC
T0 = dt.datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


def tick(seconds: int) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


def task_version(version: int = 1, task_id: str = "task-1") -> TaskVersion:
    return TaskVersion(
        task_id=task_id,
        version=version,
        primary_route="technical_diagnosis",
        auxiliary_routes=["question_answer"],
        runtime_requirement="required",
        contract_version="2.0.0",
        config_version="fixture-config-1",
        media_id="media-1",
        consent_id="consent-1",
        simulation=True,
    )


def task(task_id: str = "task-1", state: str = "draft", stage: str = "input") -> Task:
    return Task(
        task_id=task_id,
        versions=[task_version(task_id=task_id)],
        state=state,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        simulation=True,
    )


def probe() -> MediaProbe:
    return MediaProbe(
        container="mp4",
        codec="avc1",
        duration_s=12.5,
        fps=30.0,
        width=1920,
        height=1080,
        rotation="0",
        slow_motion="no",
        hdr="no",
        device_source="phone-handheld",
        panorama="flat",
        observation_kind="measured",
    )


def media_asset(media_id: str = "media-1", **overrides: Any) -> MediaAsset:
    fields: dict[str, Any] = {
        "media_id": media_id,
        "role": "original",
        "original_ref": "projects/demo/originals/clip.mp4",
        "probe": probe(),
        "time_mapping": [
            TimePoint(normalized_s=0.0, original_s=0.0),
            TimePoint(normalized_s=1.0, original_s=1.0),
        ],
        "time_mapping_valid": True,
        "simulation": True,
    }
    fields.update(overrides)
    return MediaAsset(**fields)


@pytest.fixture
def store(tmp_path: Path) -> MetadataStore:
    """One metadata database per test, closed afterwards."""
    database = MetadataStore(tmp_path / "metadata.sqlite3")
    database.create_task(task(), at=T0)
    yield database
    database.close()


def advance(
    store: MetadataStore,
    current: str,
    target: str,
    *,
    checkpoint: str | None = None,
    digest: str | None = None,
    second: int = 1,
) -> None:
    store.record_transition(
        "task-1",
        current=current,  # type: ignore[arg-type]
        target=target,  # type: ignore[arg-type]
        reason="unit test",
        checkpoint=checkpoint,  # type: ignore[arg-type]
        checkpoint_digest=digest,
        at=tick(second),
    )


# ---------------------------------------------------------------------------
# Task metadata, versions and state events
# ---------------------------------------------------------------------------


def test_created_task_is_readable_with_versions_and_initial_event(
    store: MetadataStore,
) -> None:
    record = store.task_record("task-1")
    assert isinstance(record, TaskRecord)
    assert record.state == "draft"
    assert record.stage == "input"
    assert record.simulation is True
    assert [version.version for version in record.versions] == [1]
    assert record.versions[0].primary_route == "technical_diagnosis"
    assert record.created_at == T0
    events = store.state_events("task-1")
    assert len(events) == 1
    assert events[0].from_state == "draft"
    assert events[0].to_state == "draft"
    assert events[0].reason == "created"


def test_unknown_tasks_are_reported_without_details(store: MetadataStore) -> None:
    with pytest.raises(DomainViolation) as raised:
        store.task_record("task-missing")
    assert "task-missing" in str(raised.value)
    assert store.task_state("task-missing") is None
    assert store.task_stage("task-missing") is None


def test_duplicate_task_and_duplicate_version_are_conflicts(
    store: MetadataStore,
) -> None:
    with pytest.raises(DomainViolation):
        store.create_task(task(), at=tick(1))
    with pytest.raises(DomainViolation):
        store.add_task_version(task_version(), at=tick(2))


def test_version_can_only_be_added_to_a_stored_task(store: MetadataStore) -> None:
    with pytest.raises(DomainViolation):
        store.add_task_version(task_version(task_id="task-404"), at=tick(1))


def test_new_version_is_persisted_and_listed_in_order(store: MetadataStore) -> None:
    store.add_task_version(task_version(version=2), at=tick(1))
    record = store.task_record("task-1")
    assert [version.version for version in record.versions] == [1, 2]
    assert record.updated_at == tick(1)
    assert record.versions[1].input_digest is None


# ---------------------------------------------------------------------------
# Transitions: legal moves, mismatch guard, atomicity on interruption
# ---------------------------------------------------------------------------


def test_legal_transitions_update_state_stage_and_events(
    store: MetadataStore,
) -> None:
    advance(store, "draft", "checking_input", second=1)
    advance(store, "checking_input", "analyzing", second=2)
    assert store.task_state("task-1") == "analyzing"
    assert store.task_stage("task-1") == "analysis"
    states = [event.to_state for event in store.state_events("task-1")]
    assert states == ["draft", "checking_input", "analyzing"]


def test_illegal_transitions_leave_the_task_untouched(store: MetadataStore) -> None:
    with pytest.raises(IllegalTransition):
        advance(store, "draft", "complete")
    assert store.task_state("task-1") == "draft"
    assert len(store.state_events("task-1")) == 1


def test_stale_current_state_is_rejected(store: MetadataStore) -> None:
    advance(store, "draft", "checking_input", second=1)
    with pytest.raises(DomainViolation) as raised:
        advance(store, "draft", "analyzing", second=2)
    assert raised.value.code == "DOMAIN_STATE_MISMATCH"
    assert store.task_state("task-1") == "checking_input"


def test_interrupted_transition_rolls_back_event_and_checkpoint(
    store: MetadataStore,
) -> None:
    """A failure inside the write leaves neither event nor checkpoint behind."""
    advance(store, "draft", "checking_input", second=1)
    advance(
        store,
        "checking_input",
        "analyzing",
        checkpoint="checking_input",
        digest="d" * 64,
        second=2,
    )
    events_before = len(store.state_events("task-1"))
    with pytest.raises(DomainViolation):
        # The same stage output cannot be checkpointed twice; the unique
        # constraint fails after the event insert, so the whole unit rolls back.
        advance(
            store,
            "analyzing",
            "researching",
            checkpoint="checking_input",
            digest="e" * 64,
            second=3,
        )
    assert store.task_state("task-1") == "analyzing"
    assert store.task_stage("task-1") == "analysis"
    assert len(store.state_events("task-1")) == events_before
    assert [checkpoint.name for checkpoint in store.checkpoints("task-1")] == ["checking_input"]


def test_checkpoints_are_only_written_with_their_stage_output_digest(
    store: MetadataStore,
) -> None:
    with pytest.raises(DomainViolation):
        advance(store, "draft", "checking_input", checkpoint="checking_input", digest=None)
    advance(
        store,
        "draft",
        "checking_input",
        checkpoint=None,
        digest="a" * 64,
        second=1,
    )
    assert store.checkpoints("task-1") == ()


def test_checkpoint_bounds_follow_the_next_state(store: MetadataStore) -> None:
    advance(store, "draft", "checking_input", second=1)
    with pytest.raises(DomainViolation):
        # checking_input is not finished yet, so it cannot be checkpointed.
        advance(
            store,
            "checking_input",
            "analyzing",
            checkpoint="analyzing",
            digest="b" * 64,
            second=2,
        )
    assert store.checkpoints("task-1") == ()
    advance(
        store,
        "checking_input",
        "analyzing",
        checkpoint="checking_input",
        digest="c" * 64,
        second=3,
    )
    assert [checkpoint.name for checkpoint in store.checkpoints("task-1")] == ["checking_input"]
    assert all(isinstance(item, CheckpointRecord) for item in store.checkpoints("task-1"))


def test_each_completed_stage_is_checkpointed_once(
    store: MetadataStore,
) -> None:
    moves = [
        ("draft", "checking_input", None),
        ("checking_input", "analyzing", "checking_input"),
        ("analyzing", "researching", "analyzing"),
        ("researching", "rendering", "researching"),
    ]
    second = 1
    for current, target, checkpoint in moves:
        advance(
            store,
            current,
            target,
            checkpoint=checkpoint,
            digest=f"{checkpoint}-digest" if checkpoint else None,
            second=second,
        )
        second += 1
    advance(store, "rendering", "complete", second=second)
    record = store.task_record("task-1")
    assert record.state == "complete"
    assert record.stage == "none"
    assert [checkpoint.name for checkpoint in record.checkpoints] == [
        "checking_input",
        "analyzing",
        "researching",
    ]
    assert record.checkpoints[0].payload_digest == "checking_input-digest"


# ---------------------------------------------------------------------------
# Recovery: resume from the last complete stage (AC-SPEC-045)
# ---------------------------------------------------------------------------


def test_recovery_lists_only_stages_without_a_complete_checkpoint(
    store: MetadataStore,
) -> None:
    advance(store, "draft", "checking_input", second=1)
    advance(
        store,
        "checking_input",
        "analyzing",
        checkpoint="checking_input",
        digest="f" * 64,
        second=2,
    )
    # Interruption in the middle of analyzing: checking_input is finished and
    # must not be repeated, the rest is listed for redo.
    assert store.incomplete_stages("task-1") == ("analyzing", "researching", "rendering")


def test_fully_analysed_task_has_no_incomplete_pipeline_stages(
    store: MetadataStore,
) -> None:
    second = 1
    moves = [
        ("draft", "checking_input", None),
        ("checking_input", "analyzing", "checking_input"),
        ("analyzing", "researching", "analyzing"),
        ("researching", "rendering", "researching"),
    ]
    for current, target, checkpoint in moves:
        advance(store, current, target, checkpoint=checkpoint, digest="0" * 64, second=second)
        second += 1
    assert store.incomplete_stages("task-1") == ("rendering",)
    assert store.task_state("task-1") == "rendering"


def test_unknown_task_has_no_recovery_plan(store: MetadataStore) -> None:
    with pytest.raises(DomainViolation):
        store.incomplete_stages("task-404")


def test_checkpoint_names_come_from_the_state_domain(store: MetadataStore) -> None:
    for allowed in MAX_CHECKPOINTS.values():
        for name in allowed:
            assert name in PIPELINE_STATES


# ---------------------------------------------------------------------------
# Concurrent connections: a stale request must not overwrite a newer state
# ---------------------------------------------------------------------------


def test_stale_transition_loses_against_a_committed_interleaving(
    tmp_path: Path,
) -> None:
    """Two real connections on one file race on the same task (AC-SPEC-045).

    Connection A wants ``checking_input -> analyzing``. The moment A opens its
    write transaction, connection B commits ``checking_input -> unsupported``
    on the same database. A must re-read the stored state under the write lock,
    see that it is stale, and fail - never record its move over the settled
    state.
    """
    database = tmp_path / "race.sqlite3"
    with MetadataStore(database) as connection_a, MetadataStore(database) as connection_b:
        connection_a.create_task(task(), at=T0)
        advance(connection_a, "draft", "checking_input", second=1)
        events_before = len(connection_a.state_events("task-1"))
        checkpoints_before = connection_a.checkpoints("task-1")

        interleaved = {"done": False}

        def on_statement(statement: str) -> None:
            """Commit B's competing move while A is opening its transaction."""
            if interleaved["done"] or not statement.startswith("BEGIN IMMEDIATE"):
                return
            interleaved["done"] = True
            connection_b.record_transition(
                "task-1",
                current="checking_input",
                target="unsupported",
                reason="another worker finished first",
                at=tick(5),
            )

        connection_a._connection.set_trace_callback(on_statement)
        try:
            with pytest.raises(DomainViolation) as raised:
                advance(
                    connection_a,
                    "checking_input",
                    "analyzing",
                    checkpoint="checking_input",
                    digest="9" * 64,
                    second=2,
                )
        finally:
            connection_a._connection.set_trace_callback(None)

        assert interleaved["done"] is True
        assert raised.value.code == "DOMAIN_STATE_MISMATCH"
        # The settled verdict survives and nothing of A's attempt is stored.
        assert connection_a.task_state("task-1") == "unsupported"
        assert connection_b.task_state("task-1") == "unsupported"
        assert len(connection_a.state_events("task-1")) == events_before + 1
        assert connection_a.state_events("task-1")[-1].to_state == "unsupported"
        assert connection_a.checkpoints("task-1") == checkpoints_before
        # The rolled-back connection is usable again, and the settled task only
        # accepts local deletion.
        connection_b.record_transition(
            "task-1",
            current="unsupported",
            target="deleted",
            reason="user deleted the task",
            at=tick(6),
        )
        assert connection_a.task_state("task-1") == "deleted"
        with pytest.raises(IllegalTransition):
            advance(connection_b, "deleted", "analyzing", second=7)


def test_stale_runtime_transition_loses_against_a_committed_interleaving(
    tmp_path: Path,
) -> None:
    """The runtime compare-and-set holds under the same interleaving."""
    database = tmp_path / "race-runtime.sqlite3"
    with MetadataStore(database) as connection_a, MetadataStore(database) as connection_b:
        events_before = len(connection_a.runtime_events())
        interleaved = {"done": False}

        def on_statement(statement: str) -> None:
            if interleaved["done"] or not statement.startswith("BEGIN IMMEDIATE"):
                return
            interleaved["done"] = True
            connection_b.record_runtime_transition("awaiting_consent", reason="b first", at=T0)

        connection_a._connection.set_trace_callback(on_statement)
        try:
            with pytest.raises(DomainViolation) as raised:
                connection_a.record_runtime_transition(
                    "awaiting_consent",
                    current="not_initialized",
                    reason="a stale",
                    at=tick(1),
                )
        finally:
            connection_a._connection.set_trace_callback(None)

        assert raised.value.code == "DOMAIN_STATE_MISMATCH"
        assert connection_a.runtime_state() == "awaiting_consent"
        assert len(connection_a.runtime_events()) == events_before + 1


# ---------------------------------------------------------------------------
# Runtime domain kept in its own tables
# ---------------------------------------------------------------------------


def test_runtime_defaults_to_not_initialized_and_records_moves(
    tmp_path: Path,
) -> None:
    with MetadataStore(tmp_path / "runtime.sqlite3") as runtime:
        assert runtime.runtime_state() == "not_initialized"
        runtime.record_runtime_transition("awaiting_consent", reason="prepare asked", at=T0)
        runtime.record_runtime_transition("preparing", reason="confirmed", at=tick(1))
        assert runtime.runtime_state() == "preparing"
        assert [(move[0], move[1]) for move in runtime.runtime_events()] == [
            ("not_initialized", "awaiting_consent"),
            ("awaiting_consent", "preparing"),
        ]


def test_runtime_and_task_states_live_in_separate_tables(
    tmp_path: Path,
) -> None:
    with MetadataStore(tmp_path / "separate.sqlite3") as store:
        store.record_runtime_transition("awaiting_consent", at=T0)
        assert store.runtime_state() == "awaiting_consent"
        assert store.task_state("task-1") is None


def test_illegal_runtime_move_is_rejected_before_any_write(
    tmp_path: Path,
) -> None:
    with MetadataStore(tmp_path / "runtime2.sqlite3") as runtime:
        with pytest.raises(IllegalTransition):
            runtime.record_runtime_transition("ready", at=T0)
        assert runtime.runtime_state() == "not_initialized"
        assert runtime.runtime_events() == ()


# ---------------------------------------------------------------------------
# Media metadata only: no blobs
# ---------------------------------------------------------------------------


def test_media_is_stored_as_metadata_and_round_trips(store: MetadataStore) -> None:
    store.record_media_asset(media_asset())
    loaded = store.media_asset("media-1")
    assert loaded == media_asset()
    assert loaded.probe.container == "mp4"
    assert loaded.time_mapping[1].original_s == 1.0
    assert store.media_probe("media-1").observation_kind == "measured"


def test_media_tables_declare_no_blob_column(store: MetadataStore) -> None:
    for table in (
        "media_assets",
        "tasks",
        "task_versions",
        "task_state_events",
        "task_checkpoints",
    ):
        types = set(store.column_types(table).values())
        assert types <= {"TEXT", "INTEGER", "REAL"}, table


def test_duplicate_media_and_unknown_origin_are_conflicts(store: MetadataStore) -> None:
    store.record_media_asset(media_asset())
    with pytest.raises(DomainViolation):
        store.record_media_asset(media_asset())
    with pytest.raises(DomainViolation):
        store.record_media_asset(
            media_asset("media-2", role="standardized", derived_from="media-404")
        )


def test_derived_media_keeps_the_original_reference(store: MetadataStore) -> None:
    store.record_media_asset(media_asset())
    derivative = media_asset(
        "media-2",
        role="standardized",
        derived_from="media-1",
    )
    store.record_media_asset(derivative)
    assert store.media_asset("media-2").original_ref == store.media_asset("media-1").original_ref


def test_unknown_media_is_reported(store: MetadataStore) -> None:
    with pytest.raises(DomainViolation):
        store.media_asset("media-404")
