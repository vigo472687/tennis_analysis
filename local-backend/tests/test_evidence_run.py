"""The end-to-end evidence run, with fake detectors (no model inference).

These cases pin the order and the refusals, not detection quality: exactly one
detector runs, a full run without an approved estimate is refused, far view
produces ``ball_track`` candidates and near view does not, and the estimate
warns that it is an estimate.

The detector constructors and the video readers are patched so the suite stays
fast and offline; the real models are covered by the B0 experiment and the
optional real-video tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tennis_backend.application import evidence_run
from tennis_backend.domain.models import DomainViolation
from tennis_backend.media.ball_detect import BallRacketScan
from tennis_backend.media.wasb_track import BallTrack, TrackPoint
from tennis_backend.storage.evidence_store import EvidenceStore

NOW = datetime(2026, 11, 17, 12, 0, 0)
FPS = 30.0


def near_scan(count: int = 60, *, hit_at: int = 30) -> BallRacketScan:
    """Ball approaching the racket at ``hit_at`` (triangular distance profile)."""
    scan = BallRacketScan(fps=FPS, width=640, height=480)
    for frame in range(count):
        distance = abs(frame - hit_at) * 20.0
        ball = [300.0 - distance, 200.0, 300.0 - distance + 12.0, 212.0, 0.9]
        scan.frames[frame] = {
            "ball": [ball],
            "racket": [[280.0, 180.0, 340.0, 260.0, 0.9]],
        }
    return scan


def far_track(around: int = 40, span: int = 20) -> BallTrack:
    track = BallTrack(fps=FPS, width=886, height=498, frames=around + span + 10)
    points: dict[int, TrackPoint] = {}
    for offset in range(span, 0, -1):
        points[around - offset] = TrackPoint(
            x=300.0 - 5.0 * offset, y=200.0, source="detected", peak=0.8
        )
    points[around] = TrackPoint(x=300.0, y=200.0, source="detected", peak=0.8)
    for offset in range(1, span + 1):
        points[around + offset] = TrackPoint(
            x=300.0 - 5.0 * offset, y=200.0, source="detected", peak=0.8
        )
    track.points = points
    return track


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    with EvidenceStore(tmp_path / "evidence.sqlite") as opened:
        yield opened


def test_an_explicit_detector_is_not_marked_as_fallback() -> None:
    assert evidence_run.choose_detector(None, "coco") == ("coco", False)
    assert evidence_run.choose_detector(0.5, "wasb") == ("wasb", False)


def test_an_unknown_detector_is_refused() -> None:
    with pytest.raises(DomainViolation):
        evidence_run.choose_detector(None, "both")


def test_the_fallback_uses_the_measured_height_ratio() -> None:
    assert evidence_run.choose_detector(0.40, None) == ("coco", True)
    assert evidence_run.choose_detector(0.10, None) == ("wasb", True)
    assert evidence_run.choose_detector(None, None) == ("wasb", True)


def test_near_view_stores_candidates_and_writes_retained_images(
    store: EvidenceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence_run, "scan_near", lambda *_a, **_k: near_scan())
    written: list[list[int]] = []

    def fake_write(_video, _root, _rel, frames, **_kwargs):
        written.append([item.frame for item in frames])
        return ()

    monkeypatch.setattr(evidence_run, "write_frames", fake_write)
    outcome = evidence_run.run(
        tmp_path / "v.mp4",
        public_root=tmp_path,
        relative_evidence_dir="projects/t/v1/evidence",
        store=store,
        task_id="t",
        version=1,
        route="technical_diagnosis",
        retain_mode="route_trimmed",
        uploaded_at=NOW,
        detector="coco",
    )
    assert outcome.detector == "coco"
    assert outcome.fallback is False
    assert outcome.summary.candidates >= 1
    stored = store.candidates("t", 1)
    assert stored and stored[0].basis == "ball_racket"
    ranges = store.retained_ranges("t", 1)
    assert ranges
    # only retained frames reach the writer: the run keeps its storage promise
    assert written and all(any(lo <= frame <= hi for lo, hi in ranges) for frame in written[0])


def test_far_view_produces_ball_track_candidates(
    store: EvidenceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence_run, "track_far", lambda *_a, **_k: far_track())
    monkeypatch.setattr(evidence_run, "write_frames", lambda *_a, **_k: ())
    outcome = evidence_run.run(
        tmp_path / "v.mp4",
        public_root=tmp_path,
        relative_evidence_dir="projects/t/v1/evidence",
        store=store,
        task_id="t",
        version=1,
        route="technical_diagnosis",
        retain_mode="route_trimmed",
        uploaded_at=NOW,
        detector="wasb",
    )
    stored = store.candidates("t", 1)
    assert stored and all(candidate.basis == "ball_track" for candidate in stored)
    assert outcome.detector == "wasb"
    rows = store.between("t", 1, 0, 10**9)
    assert rows, "far view still stores per-frame metadata"
    assert all(row.ball is not None for row in rows)


def test_the_fallback_choice_is_reported_to_the_expert(
    store: EvidenceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence_run, "scan_near", lambda *_a, **_k: near_scan())
    monkeypatch.setattr(evidence_run, "write_frames", lambda *_a, **_k: ())
    outcome = evidence_run.run(
        tmp_path / "v.mp4",
        public_root=tmp_path,
        relative_evidence_dir="projects/t/v1/evidence",
        store=store,
        task_id="t",
        version=1,
        route="technical_diagnosis",
        retain_mode="route_trimmed",
        uploaded_at=NOW,
        subject_height_ratio=0.5,
    )
    assert outcome.fallback is True
    assert any("fallback" in note for note in outcome.notes)


def test_an_estimate_is_flagged_as_an_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        evidence_run,
        "detect_video",
        lambda *_a, **_k: near_scan(),
    )
    monkeypatch.setattr(evidence_run, "track_far", lambda *_a, **_k: far_track())
    estimate = evidence_run.estimate(
        tmp_path / "v.mp4",
        route="technical_diagnosis",
        retain_mode="route_trimmed",
        detector="coco",
    )
    assert estimate["estimate_only"] is True
    assert estimate["requires_confirmation"] is False
    assert estimate["estimated_bytes"] == estimate["retained_frames"] * 29_000


def test_a_full_run_requires_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evidence_run,
        "detect_video",
        lambda *_a, **_k: near_scan(),
    )
    monkeypatch.setattr(evidence_run, "track_far", lambda *_a, **_k: far_track())
    estimate = evidence_run.estimate(
        tmp_path / "v.mp4",
        route="technical_diagnosis",
        retain_mode="full",
        detector="coco",
    )
    assert estimate["requires_confirmation"] is True
    assert estimate["ranges"] == [{"start": 0, "end": 59}]


def test_purging_expires_rows_and_images_together(tmp_path: Path) -> None:
    root = tmp_path / "public"
    with EvidenceStore(tmp_path / "evidence.sqlite") as store:
        from tennis_backend.storage.evidence_store import EvidenceFrame

        store.record_frames(
            "t",
            1,
            [EvidenceFrame(frame=0, time_s=0.0)],
            uploaded_at=NOW - timedelta(days=40),
        )
        images = root / "projects/t/v1/evidence/frames"
        images.mkdir(parents=True)
        (images / "f000000.jpg").write_bytes(b"x")
        counts = evidence_run.purge_expired(
            store=store, metadata_path=tmp_path / "m.sqlite", public_root=root, now=NOW
        )
    assert counts["frames"] == 1
