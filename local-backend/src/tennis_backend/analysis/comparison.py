"""Comparable-retest comparison with gated relative changes (BE-021).

Compares two task versions (a baseline and a retest session) under the
frozen ``analysis:compare_result`` contract (AC-SPEC-029, FR-052/057):

- **Comparable only when the retest protocol matches** (FR-052's reusable
  conditions): same device scenario (机位), same target stroke (动作), same
  subject (对象), same configuration and contract versions (配置), the same
  declared retest protocol items (复测协议), both sides mirror-confirmed and
  safety-clear. Any mismatch means ``comparable=false``.
- **Relative changes only when comparable**: each metric observed on both
  sides with a numeric value contributes one delta entry in the frozen
  ``analysis:metric`` shape — the difference against the same baseline
  reference, with the baseline limitations carried over. Unit-free
  conclusions, percentages of "improvement" or any permanent rating are
  never produced (不输出永久等级): the output vocabulary has no rating
  fields at all.
- **Not comparable → differences and reshoot guidance only** (不可比只列
  差异与补拍): every mismatch becomes one stable difference string plus the
  matching reshoot action, and ``changes`` stays empty (the frozen contract
  enforces this too).

The comparison reads only what the caller hands in — the published metric
sets come from the BE-019 catalog, so an unpublished or refused metric can
never leak into a comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tennis_backend.domain.models import DomainViolation

#: Stable reshoot actions per mismatched condition (FR-057 补拍办法).
_RESHOOT_BY_FIELD: dict[str, str] = {
    "device_scenario": "reshoot with the same camera position as the baseline",
    "stroke_kind": "reshoot performing the same stroke as the baseline",
    "subject_id": "analyse the same subject as the baseline",
    "config_version": "re-run under the baseline configuration version",
    "contract_version": "re-run under the baseline contract version",
    "mirror_confirmed": "confirm the hitting direction before comparing",
    "safety": "clear the safety check first; comparison is not a training action",
    "protocol": "repeat the baseline retest protocol items exactly",
}


@dataclass(frozen=True)
class SessionSnapshot:
    """One measured session: its conditions and its published metrics."""

    task_id: str
    version: int
    fixture_id: str
    device_scenario: str
    stroke_kind: str
    subject_id: str | None
    config_version: str
    contract_version: str
    mirror_confirmed: bool
    safety: str
    #: Sorted key/value pairs declaring the reusable retest protocol
    #: (camera position, ball task, observation items, success condition).
    protocol: tuple[tuple[str, str], ...]
    #: Published (observed) metrics, in the BE-019 catalog shape.
    metrics: tuple[dict[str, Any], ...]

    def __post_init__(self) -> None:
        if self.protocol != tuple(sorted(self.protocol)):
            raise DomainViolation(
                "PROTOCOL_UNORDERED", "protocol items must be sorted key/value pairs"
            )


#: Condition fields compared for equality, with their difference labels.
_CONDITIONS: tuple[tuple[str, str], ...] = (
    ("device_scenario", "camera position (device scenario)"),
    ("stroke_kind", "stroke kind"),
    ("subject_id", "subject"),
    ("config_version", "configuration version"),
    ("contract_version", "contract version"),
    ("mirror_confirmed", "mirror confirmation"),
    ("safety", "safety state"),
)


def _field(snapshot: SessionSnapshot, name: str) -> Any:
    return getattr(snapshot, name)


def compare_sessions(baseline: SessionSnapshot, retest: SessionSnapshot) -> dict[str, Any]:
    """Return the frozen ``analysis:compare_result`` for two sessions."""
    differences: list[str] = []

    for field_name, label in _CONDITIONS:
        base_value = _field(baseline, field_name)
        retest_value = _field(retest, field_name)
        if base_value != retest_value:
            differences.append(
                f"{label}: baseline={base_value!r} retest={retest_value!r}"
                f" — {_RESHOOT_BY_FIELD[field_name]}"
            )
    if baseline.protocol != retest.protocol:
        missing = sorted(set(baseline.protocol) - set(retest.protocol))
        extra = sorted(set(retest.protocol) - set(baseline.protocol))
        differences.append(
            f"retest protocol differs (baseline-only items: {missing};"
            f" retest-only items: {extra}) — {_RESHOOT_BY_FIELD['protocol']}"
        )
    if baseline.fixture_id != retest.fixture_id:
        # fixture identity follows from scenario+stroke+config in the frozen
        # library; a mismatch means one side was not re-shot as planned
        differences.append(
            f"fixture identity: baseline={baseline.fixture_id!r}"
            f" retest={retest.fixture_id!r} — reshoot with the baseline fixture setup"
        )

    # Comparable means the retest CONDITIONS matched. Metric coverage gaps,
    # non-numeric values or unit mismatches are listed as differences for
    # the affected metric while the remaining metrics still compare.
    comparable = not differences
    changes: list[dict[str, Any]] = []
    if comparable:
        baseline_metrics = {str(m["metric_id"]): m for m in baseline.metrics}
        retest_metrics = {str(m["metric_id"]): m for m in retest.metrics}
        for metric_id in sorted(set(baseline_metrics) & set(retest_metrics)):
            base_metric = baseline_metrics[metric_id]
            retest_metric = retest_metrics[metric_id]
            base_value = base_metric.get("value")
            retest_value = retest_metrics[metric_id].get("value")
            if not isinstance(base_value, (int, float)) or not isinstance(
                retest_value, (int, float)
            ):
                differences.append(
                    f"metric {metric_id}: non-numeric values cannot produce a relative change"
                )
                continue
            if base_metric.get("unit") != retest_metric.get("unit"):
                differences.append(
                    f"metric {metric_id}: units differ"
                    f" ({base_metric.get('unit')} vs {retest_metric.get('unit')})"
                )
                continue
            if str(base_metric.get("definition", "")) != str(retest_metric.get("definition", "")):
                # a shared metric_id does not guarantee a shared definition:
                # comparing different definitions would fabricate a delta
                differences.append(
                    f"metric {metric_id}: definitions differ between baseline and retest"
                    " — re-measure both sides with the same metric definition"
                )
                continue
            changes.append(
                {
                    "metric_id": metric_id,
                    "definition": str(retest_metric.get("definition", "")),
                    "value": round(float(retest_value) - float(base_value), 6),
                    "value_state": "observed",
                    "unit": retest_metric.get("unit"),
                    "reference": (f"delta against baseline {baseline.task_id} v{baseline.version}"),
                    "evidence_ids": [],
                    "limitations": [
                        "relative change only; not a rating, level or permanent score",
                        *[str(item) for item in retest_metric.get("limitations", [])],
                    ],
                }
            )
        for metric_id in sorted(set(baseline_metrics) ^ set(retest_metrics)):
            side = "baseline only" if metric_id in baseline_metrics else "retest only"
            differences.append(
                f"metric coverage: {metric_id} observed in {side} —"
                " reshoot the missing side with the same conditions to compare it"
            )

    return {
        "comparable": comparable,
        "differences": differences[:200],
        "changes": changes[:200] if comparable else [],
        "simulation": True,
    }
