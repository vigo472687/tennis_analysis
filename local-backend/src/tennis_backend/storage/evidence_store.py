"""Per-frame evidence storage and interval retrieval (expert review support).

Every analysed task keeps, for each frame the pipeline looked at, *what was
detected* — ball box and confidence, racket box and confidence, whether the
pose was valid, which evidence sources fired, the candidate it belongs to and
its distance to the nearest anchor. It never stores "this is a hit": that
judgement belongs to the expert reading the frames (AC-POSE-004), and the B0
experiment showed anchors exist on walking/holding-ball frames too.

Retention (user decisions, 2026-11-16):

- frames and metadata stay **1 month from upload**, measured from the task's
  creation time; reading never extends it;
- what is kept depends on the task route: a technical-diagnosis run keeps
  only the 3-4 complete actions (a window per candidate), a tactics run keeps
  the whole analysed span;
- deletion is a backend job (:meth:`EvidenceStore.purge_expired`), not a
  manual step.

Retrieval returns whole intervals, never a single frame: judging whether the
contact was early or late needs the approach and the follow-through.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from tennis_backend.domain.models import DomainViolation

#: Evidence frames and metadata are kept this long after upload.
RETENTION = timedelta(days=30)

#: Frames a technical-diagnosis run keeps per retained action.
ANALYSIS_FRAMES_PER_ACTION = 80
#: Actions a technical-diagnosis run keeps.
ANALYSIS_MAX_ACTIONS = 4

#: Whole-span retention for a tactics review.
TACTICS_SPAN = "full"

RetentionMode = Literal["route_trimmed", "full"]
RetrievalFailure = Literal["not_retained", "expired", "no_candidate"]

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS evidence_frames (
        task_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        frame INTEGER NOT NULL,
        time_s REAL NOT NULL,
        ball_box TEXT,
        ball_confidence REAL,
        racket_box TEXT,
        racket_confidence REAL,
        pose_valid INTEGER NOT NULL,
        markers TEXT NOT NULL,
        candidate_id TEXT,
        frames_to_anchor INTEGER,
        uploaded_at TEXT NOT NULL,
        PRIMARY KEY (task_id, version, frame)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_candidates (
        task_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        candidate_id TEXT NOT NULL,
        interval_start INTEGER NOT NULL,
        interval_end INTEGER NOT NULL,
        anchor_frame INTEGER NOT NULL,
        basis TEXT NOT NULL,
        basis_error_frames INTEGER,
        confidence REAL NOT NULL,
        window_level INTEGER NOT NULL,
        PRIMARY KEY (task_id, version, candidate_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_intervals (
        task_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        label TEXT NOT NULL,
        start_frame INTEGER NOT NULL,
        end_frame INTEGER NOT NULL,
        uploaded_at TEXT NOT NULL,
        PRIMARY KEY (task_id, version, label)
    )
    """,
)


@dataclass(frozen=True)
class EvidenceFrame:
    """One frame's observations, exactly as detected."""

    frame: int
    time_s: float
    ball: list[float] | None = None
    ball_confidence: float | None = None
    racket: list[float] | None = None
    racket_confidence: float | None = None
    pose_valid: bool = False
    markers: tuple[str, ...] = ()
    candidate_id: str | None = None
    frames_to_anchor: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "frame": self.frame,
            "time_s": round(self.time_s, 3),
            "ball_box": None if self.ball is None else [round(v, 1) for v in self.ball],
            "ball_confidence": (
                None if self.ball_confidence is None else round(self.ball_confidence, 3)
            ),
            "racket_box": None if self.racket is None else [round(v, 1) for v in self.racket],
            "racket_confidence": (
                None if self.racket_confidence is None else round(self.racket_confidence, 3)
            ),
            "pose_valid": self.pose_valid,
            "markers": list(self.markers),
            "candidate_id": self.candidate_id,
            "frames_to_anchor": self.frames_to_anchor,
        }


@dataclass(frozen=True)
class StoredCandidate:
    """A stored hit candidate (not a hit: a candidate)."""

    candidate_id: str
    interval_start: int
    interval_end: int
    anchor_frame: int
    basis: str
    basis_error_frames: int | None
    confidence: float
    window_level: int


@dataclass(frozen=True)
class EvidenceInterval:
    """Some frames of one task version, as returned to the expert."""

    frames: list[EvidenceFrame] = field(default_factory=list)
    window_level: int | None = None
    truncated: bool = False
    reason: RetrievalFailure | None = None
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "frames": [frame.as_dict() for frame in self.frames],
            "window_level": self.window_level,
            "truncated": self.truncated,
            "reason": self.reason,
            "message": self.message,
        }


class EvidenceStore:
    """SQLite repository for per-frame evidence and its retention windows."""

    def __init__(self, path: str | Path) -> None:
        self._connection = sqlite3.connect(path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        try:
            with self._transaction():
                for statement in _SCHEMA:
                    self._connection.execute(statement)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> EvidenceStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------

    def record_frames(
        self,
        task_id: str,
        version: int,
        frames: Iterable[EvidenceFrame],
        *,
        uploaded_at: datetime,
    ) -> int:
        """Store frames (idempotent per frame number) and return how many landed."""
        stamp = uploaded_at.isoformat()
        rows = list(frames)
        with self._transaction():
            for item in rows:
                self._connection.execute(
                    "INSERT OR REPLACE INTO evidence_frames VALUES"
                    " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        version,
                        item.frame,
                        item.time_s,
                        None if item.ball is None else json.dumps(item.ball),
                        item.ball_confidence,
                        None if item.racket is None else json.dumps(item.racket),
                        item.racket_confidence,
                        int(item.pose_valid),
                        json.dumps(list(item.markers)),
                        item.candidate_id,
                        item.frames_to_anchor,
                        stamp,
                    ),
                )
        return len(rows)

    def record_candidates(
        self, task_id: str, version: int, candidates: Iterable[StoredCandidate]
    ) -> int:
        rows = list(candidates)
        with self._transaction():
            for item in rows:
                self._connection.execute(
                    "INSERT OR REPLACE INTO evidence_candidates VALUES"
                    " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        version,
                        item.candidate_id,
                        item.interval_start,
                        item.interval_end,
                        item.anchor_frame,
                        item.basis,
                        item.basis_error_frames,
                        item.confidence,
                        item.window_level,
                    ),
                )
        return len(rows)

    def record_retention(
        self,
        task_id: str,
        version: int,
        *,
        start_frame: int,
        end_frame: int,
        uploaded_at: datetime,
        label: str = "retained",
    ) -> None:
        """Declare a retained frame range; anything outside every range is dropped."""
        with self._transaction():
            self._connection.execute(
                "INSERT OR REPLACE INTO evidence_intervals VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, version, label, start_frame, end_frame, uploaded_at.isoformat()),
            )

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------

    def candidates(self, task_id: str, version: int) -> tuple[StoredCandidate, ...]:
        rows = self._connection.execute(
            "SELECT * FROM evidence_candidates WHERE task_id = ? AND version = ?"
            " ORDER BY interval_start",
            (task_id, version),
        ).fetchall()
        return tuple(
            StoredCandidate(
                candidate_id=row["candidate_id"],
                interval_start=row["interval_start"],
                interval_end=row["interval_end"],
                anchor_frame=row["anchor_frame"],
                basis=row["basis"],
                basis_error_frames=row["basis_error_frames"],
                confidence=row["confidence"],
                window_level=row["window_level"],
            )
            for row in rows
        )

    def retained_ranges(self, task_id: str, version: int) -> list[tuple[int, int]]:
        rows = self._connection.execute(
            "SELECT start_frame, end_frame FROM evidence_intervals"
            " WHERE task_id = ? AND version = ? ORDER BY start_frame",
            (task_id, version),
        ).fetchall()
        return [(row["start_frame"], row["end_frame"]) for row in rows]

    def _inside_retention(
        self, task_id: str, version: int, frame_start: int, frame_end: int
    ) -> bool:
        ranges = self.retained_ranges(task_id, version)
        if not ranges:
            return False
        return all(any(lo <= f <= hi for lo, hi in ranges) for f in (frame_start, frame_end))

    def _uploaded_at(self, task_id: str, version: int) -> datetime | None:
        row = self._connection.execute(
            "SELECT uploaded_at FROM evidence_frames WHERE task_id = ? AND version = ?"
            " ORDER BY uploaded_at LIMIT 1",
            (task_id, version),
        ).fetchone()
        if row is None:
            row = self._connection.execute(
                "SELECT uploaded_at FROM evidence_intervals WHERE task_id = ? AND version = ?"
                " LIMIT 1",
                (task_id, version),
            ).fetchone()
        return None if row is None else datetime.fromisoformat(row["uploaded_at"])

    def expired(self, task_id: str, version: int, *, now: datetime) -> bool:
        uploaded = self._uploaded_at(task_id, version)
        return uploaded is not None and now >= uploaded + RETENTION

    def between(self, task_id: str, version: int, start: int, end: int) -> list[EvidenceFrame]:
        """Stored frames in ``[start, end]``; empty when nothing was kept."""
        rows = self._connection.execute(
            "SELECT * FROM evidence_frames WHERE task_id = ? AND version = ?"
            " AND frame BETWEEN ? AND ? ORDER BY frame",
            (task_id, version, start, end),
        ).fetchall()
        return [_frame_of(row) for row in rows]

    # ------------------------------------------------------------------
    # expert retrieval
    # ------------------------------------------------------------------

    def retrieve(
        self,
        task_id: str,
        version: int,
        *,
        now: datetime,
        candidate_id: str | None = None,
        frame_start: int | None = None,
        frame_end: int | None = None,
        frame_limit: int = 200,
    ) -> EvidenceInterval:
        """Interval retrieval in either of the two supported modes.

        By candidate: the stored candidate interval, limited to ``frame_limit``
        frames counted from the anchor outward, and reported as truncated.
        By frame range: exactly what the expert asked for.
        Both refuse rather than fabricate: an expired version, a range outside
        what was retained, or an unknown candidate each return a reason.
        """
        if self.expired(task_id, version, now=now):
            return EvidenceInterval(reason="expired", message="该段证据已过保留期（1 个月）")
        if candidate_id is not None:
            stored = next(
                (c for c in self.candidates(task_id, version) if c.candidate_id == candidate_id),
                None,
            )
            if stored is None:
                return EvidenceInterval(reason="no_candidate", message="该区间未找到击球候选")
            start, end = _limit_around(
                stored.interval_start, stored.interval_end, stored.anchor_frame, frame_limit
            )
            frames = self.between(task_id, version, start, end)
            return EvidenceInterval(
                frames=frames,
                window_level=stored.window_level,
                truncated=(start, end) != (stored.interval_start, stored.interval_end),
                reason=None if frames else "not_retained",
                message="" if frames else "该区间未保存图像（按任务路由只保留了击球片段）",
            )
        if frame_start is None or frame_end is None:
            raise DomainViolation(
                "EVIDENCE_INPUT", "retrieval needs a candidate id or a frame range"
            )
        if frame_end < frame_start:
            raise DomainViolation("EVIDENCE_INPUT", "frame_end must not precede frame_start")
        start, end = _limit_around(frame_start, frame_end, frame_end, frame_limit)
        if not self._inside_retention(task_id, version, start, end):
            return EvidenceInterval(
                reason="not_retained", message="该区间未保存图像（按任务路由只保留了击球片段）"
            )
        frames = self.between(task_id, version, start, end)
        return EvidenceInterval(
            frames=frames,
            truncated=(start, end) != (frame_start, frame_end),
            reason=None if frames else "not_retained",
            message="" if frames else "该区间未保存图像",
        )

    # ------------------------------------------------------------------
    # retention
    # ------------------------------------------------------------------

    def purge_expired(self, *, now: datetime, task_id: str | None = None) -> int:
        """Delete every frame, candidate and interval past its retention.

        Returns the number of frames deleted. Called by the backend's periodic
        job; a frame's own ``uploaded_at`` decides, so no clock is extended by
        a read.
        """
        cutoff = (now - RETENTION).isoformat()
        clause = "uploaded_at <= ?"
        params: tuple[Any, ...] = (cutoff, task_id) if task_id else (cutoff,)
        if task_id:
            clause += " AND task_id = ?"
        with self._transaction():
            deleted = self._connection.execute(
                f"DELETE FROM evidence_frames WHERE {clause}", params
            ).rowcount
            self._connection.execute(
                "DELETE FROM evidence_intervals WHERE uploaded_at <= ?"
                + (" AND task_id = ?" if task_id else ""),
                params,
            )
            self._connection.execute(
                "DELETE FROM evidence_candidates WHERE task_id NOT IN"
                " (SELECT DISTINCT task_id FROM evidence_frames)"
            )
        return int(deleted)

    def storage_estimate(
        self, task_id: str, version: int, *, bytes_per_frame: int = 29_000
    ) -> dict[str, Any]:
        """Frame count and byte estimate for the retained ranges of one version."""
        ranges = self.retained_ranges(task_id, version)
        frames = sum(hi - lo + 1 for lo, hi in ranges)
        return {
            "retained_frames": frames,
            "estimated_bytes": frames * bytes_per_frame,
            "ranges": [{"start": lo, "end": hi} for lo, hi in ranges],
        }


def _limit_around(start: int, end: int, anchor: int, limit: int) -> tuple[int, int]:
    """Trim ``[start, end]`` to at most ``limit`` frames, nearest the anchor first."""
    span = end - start + 1
    if span <= limit or limit <= 0:
        return start, end
    lo = max(start, anchor - limit // 2)
    hi = min(end, lo + limit - 1)
    lo = max(start, hi - limit + 1)
    return lo, hi


def _frame_of(row: sqlite3.Row) -> EvidenceFrame:
    return EvidenceFrame(
        frame=row["frame"],
        time_s=row["time_s"],
        ball=None if row["ball_box"] is None else json.loads(row["ball_box"]),
        ball_confidence=row["ball_confidence"],
        racket=None if row["racket_box"] is None else json.loads(row["racket_box"]),
        racket_confidence=row["racket_confidence"],
        pose_valid=bool(row["pose_valid"]),
        markers=tuple(json.loads(row["markers"])),
        candidate_id=row["candidate_id"],
        frames_to_anchor=row["frames_to_anchor"],
    )


__all__ = [
    "ANALYSIS_FRAMES_PER_ACTION",
    "ANALYSIS_MAX_ACTIONS",
    "RETENTION",
    "TACTICS_SPAN",
    "EvidenceFrame",
    "EvidenceInterval",
    "EvidenceStore",
    "RetentionMode",
    "RetrievalFailure",
    "StoredCandidate",
]
