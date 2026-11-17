"""Evidence builder tests: synthetic scan -> stored frames, candidates, retention."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tennis_backend.application.evidence_builder import (
    build_candidates,
    build_evidence,
    plan_retention,
)
from tennis_backend.media.ball_detect import BallRacketScan
from tennis_backend.storage.evidence_store import EvidenceStore

UPLOADED = datetime(2026, 11, 16, 9, 0, 0)
RACKET = [100.0, 100.0, 160.0, 220.0, 0.8]


def ball(x: float, y: float, conf: float = 0.8) -> list[float]:
    return [x - 5, y - 5, x + 5, y + 5, conf]


def make_scan(hit_frames: list[int], length: int = 600) -> BallRacketScan:
    """Ball far from the racket except right at each hit frame."""
    frames = {}
    for f in range(length):
        distance = min((abs(f - h) for h in hit_frames), default=999) * 4.0 + 5.0
        frames[f] = {"ball": [ball(130.0 - distance, 160.0)], "racket": [RACKET]}
    return BallRacketScan(fps=30.0, width=720, height=1280, frames=frames)


@pytest.fixture()
def store(tmp_path: Path) -> EvidenceStore:
    with EvidenceStore(tmp_path / "e.sqlite") as opened:
        yield opened


def test_one_candidate_per_anchor() -> None:
    candidates = build_candidates(make_scan([100, 300, 500]))
    assert [c.anchor_frame for c in candidates] == pytest.approx([100, 300, 500], abs=3)


def test_every_frame_gets_metadata_and_distance_to_anchor(store: EvidenceStore) -> None:
    scan = make_scan([300])
    summary = build_evidence(
        store,
        scan,
        task_id="t",
        version=1,
        route="tactics",
        retain_mode="full",
        uploaded_at=UPLOADED,
    )
    assert summary.frames == 600
    got = store.retrieve("t", 1, frame_start=290, frame_end=310, now=UPLOADED)
    assert [f.frame for f in got.frames] == list(range(290, 311))
    marked = [f for f in got.frames if "ball_racket_anchor" in f.markers]
    assert len(marked) == 1
    assert all(f.frames_to_anchor is not None for f in got.frames)
    assert min(f.frames_to_anchor for f in got.frames if f.frames_to_anchor is not None) == 0


def test_tactics_keeps_the_whole_span() -> None:
    scan = make_scan([100])
    assert plan_retention(scan, [], route="tactics", retain_mode="route_trimmed") == [(0, 599)]


def test_full_mode_keeps_the_whole_span_for_any_route() -> None:
    scan = make_scan([100])
    assert plan_retention(scan, [], route="technical_diagnosis", retain_mode="full") == [(0, 599)]


def test_diagnosis_keeps_at_most_four_actions_of_at_most_eighty_frames() -> None:
    scan = make_scan([50, 150, 250, 350, 450])
    candidates = build_candidates(scan)
    ranges = plan_retention(
        scan, candidates, route="technical_diagnosis", retain_mode="route_trimmed"
    )
    assert 1 <= len(ranges) <= 4
    assert all(hi - lo + 1 <= 81 for lo, hi in ranges)


def test_other_routes_keep_no_images_by_default() -> None:
    scan = make_scan([100])
    candidates = build_candidates(scan)
    assert (
        plan_retention(scan, candidates, route="question_answer", retain_mode="route_trimmed") == []
    )


def test_trimmed_diagnosis_refuses_ranges_it_did_not_keep(store: EvidenceStore) -> None:
    scan = make_scan([100, 300])
    build_evidence(
        store,
        scan,
        task_id="t",
        version=1,
        route="technical_diagnosis",
        retain_mode="route_trimmed",
        uploaded_at=UPLOADED,
    )
    kept = store.retained_ranges("t", 1)
    assert kept
    outside = store.retrieve("t", 1, frame_start=450, frame_end=470, now=UPLOADED)
    assert outside.reason == "not_retained"
    inside_lo, inside_hi = kept[0]
    inside = store.retrieve("t", 1, frame_start=inside_lo, frame_end=inside_lo + 5, now=UPLOADED)
    assert inside.reason is None and inside_hi >= inside_lo


def test_candidate_retrieval_end_to_end(store: EvidenceStore) -> None:
    scan = make_scan([300])
    build_evidence(
        store,
        scan,
        task_id="t",
        version=1,
        route="tactics",
        retain_mode="route_trimmed",
        uploaded_at=UPLOADED,
    )
    candidate_id = store.candidates("t", 1)[0].candidate_id
    got = store.retrieve("t", 1, candidate_id=candidate_id, now=UPLOADED)
    assert got.reason is None
    assert len(got.frames) >= 41


def test_pose_validity_is_recorded_per_frame(store: EvidenceStore) -> None:
    scan = make_scan([100], length=50)
    build_evidence(
        store,
        scan,
        task_id="t",
        version=1,
        route="tactics",
        retain_mode="full",
        uploaded_at=UPLOADED,
        pose_valid={f: f % 2 == 0 for f in range(50)},
    )
    got = store.retrieve("t", 1, frame_start=0, frame_end=3, now=UPLOADED)
    assert [f.pose_valid for f in got.frames] == [True, False, True, False]
