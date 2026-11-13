"""Evidence catalog: back-linked evidence and relative metric publication (BE-019).

Turns adapter stage results (BE-015/016) plus a standardized media asset
(BE-014) into publishable evidence entries and relative metrics, enforcing
the publication gates the spec puts on them (AC-SPEC-012/017/018/046,
FR-022/032/033):

- **Every evidence maps back to the original video** (FR-022). Intervals are
  given in normalized time and converted to original time through the
  asset's *measured* time mapping. A task with ``time_mapping_valid=False``
  cannot publish any evidence (``EVIDENCE_TIME_UNMAPPED``, EX-043), and an
  interval reaching beyond the mapping's measured span is refused
  (``EVIDENCE_OUTSIDE_MAPPING``) — a partial mapping never silently covers
  the whole clip (BE-014 review follow-up).
- **Invisible objects produce no evidence** (``EVIDENCE_OBJECT_NOT_VISIBLE``
  as an item-level refusal): timing events the adapter reported as
  ``not_visible`` yield no entry, and claimed objects are checked against
  the frozen unavailable list.
- **Target pose is never evidence** (``EVIDENCE_TARGET_POSE``): presenting a
  target resource in the evidence channel is refused outright.
- **Relative metrics only, with their frozen definitions, references and
  limitations** (FR-033): a metric is published only when its reference
  timing event is observed, its required objects are all visible and — for
  footwork samples — the feet axis was actually observed; otherwise the
  metric is refused as an item with a stable reason and stays out of the
  published set (``EVIDENCE_METRIC_REFUSED``).
- **Evidence references must resolve** (``EVIDENCE_REFERENCE_UNKNOWN``): a
  metric citing evidence ids that the catalog does not hold is refused.

Hard refusals raise :class:`DomainViolation`; item-level gating is recorded
in ``refused`` and keeps the rest of the catalog publishable. All published
shapes satisfy the frozen ``analysis:evidence`` / ``analysis:metric``
definitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tennis_backend.domain.models import DomainViolation, MediaAsset

_TIME_UNMAPPED = "EVIDENCE_TIME_UNMAPPED"
_OUTSIDE_MAPPING = "EVIDENCE_OUTSIDE_MAPPING"
_TARGET_AS_EVIDENCE = "EVIDENCE_TARGET_POSE"
_METRIC_REFUSED = "EVIDENCE_METRIC_REFUSED"
_REFERENCE_UNKNOWN = "EVIDENCE_REFERENCE_UNKNOWN"


def original_time(media: MediaAsset, normalized_s: float) -> float:
    """Public wrapper: normalized time → original time (measured mapping)."""
    return _original_time(media, normalized_s)


def _original_time(media: MediaAsset, normalized_s: float) -> float:
    """Map normalized time to original time through the measured mapping."""
    points = media.time_mapping
    if normalized_s <= points[0].normalized_s:
        return points[0].original_s
    for previous, current in zip(points, points[1:], strict=False):
        if previous.normalized_s <= normalized_s <= current.normalized_s:
            span = current.normalized_s - previous.normalized_s
            if span <= 0:
                return current.original_s
            ratio = (normalized_s - previous.normalized_s) / span
            return previous.original_s + (current.original_s - previous.original_s) * ratio
    return points[-1].original_s


@dataclass(frozen=True)
class CatalogRefusal:
    """One item-level gate: the item stays unpublished, with a stable reason."""

    item: str
    reason: str
    detail: str


@dataclass(frozen=True)
class EvidenceCatalog:
    """Published evidence and metrics for one task version, plus refusals."""

    task_id: str
    version: int
    subject_id: str
    evidence: tuple[dict[str, Any], ...]
    metrics: tuple[dict[str, Any], ...]
    refused: tuple[CatalogRefusal, ...]
    mapping_span_s: float
    notes: tuple[str, ...] = field(default_factory=tuple)

    # ------------------------------------------------------------------
    # Builder
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        version: int,
        subject_id: str,
        media: MediaAsset,
        visible_objects: tuple[str, ...],
        unavailable_objects: tuple[str, ...],
        strokes: dict[str, Any],
        timing: dict[str, Any],
        metrics: dict[str, Any],
        metric_requires: dict[str, tuple[str, ...]] | None = None,
        footwork_axis_observed: bool | None = None,
        target_resources: tuple[str, ...] = (),
    ) -> EvidenceCatalog:
        """Assemble the catalog under the publication gates.

        ``metric_requires`` maps metric ids to the objects they need (from
        the frozen ``event_requires`` data). ``footwork_axis_observed`` is
        None/False when the feet axis was not observed, which refuses
        footwork support metrics instead of guessing them.
        """
        if not media.time_mapping_valid:
            raise DomainViolation(
                _TIME_UNMAPPED,
                "media has no valid time mapping; no evidence can be published (EX-043)",
            )
        if target_resources:
            raise DomainViolation(
                _TARGET_AS_EVIDENCE,
                f"target resources {list(target_resources)} cannot be published as evidence",
            )
        span = media.time_mapping[-1].normalized_s
        if span <= 0:
            raise DomainViolation(_TIME_UNMAPPED, "time mapping has a non-positive span")
        unavailable = set(unavailable_objects)
        visible = tuple(obj for obj in visible_objects if obj not in unavailable)
        refused: list[CatalogRefusal] = []
        entries: list[dict[str, Any]] = []

        # stroke window → one clip-shaped evidence entry
        stroke = strokes.get("events", [{}])[0] if strokes.get("events") else {}
        start_n = float(stroke.get("start_s", 0.0))
        end_n = float(stroke.get("end_s", 0.0))
        if end_n > span:
            raise DomainViolation(
                _OUTSIDE_MAPPING,
                f"stroke interval reaches {end_s_text(end_n)}s but the mapping covers only"
                f" {span:.3f}s of normalized time; publish nothing beyond it",
            )
        if end_n > start_n:
            claimed = tuple(obj for obj in visible if obj in ("body", "feet", "ball", "racket"))
            entries.append(
                _entry(
                    evidence_id=f"ev-{task_id}-v{version}-stroke",
                    kind="video_clip",
                    asset_id=media.media_id,
                    subject_id=subject_id,
                    original_start=_original_time(media, start_n),
                    original_end=_original_time(media, end_n),
                    visible_objects=list(claimed),
                )
            )
        else:
            refused.append(
                CatalogRefusal(
                    "evidence:stroke_window", "EVIDENCE_EMPTY_INTERVAL", "zero-length window"
                )
            )

        # timing: only observed events become evidence; not_visible yields nothing
        for event in timing.get("events", []):
            kind = str(event.get("kind"))
            if event.get("value_state") != "observed":
                refused.append(
                    CatalogRefusal(
                        f"evidence:timing:{kind}",
                        "EVIDENCE_OBJECT_NOT_VISIBLE",
                        "the event's required objects are not visible; no time is invented",
                    )
                )
                continue
            stamp_n = float(event.get("original_s") or 0.0)
            if stamp_n > span:
                raise DomainViolation(
                    _OUTSIDE_MAPPING,
                    f"timing event {kind} at {stamp_n}s exceeds the mapping span {span:.3f}s",
                )
            required = tuple(str(obj) for obj in event.get("requires", []))
            claimed = tuple(obj for obj in required if obj not in unavailable)
            original = _original_time(media, stamp_n)
            entries.append(
                _entry(
                    evidence_id=f"ev-{task_id}-v{version}-timing-{kind}",
                    kind="video_frame",
                    asset_id=media.media_id,
                    subject_id=subject_id,
                    original_start=original,
                    original_end=original,
                    visible_objects=list(claimed),
                )
            )

        evidence_ids = {entry["evidence_id"] for entry in entries}

        # metrics: relative only, gated by objects, references and axis
        published_metrics: list[dict[str, Any]] = []
        is_footwork = stroke.get("kind") == "footwork"
        for metric in metrics.get("metrics", []):
            metric_id = str(metric.get("metric_id"))
            requires = tuple((metric_requires or {}).get(metric_id, ()))
            blocked = [obj for obj in requires if obj in unavailable]
            if blocked:
                refused.append(
                    CatalogRefusal(
                        f"metric:{metric_id}",
                        _METRIC_REFUSED,
                        f"required objects {blocked} are not visible",
                    )
                )
                continue
            if is_footwork and metric_id in _FOOTWORK_AXIS_METRICS and not footwork_axis_observed:
                refused.append(
                    CatalogRefusal(
                        f"metric:{metric_id}",
                        _METRIC_REFUSED,
                        "footwork axis not observed; no same-axis support conclusion",
                    )
                )
                continue
            if metric.get("value_state") != "observed":
                refused.append(
                    CatalogRefusal(
                        f"metric:{metric_id}",
                        _METRIC_REFUSED,
                        f"reference value_state={metric.get('value_state')}; nothing to publish",
                    )
                )
                continue
            cited = [str(item) for item in metric.get("evidence_ids", [])]
            unknown = [item for item in cited if item not in evidence_ids]
            if unknown:
                raise DomainViolation(
                    _REFERENCE_UNKNOWN,
                    f"metric {metric_id} cites unknown evidence {unknown}",
                )
            published_metrics.append(
                {
                    "metric_id": metric_id,
                    "definition": str(metric.get("definition", "")),
                    "value": metric.get("value"),
                    "value_state": "observed",
                    "unit": str(metric.get("unit", "relative")),
                    "reference": str(metric.get("reference", "")),
                    "evidence_ids": cited,
                    "limitations": [str(item) for item in metric.get("limitations", [])],
                }
            )

        notes = (
            f"mapping_span_s={span:.3f}",
            "evidence intervals are mapped through the measured time mapping only",
        )
        return cls(
            task_id=task_id,
            version=version,
            subject_id=subject_id,
            evidence=tuple(entries),
            metrics=tuple(published_metrics),
            refused=tuple(refused),
            mapping_span_s=span,
            notes=notes,
        )


#: Footwork metrics that need an observed same-axis feet condition.
_FOOTWORK_AXIS_METRICS = frozenset({"support-continuity"})


def _entry(
    *,
    evidence_id: str,
    kind: str,
    asset_id: str,
    subject_id: str,
    original_start: float,
    original_end: float,
    visible_objects: list[str],
) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "asset_id": asset_id,
        "subject_id": subject_id,
        "original_start_s": round(max(original_start, 0.0), 6),
        "original_end_s": round(max(original_end, max(original_start, 0.0)), 6),
        "visible_objects": visible_objects,
        "resource": None,
        "simulation": True,
    }


def end_s_text(value: float) -> str:
    """Format helper for refusal messages (no path or user content)."""
    return f"{value:.3f}"
