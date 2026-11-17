"""Evidence storage, retention and interval retrieval tests.

Synthetic frames only. The retention clock is passed in, so these tests never
depend on the wall clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tennis_backend.domain.models import DomainViolation
from tennis_backend.storage.evidence_store import (
    RETENTION,
    EvidenceFrame,
    EvidenceStore,
    StoredCandidate,
)

UPLOADED = datetime(2026, 11, 16, 9, 0, 0)


@pytest.fixture()
def store(tmp_path: Path) -> EvidenceStore:
    with EvidenceStore(tmp_path / "evidence.sqlite") as opened:
        yield opened


def frame(number: int, *, ball: bool = True, candidate: str | None = None) -> EvidenceFrame:
    return EvidenceFrame(
        frame=number,
        time_s=number / 30.0,
        ball=[10.0, 20.0, 12.0, 22.0] if ball else None,
        ball_confidence=0.62 if ball else None,
        racket=[100.0, 100.0, 160.0, 220.0],
        racket_confidence=0.8,
        pose_valid=True,
        markers=("ball_racket_anchor",),
        candidate_id=candidate,
        frames_to_anchor=None if candidate is None else 0,
    )


def candidate(cid: str, start: int, end: int, anchor: int, level: int = 20) -> StoredCandidate:
    return StoredCandidate(
        candidate_id=cid,
        interval_start=start,
        interval_end=end,
        anchor_frame=anchor,
        basis="ball_racket",
        basis_error_frames=7,
        confidence=0.8,
        window_level=level,
    )


def prepare(store: EvidenceStore, *, frames: range = range(1, 101)) -> None:
    store.record_frames("task-a", 1, [frame(f) for f in frames], uploaded_at=UPLOADED)
    store.record_retention("task-a", 1, start_frame=1, end_frame=100, uploaded_at=UPLOADED)


# ----------------------------------------------------------------------
# writing and reading
# ----------------------------------------------------------------------


def test_frames_round_trip_without_adding_judgements(store: EvidenceStore) -> None:
    prepare(store)
    retrieved = store.retrieve("task-a", 1, frame_start=10, frame_end=12, now=UPLOADED)
    assert [f.frame for f in retrieved.frames] == [10, 11, 12]
    payload = retrieved.as_dict()
    assert set(payload["frames"][0]) == {
        "frame",
        "time_s",
        "ball_box",
        "ball_confidence",
        "ball_source",
        "racket_box",
        "racket_confidence",
        "pose_valid",
        "markers",
        "candidate_id",
        "frames_to_anchor",
    }
    assert "hit" not in str(payload).lower()


def test_recording_the_same_frame_twice_is_idempotent(store: EvidenceStore) -> None:
    prepare(store, frames=range(1, 11))
    prepare(store, frames=range(1, 11))
    retrieved = store.retrieve("task-a", 1, frame_start=1, frame_end=10, now=UPLOADED)
    assert len(retrieved.frames) == 10


# ----------------------------------------------------------------------
# retrieval modes
# ----------------------------------------------------------------------


def test_candidate_retrieval_returns_the_whole_interval(store: EvidenceStore) -> None:
    prepare(store)
    store.record_candidates("task-a", 1, [candidate("cand-1", 30, 70, 50)])
    retrieved = store.retrieve("task-a", 1, candidate_id="cand-1", now=UPLOADED)
    assert retrieved.reason is None
    assert retrieved.truncated is False
    assert retrieved.window_level == 20
    assert [f.frame for f in retrieved.frames] == list(range(30, 71))


def test_unknown_candidate_is_refused_not_invented(store: EvidenceStore) -> None:
    prepare(store)
    retrieved = store.retrieve("task-a", 1, candidate_id="nope", now=UPLOADED)
    assert retrieved.reason == "no_candidate"
    assert retrieved.frames == []
    assert retrieved.message == "该区间未找到击球候选"


def test_candidate_retrieval_truncates_around_the_anchor(store: EvidenceStore) -> None:
    prepare(store)
    store.record_candidates("task-a", 1, [candidate("cand-1", 10, 90, 50)])
    retrieved = store.retrieve("task-a", 1, candidate_id="cand-1", now=UPLOADED, frame_limit=21)
    assert retrieved.truncated is True
    assert len(retrieved.frames) == 21
    assert retrieved.frames[0].frame <= 50 <= retrieved.frames[-1].frame


def test_frame_range_retrieval_needs_no_candidate(store: EvidenceStore) -> None:
    prepare(store)
    retrieved = store.retrieve("task-a", 1, frame_start=5, frame_end=8, now=UPLOADED)
    assert retrieved.reason is None
    assert [f.frame for f in retrieved.frames] == [5, 6, 7, 8]


def test_range_outside_retention_is_refused(store: EvidenceStore) -> None:
    store.record_frames("task-a", 1, [frame(f) for f in range(1, 21)], uploaded_at=UPLOADED)
    store.record_retention("task-a", 1, start_frame=1, end_frame=20, uploaded_at=UPLOADED)
    retrieved = store.retrieve("task-a", 1, frame_start=10, frame_end=40, now=UPLOADED)
    assert retrieved.reason == "not_retained"
    assert retrieved.frames == []


def test_retrieval_requires_one_of_the_two_modes(store: EvidenceStore) -> None:
    with pytest.raises(DomainViolation):
        store.retrieve("task-a", 1, now=UPLOADED)
    with pytest.raises(DomainViolation):
        store.retrieve("task-a", 1, frame_start=5, frame_end=4, now=UPLOADED)


# ----------------------------------------------------------------------
# retention
# ----------------------------------------------------------------------


def test_reads_do_not_extend_retention(store: EvidenceStore) -> None:
    prepare(store)
    later = UPLOADED + RETENTION - timedelta(seconds=1)
    assert store.retrieve("task-a", 1, frame_start=1, frame_end=2, now=later).reason is None
    expired = UPLOADED + RETENTION
    retrieved = store.retrieve("task-a", 1, frame_start=1, frame_end=2, now=expired)
    assert retrieved.reason == "expired"
    assert retrieved.frames == []


def test_purge_removes_expired_evidence_only(store: EvidenceStore) -> None:
    prepare(store)
    store.record_candidates("task-a", 1, [candidate("cand-1", 30, 70, 50)])
    fresh = UPLOADED + RETENTION
    store.record_frames("task-b", 1, [frame(1)], uploaded_at=fresh)
    store.record_retention("task-b", 1, start_frame=1, end_frame=1, uploaded_at=fresh)
    deleted = store.purge_expired(now=fresh)
    assert deleted == 100
    assert store.retained_ranges("task-a", 1) == []
    assert store.candidates("task-a", 1) == ()
    assert store.retained_ranges("task-b", 1) == [(1, 1)]


def test_purge_can_target_one_task(store: EvidenceStore) -> None:
    prepare(store)
    store.record_frames("task-b", 1, [frame(1)], uploaded_at=UPLOADED)
    store.record_retention("task-b", 1, start_frame=1, end_frame=1, uploaded_at=UPLOADED)
    deleted = store.purge_expired(now=UPLOADED + RETENTION, task_id="task-a")
    assert deleted == 100
    assert store.retained_ranges("task-b", 1) == [(1, 1)]


# ----------------------------------------------------------------------
# storage estimate (asked before a run)
# ----------------------------------------------------------------------


def test_storage_estimate_counts_retained_frames(store: EvidenceStore) -> None:
    prepare(store)
    store.record_retention(
        "task-a", 1, start_frame=10, end_frame=49, uploaded_at=UPLOADED, label="b"
    )
    store.record_retention(
        "task-a", 1, start_frame=60, end_frame=79, uploaded_at=UPLOADED, label="c"
    )
    estimate = store.storage_estimate("task-a", 1)
    assert estimate["retained_frames"] == 100 + 40 + 20
    assert estimate["estimated_bytes"] == estimate["retained_frames"] * 29_000
    assert len(estimate["ranges"]) == 3


def test_storage_estimate_is_zero_without_retention(store: EvidenceStore) -> None:
    assert store.storage_estimate("task-z", 1) == {
        "retained_frames": 0,
        "estimated_bytes": 0,
        "ranges": [],
    }
