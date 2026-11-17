"""Non-interactive command line entry for the tennis local backend.

``--stdio`` serves the frozen NDJSON protocol (BE-010 transport) with the
full BE-027 dispatch over the frozen method table:

- ``runtime.status`` — real read-only health checks (BE-009),
  ``simulation=false``; the caller must send ``simulation=false``.
- ``analysis.create`` — creates a fixture task in ``draft`` and publishes a
  ``task-config`` record (the declared fixture and input) into the task
  workspace; starting the server or creating a task never runs analysis.
- ``analysis.status`` — task lookup.
- ``analysis.cancel`` — cooperative cancellation with checkpoint retention
  (BE-022); answers with the task summary.
- ``analysis.delete`` — stop-then-delete with per-item results (BE-025).
- ``analysis.quality`` / ``analysis.strokes`` / ``analysis.timing`` /
  ``analysis.metrics`` — frozen fixture responses (BE-016), validated
  through the shared adapter interface (BE-015).
- ``evidence.render`` — real standardization of the task's input copy plus
  evidence rendering (BE-014/019/020).
- ``target.generate`` — the watermarked simulated target skeleton (BE-020),
  structurally separate from evidence.
- ``sessions.compare`` — comparable-retest comparison (BE-021).
- ``review.export`` — the redacted diagnostic package (BE-024); the wire
  consent must cover ``diagnostics`` for the exact task version (BE-011).
- ``analysis.reset_all_demo`` is reached through ``analysis.delete`` with
  ``scope=all_demo`` (BE-026).

The runtime prepare/start/repair/uninstall methods stay refused with
``RUNTIME_BRIDGE_OWNED``: installing and uninstalling the local capability
is the Node Bridge's job (BE-009); the backend only verifies. The 17 MCP
tools remain exposed by the Bridge — this process opens no network entry.

Controlled locating: the server refuses to start unless ``TENNIS_DATA_ROOT``
names the data root the user chose; all state lives inside
``<root>/internal`` and workspaces under ``<root>/public``, resolved
independently of the current working directory.

Thread note (AG-B02): handlers run on transport worker threads, so the
SQLite stores are opened per request on the calling thread.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tennis_backend import CONTRACT_VERSION, __version__
from tennis_backend.analysis.adapters import StageRequest, run_stage
from tennis_backend.analysis.comparison import SessionSnapshot, compare_sessions
from tennis_backend.analysis.fixture import FixtureAnalysisAdapter, FixtureLibrary
from tennis_backend.application.cancel import CancelService
from tennis_backend.application.delete import DeleteService
from tennis_backend.application.idempotency import OperationLedger
from tennis_backend.application.reset import ResetService
from tennis_backend.application.tasks import TaskService
from tennis_backend.application.worker import CancellationToken
from tennis_backend.contracts.validation import (
    CONTRACT_VERSION as _FROZEN_CONTRACT_VERSION,
)
from tennis_backend.contracts.validation import (
    ContractRegistry,
    ContractViolation,
    CreateInput,
    Lookup,
    ValidatedRequest,
)
from tennis_backend.domain.models import (
    ConsentSnapshot,
    DomainViolation,
    Grounded,
    MediaProbe,
    UserInput,
)
from tennis_backend.export.package import SelectedMaterial, build_diagnostic_package
from tennis_backend.runtime.health import HealthConfig, runtime_health
from tennis_backend.storage.evidence_store import EvidenceStore
from tennis_backend.storage.metadata import MetadataStore
from tennis_backend.storage.resources import ResourceStore
from tennis_backend.transport.server import serve_stdio

_STDIO_ROOT_REQUIRED = (
    "TENNIS_DATA_ROOT is not set; refusing to start stdio (set it to the data root the user chose)"
)

#: Frozen fixture library, located from this file so cwd never matters.
FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "fixtures"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser without executing anything."""
    parser = argparse.ArgumentParser(
        prog="tennis-backend",
        description=(
            "Local tennis prototype backend (fixed-fixture simulation, "
            "structured results only; no teaching prose, network or email)."
        ),
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print backend and contract version, then exit",
    )
    parser.add_argument(
        "--stdio",
        action="store_true",
        help=(
            "serve one JSON line per request/response on stdin/stdout; "
            "requires TENNIS_DATA_ROOT to name the authorized data root"
        ),
    )
    return parser


class StdioHandler:
    """Full frozen-method dispatch over the B1/B2/B3 application services."""

    def __init__(
        self,
        registry: ContractRegistry,
        data_root: Path,
        *,
        subject_detector: Any = None,
        pose_model: Any = None,
    ) -> None:
        self._registry = registry
        self._data_root = data_root.resolve()
        self._internal = data_root / "internal"
        self._public = data_root / "public"
        self._fixtures = FixtureLibrary.load(FIXTURE_ROOT)
        self._subject_detector = subject_detector
        self._pose_model = pose_model

    def __call__(self, request: ValidatedRequest) -> dict[str, Any]:
        try:
            return self._dispatch(request)
        except DomainViolation as violation:
            # keep the stable domain code instead of collapsing to a generic
            # handler failure: the transport maps this onto the error package
            raise ContractViolation(str(violation), code=violation.code) from violation

    def _dispatch(self, request: ValidatedRequest) -> dict[str, Any]:
        method = request.method
        if method == "runtime.status":
            return self._health(request)
        if method == "analysis.create":
            return self._create(request)
        if method == "analysis.status":
            return self._lookup(request)
        if method == "analysis.cancel":
            return self._cancel(request)
        if method == "analysis.delete":
            return self._delete(request)
        if method in (
            "analysis.quality",
            "analysis.strokes",
            "analysis.timing",
            "analysis.metrics",
        ):
            return self._fixture_stage(request)
        if method == "evidence.render":
            return self._render(request)
        if method == "evidence.get":
            return self._evidence_get(request)
        if method == "analysis.estimate_storage":
            return self._estimate_storage(request)
        if method == "target.generate":
            return self._target(request)
        if method == "sessions.compare":
            return self._compare(request)
        if method == "review.export":
            return self._export(request)
        if method.startswith("runtime."):
            raise ContractViolation(
                f"method {method} is owned by the Node Bridge (install/prepare/"
                "uninstall); the backend only verifies via runtime.status",
                code="RUNTIME_BRIDGE_OWNED",
            )
        raise ContractViolation(
            f"method {method} is not implemented by this prototype backend",
            code="METHOD_NOT_IMPLEMENTED",
        )

    # ------------------------------------------------------------------
    # Per-request service context (worker-thread affinity, AG-B02)
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _services(self) -> Iterator[dict[str, Any]]:
        metadata = MetadataStore(self._internal / "metadata.sqlite")
        ledger = OperationLedger(self._internal / "operations.sqlite")
        resources = ResourceStore(self._public, self._internal)
        try:
            service = TaskService(metadata, ledger)
            yield {
                "metadata": metadata,
                "ledger": ledger,
                "resources": resources,
                "service": service,
                "cancel": CancelService(metadata),
                "delete": DeleteService(
                    metadata=metadata,
                    ledger=ledger,
                    resources=resources,
                    cancel=CancelService(metadata),
                ),
                "reset": ResetService(
                    metadata=metadata,
                    ledger=ledger,
                    resources=resources,
                    delete=DeleteService(
                        metadata=metadata,
                        ledger=ledger,
                        resources=resources,
                        cancel=CancelService(metadata),
                    ),
                    cancel=CancelService(metadata),
                ),
                "adapter": FixtureAnalysisAdapter(self._fixtures),
            }
        finally:
            ledger.close()
            metadata.close()

    def _now(self) -> datetime:
        return datetime.now(UTC)

    # ------------------------------------------------------------------
    # Methods
    # ------------------------------------------------------------------

    def _health(self, request: ValidatedRequest) -> dict[str, Any]:
        # real process checks: genuinely measured, so the caller must send
        # the request with simulation=false; a mismatched envelope fails the
        # frozen simulation consistency check instead of being faked
        config = HealthConfig(
            backend_version=__version__,
            public_root=self._public,
            internal_root=self._internal,
        )
        return runtime_health(config).model_dump()

    def _create(self, request: ValidatedRequest) -> dict[str, Any]:
        params = request.typed_params(CreateInput)
        if params.analysis_mode == "real_video":
            return self._create_real(request, params)
        fixture_id = params.fixture_id
        assert fixture_id is not None  # guaranteed by CreateInput for fixture mode
        self._fixtures.sample(fixture_id)  # unknown fixture: refused
        with self._services() as ctx:
            service: TaskService = ctx["service"]
            resources: ResourceStore = ctx["resources"]
            outcome = service.create_task(
                operation_id=params.operation_id,
                fixture_id=params.fixture_id,
                user_input=self._user_input(params),
                primary_route=params.primary_route,
                auxiliary_routes=list(params.auxiliary_routes),
                media_id=None,  # media ingest/registration is a later batch
                consent_id=params.consent.consent_id,
                subject_id=params.subject_id,
                at=self._now(),
            )
            if outcome.replayed:
                # a replayed create must not re-publish its config record
                return service.task_summary(outcome.task_id, outcome.version)
            # publish the declared input as a durable task-config record so
            # the query methods can address this task's fixture later
            config = {
                "fixture_id": params.fixture_id,
                "media_ref": params.media_ref,
                "primary_route": params.primary_route,
                "auxiliary_routes": list(params.auxiliary_routes),
                "consent_id": params.consent.consent_id,
                "subject_id": params.subject_id,
            }
            resources.publish_bytes(
                outcome.task_id,
                outcome.version,
                f"projects/{outcome.task_id}/v{outcome.version}/records/task-config.json",
                json.dumps(config, ensure_ascii=False, indent=1).encode("utf-8"),
                kind="record",
                mime="application/json",
            )
            return service.task_summary(outcome.task_id, outcome.version)

    def _create_real(self, request: ValidatedRequest, params: CreateInput) -> dict[str, Any]:
        """Phase-2 real_video slice: real probe + subject extraction.

        The pose stages arrive in later phases; until then the task stays in
        ``analyzing`` with honest notes instead of pretending completeness.
        """
        import hashlib
        import tempfile

        from tennis_backend.media.probe import probe_video
        from tennis_backend.media.subjects import extract_subjects

        video = (self._data_root / params.media_ref).resolve()
        if not video.is_relative_to(self._data_root):
            raise ContractViolation(
                f"media_ref {params.media_ref} leaves the authorized data root",
                code="MEDIA_REF_OUTSIDE_ROOT",
            )
        if not video.is_file():
            raise ContractViolation(
                "ingest video not found at the media_ref location",
                code="INGEST_VIDEO_MISSING",
            )

        with self._services() as ctx:
            service: TaskService = ctx["service"]
            ledger: OperationLedger = ctx["ledger"]
            resources: ResourceStore = ctx["resources"]
            outcome = service.create_task(
                operation_id=params.operation_id,
                fixture_id=None,
                user_input=self._user_input(params),
                primary_route=params.primary_route,
                auxiliary_routes=list(params.auxiliary_routes),
                consent_id=params.consent.consent_id,
                subject_id=params.subject_id,
                at=self._now(),
            )
            task_id = outcome.task_id
            version = outcome.version
            if outcome.replayed:
                # replayed create: artifacts (if any) already exist from the
                # first execution. An unanswered detector prompt must still
                # advance: the replay carries the ask forward and counts it,
                # so the "three asks, then fallback" rule works even when the
                # idempotency layer replays identical input.
                from tennis_backend.analysis.detector_choice import MAX_PROMPTS

                summary = service.task_summary(task_id, version)
                has_ball = f"projects/{task_id}/v{version}/records/ball-evidence.json" in {
                    r.path for r in ctx["resources"].resources(task_id, version)
                }
                if params.detector is None and not has_ball:
                    prompts = self._detector_prompts(params.media_ref)
                    prompts = min(prompts + 1, MAX_PROMPTS)
                    self._record_detector_prompt(params.media_ref)
                    if prompts >= MAX_PROMPTS:
                        note = (
                            f"已询问 {MAX_PROMPTS} 次仍未指定 detector;"
                            "请在 analysis.create 请求中带上 detector（coco 或 wasb）后重新创建"
                        )
                    else:
                        note = (
                            f"需要指定检测器（重复请求，第 {prompts}/{MAX_PROMPTS} 次询问）："
                            "近景用 coco，远景用 wasb"
                        )
                    summary = {
                        **summary,
                        "retained": [],
                        "recovery_actions": ["ask"],
                        "notes": [note],
                    }
                return summary

            # durable task-config (analysis mode + media ref) for later phases
            config = {
                "analysis_mode": "real_video",
                "media_ref": params.media_ref,
                "primary_route": params.primary_route,
                "consent_id": params.consent.consent_id,
                "subject_id": params.subject_id,
                "retain_mode": params.retain_mode,
                "detector": params.detector,
            }
            resources.publish_bytes(
                task_id,
                version,
                f"projects/{task_id}/v{version}/records/task-config.json",
                json.dumps(config, ensure_ascii=False, indent=1).encode("utf-8"),
                kind="record",
                mime="application/json",
            )

            # Phase 5 cooperative cancellation: analysis.cancel runs on the
            # control lane and flips the task state in metadata; the long
            # pipeline polls that flag between stages and frame-by-frame, so
            # a cancel stops the burn within one frame instead of running
            # minutes of inference to completion
            metadata = service._metadata
            pose_notes: list[str] = []

            def task_cancelled() -> bool:
                return (metadata.task_state(task_id) or "") == "cancelled"

            def cancelled_summary(stage: str) -> dict[str, Any]:
                # durable cancellation marker: downstream must be able to
                # tell "records truncated by cancel" from "a naturally
                # shorter selection" (Phase 5 review D-3). Publishing to a
                # cancelled task's workspace is a deliberate exemption from
                # the derived-write ban (BE-022/BE-025): these records are
                # state-neutral observations that explain what DID run —
                # no new analysis is derived after the cancel.
                with contextlib.suppress(DomainViolation):
                    resources.publish_bytes(
                        task_id,
                        version,
                        f"projects/{task_id}/v{version}/records/cancellation.json",
                        json.dumps(
                            {
                                "cancelled": True,
                                "stage": stage,
                                "detail": pose_notes,
                                "published_records": sorted(
                                    resource.path
                                    for resource in resources.resources(task_id, version)
                                ),
                            },
                            ensure_ascii=False,
                            indent=1,
                        ).encode("utf-8"),
                        kind="record",
                        mime="application/json",
                    )
                summary = service.task_summary(task_id)
                return {
                    **summary,
                    "retained": [],
                    "recovery_actions": ["none"],
                    "notes": [
                        f"cancelled during {stage}; partial work stopped honestly",
                        *pose_notes,
                    ],
                }

            # compatibility gate with a REAL probe (BE-013)
            probe = probe_video(video)
            try:
                service._metadata.record_transition(
                    task_id,
                    current="draft",
                    target="checking_input",
                    reason="real_pipeline_start",
                    at=self._now(),
                )
            except ContractViolation:
                # a cancel landing in this window beats the pipeline (Phase 5
                # review D-2): the state machine blocked the transition —
                # return the honest cancellation instead of a generic error
                if task_cancelled():
                    return cancelled_summary("input checking")
                raise
            if probe.summary["support"] != "supported":
                service._metadata.record_transition(
                    task_id,
                    current="checking_input",
                    target="unsupported",
                    reason=f"real_probe_{probe.summary['support']}",
                    at=self._now(),
                )
                summary = service.task_summary(task_id)
                return {
                    **summary,
                    "retained": [],
                    "recovery_actions": ["reshoot"],
                    "notes": [f"real probe: {', '.join(probe.notes) or probe.summary['support']}"],
                }

            # publish the input copy; the user original is never touched
            resources.publish_input_copy(task_id, version, video, mime="video/mp4")

            # normalize FIRST (BE-014 is the single rotation authority): the
            # derivative is upright and re-probes clean, so detection/tracking/
            # annotation all share one upright coordinate system. Rotated MOVs
            # (Display Matrix) arrive sideways through OpenCV — feeding them
            # straight to the models was the phase2-rotation diagnosis defect.
            from tennis_backend.media.normalize import normalize_video

            workspace = resources.ensure_workspace(task_id, version)
            normalized = normalize_video(
                video,
                output_dir=workspace / "media",
                original_media_id=f"{task_id}-v{version}",
                original_ref=f"projects/{task_id}/v{version}/media/original.mp4",
            )
            if normalized.is_blocked or normalized.media is None:
                service._metadata.record_transition(
                    task_id,
                    current="checking_input",
                    target="failed",
                    reason="normalization_blocked",
                    at=self._now(),
                )
                summary = service.task_summary(task_id)
                return {
                    **summary,
                    "retained": [],
                    "recovery_actions": ["retry"],
                    "notes": [f"normalization blocked: {', '.join(normalized.notes)}"],
                }
            analysis_video = normalized.derivative_path
            assert analysis_video is not None  # normalized outcome guarantees it
            resources.publish_file(
                task_id,
                version,
                f"projects/{task_id}/v{version}/media/standardized.mp4",
                analysis_video,
                kind="record",
                mime="video/mp4",
            )
            # durable measured media asset (probe + BE-014 time mapping) so
            # the query/render methods map back to original time without
            # re-running normalization
            resources.publish_bytes(
                task_id,
                version,
                f"projects/{task_id}/v{version}/records/media-asset.json",
                normalized.media.model_dump_json().encode("utf-8"),
                kind="record",
                mime="application/json",
            )

            # subject extraction (Phase 2) + pose extraction (Phase 3) + motion
            # derivation (user request), artifacts are published through the
            # store from per-stage temp dirs. Retry note (Phase 5 review
            # D-4): the create ledger op settles up front, so a crashed
            # pipeline cannot resume via the same operation_id — recovery is
            # delete + re-create with a fresh operation id.
            pose_retained: list[str] = []
            digest = "no_pipeline"
            sub_op = f"{params.operation_id}.subjects"
            pose_op = f"{params.operation_id}.pose"

            if task_cancelled():
                return cancelled_summary("input checking")

            sub = ledger.begin(sub_op, "subjects.extract", {"task_id": task_id}, at=self._now())
            pose_sub: Any = None  # bound below when subjects run this attempt
            if sub.replayed:
                digest = "replayed"
                pose_notes = ["subjects extraction replayed from previous attempt"]
            else:
                from tennis_backend.analysis.motion import publish_motion
                from tennis_backend.media.subjects import extract_subjects

                with tempfile.TemporaryDirectory(dir=workspace) as tmp:
                    subjects_outcome = extract_subjects(
                        video=analysis_video,
                        out_dir=Path(tmp),
                        detector=self._subject_detector,
                        should_cancel=task_cancelled,
                    )
                    if subjects_outcome.notes and any(
                        "cancelled at frame" in note for note in subjects_outcome.notes
                    ):
                        ledger.fail(sub_op, "TASK_CANCELLED", at=self._now())
                        return cancelled_summary("subject extraction")
                    assert subjects_outcome.annotated_video is not None
                    assert subjects_outcome.per_frame_jsonl is not None
                    annotated = resources.publish_file(
                        task_id,
                        version,
                        f"projects/{task_id}/v{version}/subjects/subjects-annotated.mp4",
                        subjects_outcome.annotated_video,
                        kind="evidence_clip",
                        mime="video/mp4",
                    )
                    frames_record = resources.publish_file(
                        task_id,
                        version,
                        f"projects/{task_id}/v{version}/records/subjects-frames.jsonl",
                        subjects_outcome.per_frame_jsonl,
                        kind="record",
                        mime="application/json",
                    )
                    pose_retained = [annotated.resource_id, frames_record.resource_id]
                    pose_notes = [f"detector frames: {len(subjects_outcome.records)}"]

                    subjects_summary = resources.publish_bytes(
                        task_id,
                        version,
                        f"projects/{task_id}/v{version}/records/subjects-summary.json",
                        json.dumps(
                            {
                                "source": "backend_subject_extraction",
                                **subjects_outcome.to_payload(),
                            },
                            ensure_ascii=False,
                            indent=1,
                        ).encode("utf-8"),
                        kind="record",
                        mime="application/json",
                    )

                    # ---- per-subject pipeline (联调: confirm ONE subject or analyze ALL) ----
                    from tennis_backend.analysis.motion import derive_motion, publish_motion
                    from tennis_backend.analysis.pose import RealPoseAdapter
                    from tennis_backend.analysis.swings import derive_swings
                    from tennis_backend.media.subject_pipeline import (
                        estimate_fps,
                        resolve_subject_plan,
                        run_subject_pose,
                        subject_candidates,
                        subject_label,
                    )

                    candidates = subject_candidates(subjects_outcome.records)
                    plan = resolve_subject_plan(params.subject_id, candidates)
                    fps_est = estimate_fps(subjects_outcome.records)
                    records_base = f"projects/{task_id}/v{version}/records"

                    def publish_json(rel: str, payload: Any) -> Any:
                        return resources.publish_bytes(
                            task_id,
                            version,
                            f"{records_base}/{rel}",
                            json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"),
                            kind="record",
                            mime="application/json",
                        )

                    candidates_res = publish_json(
                        "subject-candidates.json",
                        {
                            "requested": params.subject_id,
                            "mode": plan.mode,
                            "analyzed": [
                                subject_label(t) if t is not None else None for t in plan.track_ids
                            ],
                            "subject_stability": subjects_outcome.subject_stability,
                            "candidates": candidates,
                            "note": (
                                "candidates are every tracked person (not size-filtered): choose a "
                                "subject_id from this list, or 'all', in a new create call. "
                                "subject_id=null analyzes the dominant subject per window "
                                "and stays "
                                "unconfirmed."
                            ),
                        },
                    )
                    pose_retained = [
                        annotated.resource_id,
                        frames_record.resource_id,
                        subjects_summary.resource_id,
                        candidates_res.resource_id,
                    ]
                    pose_notes = [
                        f"detector frames: {len(subjects_outcome.records)}",
                        f"subject candidates: {len(candidates)} listed (mode={plan.mode},"
                        f" requested={params.subject_id})",
                    ]

                    # ---- ball detector resolution (contract 2.0.0 detector slot) ----
                    # The expert names coco/wasb; when it does not, the backend
                    # prompts (ReAct) up to MAX_PROMPTS per media_ref, then
                    # applies the measured height-ratio fallback. The ratio is
                    # computable here because subjects have just been extracted.
                    from tennis_backend.analysis.detector_choice import (
                        DetectorChoice,
                        NeedDetector,
                        resolve_detector,
                        subject_height_ratio,
                    )

                    ratio = subject_height_ratio(
                        [
                            max((b["box"][3] - b["box"][1]) for b in record.boxes)
                            for record in subjects_outcome.records
                        ],
                        normalized.media.probe.height,
                    )
                    choice = resolve_detector(
                        params.detector,
                        prompts_used=self._detector_prompts(params.media_ref),
                        height_ratio=ratio,
                    )
                    if isinstance(choice, NeedDetector):
                        self._record_detector_prompt(params.media_ref)
                        prompt = resources.publish_bytes(
                            task_id,
                            version,
                            f"projects/{task_id}/v{version}/records/detector-prompt.json",
                            json.dumps(
                                {
                                    "prompts_used": choice.prompts_used,
                                    "prompts_left": choice.prompts_left,
                                    "options": list(choice.options),
                                    "guidance": choice.guidance,
                                    "subject_height_ratio": ratio,
                                },
                                ensure_ascii=False,
                                indent=1,
                            ).encode("utf-8"),
                            kind="record",
                            mime="application/json",
                        )
                        summary = service.task_summary(task_id)
                        return {
                            **summary,
                            "retained": [prompt.resource_id],
                            "recovery_actions": ["ask"],
                            "notes": [
                                *subjects_outcome.notes,
                                f"需要指定检测器（第 {choice.prompts_used}/3 次询问）",
                                choice.guidance,
                                "pose 分析已暂停，指定 detector 后重新 create",
                            ],
                        }
                    self._clear_detector_prompt(params.media_ref)
                    detector_name = choice.detector
                    if isinstance(choice, DetectorChoice) and choice.fallback:
                        pose_notes.append(choice.note)

                    if plan.mode == "invalid":
                        # an unknown subject_id is an honest failure with the candidate list
                        # published — never a silent fallback to some other person
                        pose_notes.append(plan.error or "invalid subject_id")
                        ledger.fail(sub_op, "SUBJECT_ID_INVALID", at=self._now())
                        service._metadata.record_transition(
                            task_id,
                            current="checking_input",
                            target="failed",
                            reason="subject_id_not_candidate",
                            at=self._now(),
                        )
                        summary = service.task_summary(task_id)
                        return {
                            **summary,
                            "retained": pose_retained,
                            "recovery_actions": ["ask"],
                            "notes": [*subjects_outcome.notes, *pose_notes],
                        }

                    # Phase 3: pose extraction per subject, each judged PER WINDOW; V2 对打/比赛
                    # builds on exactly this loop.
                    pose_sub = ledger.begin(
                        pose_op, "pose.extract", {"task_id": task_id}, at=self._now()
                    )
                    if pose_sub.replayed:
                        pose_notes = ["pose extraction replayed from previous attempt"]
                        pose_retained = [
                            r.resource_id
                            for r in resources.resources(task_id, version)
                            if Path(r.path).name
                            in (
                                "pose-frames.jsonl",
                                "pose-frames-context.jsonl",
                                "skeleton-spec.json",
                                "motion-trajectories.json",
                                "motion-angles.jsonl",
                            )
                        ]
                    else:
                        results: list[Any] = []
                        for track in plan.track_ids:
                            subject_result = run_subject_pose(
                                records=subjects_outcome.records,
                                video=analysis_video,
                                out_dir=Path(tmp)
                                / f"pose-{track if track is not None else 'default'}",
                                pose_model=self._pose_model,
                                track_id=track,
                                fps=fps_est,
                                should_cancel=task_cancelled,
                            )
                            results.append(subject_result)
                            pose_notes += subject_result.notes
                            if subject_result.cancelled:
                                break

                        def publish_set(subject_result: Any, sub: str | None) -> list[str]:
                            """Publish one subject's records; ``sub=None`` is the flat primary set
                            (the layout every existing consumer reads)."""
                            prefix = "" if sub is None else f"subjects/{sub}/"
                            work = Path(tmp) / f"publish-{sub or 'flat'}"
                            work.mkdir(parents=True, exist_ok=True)
                            ids: list[str] = [
                                publish_json(
                                    f"{prefix}action-selection.json",
                                    subject_result.selection_payload,
                                ).resource_id,
                                publish_json(
                                    f"{prefix}segment-states.json", subject_result.segment_states
                                ).resource_id,
                            ]
                            # pose-frames.jsonl is ALWAYS published (empty when no segments): an
                            # explicit empty file signals "pose ran" — absence is ambiguous
                            for name, recs in (
                                ("pose-frames.jsonl", subject_result.pose_records),
                                ("pose-frames-context.jsonl", subject_result.context_records),
                            ):
                                ids.append(
                                    resources.publish_bytes(
                                        task_id,
                                        version,
                                        f"{records_base}/{prefix}{name}",
                                        "\n".join(
                                            json.dumps(r, ensure_ascii=False) for r in recs
                                        ).encode("utf-8"),
                                        kind="record",
                                        mime="application/json",
                                    ).resource_id
                                )
                            derived: dict[str, Any] | None = None
                            if subject_result.pose_records:
                                derived = derive_motion(subject_result.pose_records)
                                traj_path, angles_path = publish_motion(derived, work)
                                ids.append(
                                    resources.publish_file(
                                        task_id,
                                        version,
                                        f"{records_base}/{prefix}motion-trajectories.json",
                                        traj_path,
                                        kind="record",
                                        mime="application/json",
                                    ).resource_id
                                )
                                ids.append(
                                    resources.publish_file(
                                        task_id,
                                        version,
                                        f"{records_base}/{prefix}motion-angles.jsonl",
                                        angles_path,
                                        kind="record",
                                        mime="application/json",
                                    ).resource_id
                                )
                            # temporal summary per swing: pure derivation from the pose
                            # records just published (no inference). Always written — an
                            # empty swing list is the honest answer for a still segment
                            ids.append(
                                publish_json(
                                    f"{prefix}swings.json",
                                    derive_swings(
                                        subject_result.pose_records,
                                        (derived or {}).get("angles_per_frame", []),
                                        subject_result.selection_payload["selected_segments"],
                                        fps=fps_est,
                                    ),
                                ).resource_id
                            )
                            if sub is not None:
                                # this subject's own metrics, computed through the same honest
                                # adapter and validated against the frozen contract
                                adapter = RealPoseAdapter(
                                    pose_records=subject_result.pose_records,
                                    motion=derived
                                    or {"trajectories": {}, "angles_per_frame": [], "summary": {}},
                                    selection=subject_result.selection_payload,
                                    segment_states=subject_result.segment_states,
                                )
                                metrics = run_stage(
                                    adapter,
                                    "metrics",
                                    StageRequest(
                                        task_id=task_id,
                                        version=version,
                                        fixture_id=None,
                                        media=self._neutral_probe(),
                                    ),
                                    cancel=CancellationToken(),
                                    registry=self._registry,
                                )
                                ids.append(
                                    publish_json(
                                        f"{prefix}metrics.json", {"subject_id": sub, **metrics}
                                    ).resource_id
                                )
                            return ids

                        primary = results[0]
                        spec_path = next(
                            (r.skeleton_spec_path for r in results if r.skeleton_spec_path), None
                        )
                        pose_retained += publish_set(primary, None)
                        if spec_path is not None:
                            pose_retained.append(
                                resources.publish_file(
                                    task_id,
                                    version,
                                    f"{records_base}/skeleton-spec.json",
                                    spec_path,
                                    kind="record",
                                    mime="application/json",
                                ).resource_id
                            )
                        if plan.mode in ("single", "all"):
                            for subject_result in results:
                                assert subject_result.label is not None
                                pose_retained += publish_set(subject_result, subject_result.label)
                        posed_total = sum(
                            s.get("pose_frames", 0)
                            for s in primary.segment_states
                            if s.get("status") == "posed"
                        )
                        if primary.pose_records:
                            pose_notes.append(
                                "motion trajectories + 2D proxy angles derived"
                                " (wrist path labeled as wrist; not racket path)"
                            )
                            pose_notes.append(
                                f"pose: {posed_total} frames with pose across segments"
                            )
                        if primary.context_records:
                            pose_notes.append(
                                f"context: {len(primary.context_records)} frames posed outside the "
                                "action"
                                " windows (records/pose-frames-context.jsonl)"
                            )
                        if not primary.segment_states:
                            pose_notes.append("no selected segments produced records")
                digest = hashlib.sha256(
                    json.dumps(
                        {
                            "stability": subjects_outcome.subject_stability,
                            "retention": subjects_outcome.retention,
                        },
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()

            # Phase 5: a cancel that landed mid-pipeline must not be papered
            # over — the task stays cancelled, the ledger records the
            # interruption, and the caller gets an honest summary
            if task_cancelled():
                if not sub.replayed:
                    ledger.succeed(
                        sub_op, json.dumps({"task_id": task_id, "done": True}), at=self._now()
                    )
                # pose_sub is only bound when subjects extraction ran this time
                if pose_sub is not None and not pose_sub.replayed:
                    ledger.fail(pose_op, "TASK_CANCELLED", at=self._now())
                return cancelled_summary("the real pipeline")

            ledger.succeed(sub_op, json.dumps({"task_id": task_id, "done": True}), at=self._now())

            try:
                service._metadata.record_transition(
                    task_id,
                    current="checking_input",
                    target="analyzing",
                    reason="subjects_extracted",
                    checkpoint="checking_input",
                    checkpoint_digest=digest,
                    at=self._now(),
                )
            except ContractViolation:
                # cancel landed between the last check and this transition
                # (Phase 5 review D-2): the state machine blocked it — the
                # honest answer is the cancellation, not STATE_MISMATCH
                if task_cancelled():
                    return cancelled_summary("the real pipeline (cancel at finalize)")
                raise
            # durable stage-limitation notes for the workbench (the frozen
            # strokes_result shape has no limitations field, so the strokes
            # explanation lives here)
            notes_record = resources.publish_bytes(
                task_id,
                version,
                f"projects/{task_id}/v{version}/records/analysis-notes.json",
                json.dumps(
                    {
                        "strokes": [
                            "V1 is pose-only: without a detected ball or racket no "
                            "stroke kind is classified; analysis.strokes returns no events"
                        ],
                        "timing": [
                            "V1 reports no phase times: ball/racket events are "
                            "not_visible, body phases are insufficient_evidence"
                        ],
                        "metrics": [
                            "all metrics are 2D image-plane proxies; camera-position "
                            "dependent, relative to this clip only"
                        ],
                    },
                    ensure_ascii=False,
                    indent=1,
                ).encode("utf-8"),
                kind="record",
                mime="application/json",
            )
            # ---- ball evidence (contract 2.0.0): detect, store, retain, write images ----
            # One detector only, chosen above (expert or measured fallback).
            ball_notes: list[str] = []
            ball_record: Any = None
            if "detector_name" in locals() and detector_name is not None:
                from tennis_backend.application.evidence_run import run as _evidence_run

                class _TaskCancel(CancellationToken):
                    """Bridge the metadata cancel flag into the evidence run."""

                    def __init__(self) -> None:
                        super().__init__()
                        self._check = task_cancelled

                    def check_point(self) -> None:
                        if self._check():
                            raise ContractViolation(
                                "cancelled during ball evidence", code="TASK_CANCELLED"
                            )

                with EvidenceStore(self._internal / "evidence.sqlite") as evidence_store:
                    ball_outcome = _evidence_run(
                        analysis_video,
                        public_root=self._public,
                        relative_evidence_dir=f"projects/{task_id}/v{version}/evidence",
                        store=evidence_store,
                        task_id=task_id,
                        version=version,
                        route=params.primary_route,
                        retain_mode=params.retain_mode,
                        uploaded_at=self._now(),
                        detector=detector_name,
                        subject_height_ratio=ratio,
                        cancel=_TaskCancel(),
                    )
                ball_notes = list(ball_outcome.notes)
                ball_record = resources.publish_bytes(
                    task_id,
                    version,
                    f"projects/{task_id}/v{version}/records/ball-evidence.json",
                    json.dumps(
                        {
                            "detector": ball_outcome.detector,
                            "detector_fallback": ball_outcome.fallback,
                            "fps": normalized.media.probe.fps,
                            "width": normalized.media.probe.width,
                            "height": normalized.media.probe.height,
                            "retain_mode": params.retain_mode,
                            "subject_height_ratio": ratio,
                            "frames": ball_outcome.summary.frames,
                            "candidates": ball_outcome.summary.candidates,
                            "retained_ranges": [
                                {"start": lo, "end": hi}
                                for lo, hi in ball_outcome.summary.retained_ranges
                            ],
                            "images_written": len(ball_outcome.images),
                        },
                        ensure_ascii=False,
                        indent=1,
                    ).encode("utf-8"),
                    kind="record",
                    mime="application/json",
                )
                ball_notes.append(
                    f"ball evidence: {ball_outcome.summary.candidates} candidates,"
                    f" {len(ball_outcome.images)} frame images retained"
                    f" (retain_mode={params.retain_mode}); evidence.get retrieves them"
                )

            summary = service.task_summary(task_id)
            recovery = ["ask"] if subjects_outcome.requires_selection else ["none"]
            return {
                **summary,
                "retained": [
                    annotated.resource_id,
                    frames_record.resource_id,
                    notes_record.resource_id,
                    *pose_retained,
                ],
                "recovery_actions": recovery,
                "notes": [
                    *subjects_outcome.notes,
                    *pose_notes,
                    *ball_notes,
                    "queries available: strokes (no events), timing (honest "
                    "states), metrics (2D proxies); evidence.render renders "
                    "frames/pose overlay/trajectory",
                ],
            }

    def _lookup(self, request: ValidatedRequest) -> dict[str, Any]:
        params = request.typed_params(Lookup)
        with self._services() as ctx:
            service: TaskService = ctx["service"]
            return service.task_summary(params.task_id, params.version)

    def _cancel(self, request: ValidatedRequest) -> dict[str, Any]:
        from tennis_backend.contracts.validation import CancelInput

        params = request.typed_params(CancelInput)
        with self._services() as ctx:
            cancel: CancelService = ctx["cancel"]
            service: TaskService = ctx["service"]
            cancel.cancel(params.task_id, reason="user_cancel")
            return service.task_summary(params.task_id, params.version)

    def _delete(self, request: ValidatedRequest) -> dict[str, Any]:
        from tennis_backend.contracts.validation import DeleteInput

        params = request.typed_params(DeleteInput)
        if params.scope == "all_demo":
            return self._reset(request)
        if params.task_id is None:
            raise ContractViolation(
                "task scope deletion needs a task_id", code="DELETE_TASK_ID_REQUIRED"
            )
        with self._services() as ctx:
            delete: DeleteService = ctx["delete"]
            return delete.delete_task(
                operation_id=params.operation_id,
                task_id=params.task_id,
                version=params.version,
                confirmed=params.confirmed,
                scope=params.scope,
                at=self._now(),
            )

    def _reset(self, request: ValidatedRequest) -> dict[str, Any]:
        from tennis_backend.contracts.validation import DeleteInput

        params = request.typed_params(DeleteInput)
        with self._services() as ctx:
            reset: ResetService = ctx["reset"]
            raw = reset.reset_all_demo(
                operation_id=params.operation_id,
                confirmed=params.confirmed,
                at=self._now(),
            )
        # map the rich reset payload onto the frozen delete_result shape:
        # one flattened item per deleted resource, per-task failures kept
        items: list[dict[str, Any]] = []
        for task in raw["tasks"]:
            for item in task["items"]:
                items.append(
                    {
                        "resource_id": f"{task['task_id']}.{item.get('resource_id', 'task')}",
                        "status": item.get("status", "failed"),
                        "code": item.get("code"),
                    }
                )
            if task.get("code"):
                items.append(
                    {"resource_id": task["task_id"], "status": "failed", "code": task["code"]}
                )
        return {
            "operation_id": raw["operation_id"],
            "items": items,
            "complete": raw["complete"],
            "external_copies_uncontrolled": True,
            "runtime_uninstalled": False,
            "simulation": True,
        }

    def _fixture_stage(self, request: ValidatedRequest) -> dict[str, Any]:
        params = request.typed_params(Lookup)
        stage = {
            "analysis.quality": "quality",
            "analysis.strokes": "strokes",
            "analysis.timing": "timing",
            "analysis.metrics": "metrics",
        }[request.method]
        with self._services() as ctx:
            if self._task_analysis_mode(ctx, params.task_id, params.version) == "real_video":
                # Phase 4: real tasks answer through the RealPoseAdapter over
                # their published records — never through the fixture library
                records = self._real_records(ctx, params.task_id, params.version)
                if stage == "quality":
                    return self._real_quality(ctx, records)
                real_adapter = self._real_adapter(records)
                stage_request = StageRequest(
                    task_id=params.task_id,
                    version=params.version,
                    fixture_id=None,
                    media=self._neutral_probe(),
                )
                token = CancellationToken()
                return run_stage(
                    real_adapter, stage, stage_request, cancel=token, registry=self._registry
                )
            adapter: FixtureAnalysisAdapter = ctx["adapter"]
            fixture_id = self._task_fixture(ctx, params.task_id, params.version)
            stage_request = StageRequest(
                task_id=params.task_id,
                version=params.version,
                fixture_id=fixture_id,
                media=self._neutral_probe(),
            )
            token = CancellationToken()
            if stage == "quality":
                return adapter.quality_report(stage_request)
            return run_stage(adapter, stage, stage_request, cancel=token, registry=self._registry)

    def _render(self, request: ValidatedRequest) -> dict[str, Any]:
        from tennis_backend.contracts.validation import RenderInput
        from tennis_backend.evidence.catalog import EvidenceCatalog
        from tennis_backend.evidence.render import render_evidence
        from tennis_backend.media.normalize import normalize_video
        from tennis_backend.media.probe import probe_video

        params = request.typed_params(RenderInput)
        with self._services() as ctx:
            resources: ResourceStore = ctx["resources"]
            if self._task_analysis_mode(ctx, params.task_id, params.version) == "real_video":
                return self._render_real(request, params, ctx)
            adapter: FixtureAnalysisAdapter = ctx["adapter"]
            fixture_id = self._task_fixture(ctx, params.task_id, params.version)
            source = self._input_copy_path(resources, params.task_id, params.version)
            source_probe = probe_video(source)
            outcome = normalize_video(
                source,
                output_dir=self._public / "normalize",
                original_media_id=f"{params.task_id}-v{params.version}",
                original_ref=f"projects/{params.task_id}/v{params.version}/media/source.mp4",
            )
            if outcome.media is None or not outcome.media.time_mapping_valid:
                raise ContractViolation(
                    "standardization failed; no evidence can be rendered (EX-043)",
                    code="RENDER_STANDARDIZATION_FAILED",
                )
            token = CancellationToken()
            stage_request = StageRequest(
                task_id=params.task_id,
                version=params.version,
                fixture_id=fixture_id,
                media=source_probe.media,
            )
            stages = {
                stage: run_stage(
                    adapter, stage, stage_request, cancel=token, registry=self._registry
                )
                for stage in ("strokes", "timing", "metrics")
            }
            quality = adapter.quality_report(stage_request)
            catalog = EvidenceCatalog.build(
                task_id=params.task_id,
                version=params.version,
                subject_id="unspecified",
                media=outcome.media,
                visible_objects=(),
                unavailable_objects=tuple(quality["unavailable_objects"]),
                strokes=stages["strokes"],
                timing=stages["timing"],
                metrics=stages["metrics"],
            )
            wanted = [e for e in catalog.evidence if e["evidence_id"] in set(params.evidence_ids)]
            if not wanted:
                raise ContractViolation(
                    "none of the requested evidence ids is published for this task",
                    code="RENDER_EVIDENCE_UNKNOWN",
                )
            rendered = render_evidence(
                source_video=source,
                root=self._public,
                relative_dir=f"projects/{params.task_id}/v{params.version}/evidence",
                evidence=tuple(wanted),
                mode=params.mode,
            )
            return rendered.result

    def _render_real(
        self, request: ValidatedRequest, params: Any, ctx: dict[str, Any]
    ) -> dict[str, Any]:
        """Render real-task evidence: frames, pose overlay, slow motion.

        Modes: ``frame`` extracts the evidence frames from the user's input
        copy at ORIGINAL time (ffmpeg autorotate applies); ``trajectory``
        renders the measured skeleton overlay on the standardized
        derivative (FR-POSE-001/002); ``slow_motion`` renders the overlay
        stretched (frame duplication, no fabricated interpolation).
        """
        from tennis_backend.analysis.pose import real_evidence_entries
        from tennis_backend.evidence.render import (
            render_evidence,
            render_skeleton_overlay,
            render_wrist_trajectory,
        )

        records = self._real_records(ctx, params.task_id, params.version)
        media = records["media"]
        if media is None or not media.time_mapping_valid:
            raise ContractViolation(
                "standardization failed; no evidence can be rendered (EX-043)",
                code="RENDER_STANDARDIZATION_FAILED",
            )
        relative_dir = f"projects/{params.task_id}/v{params.version}/evidence"
        wanted_ids = set(params.evidence_ids)
        sheet_prefix = f"sheet-{params.task_id}-v{params.version}-"
        if params.mode == "frame" and any(eid.startswith("sheet-") for eid in wanted_ids):
            return self._render_swing_sheets(params, records, media, wanted_ids, sheet_prefix, ctx)
        if params.mode == "frame":
            entries = real_evidence_entries(
                task_id=params.task_id,
                version=params.version,
                media=media,
                selection=records["selection"],
            )
            wanted = [entry for entry in entries if entry["evidence_id"] in wanted_ids]
            if not wanted:
                raise ContractViolation(
                    "none of the requested evidence ids is published for this task",
                    code="RENDER_EVIDENCE_UNKNOWN",
                )
            rendered = render_evidence(
                source_video=self._input_copy_path(
                    ctx["resources"], params.task_id, params.version
                ),
                root=self._public,
                relative_dir=relative_dir,
                evidence=tuple(wanted),
                mode="frame",
                simulation=False,
            )
            return rendered.result
        # overlay modes draw on the SAME frames the pose was extracted from
        skeleton_spec = records["skeleton_spec"]
        if skeleton_spec is None:
            raise ContractViolation(
                "no skeleton spec published for this task",
                code="POSE_RECORDS_MISSING",
            )
        # the overlay draws EVERYTHING measured: action frames + the idle
        # head/tail context frames (merged chronologically)
        overlay_records = sorted(
            [*records["pose_records"], *records["context_records"]],
            key=lambda record: record["frame"],
        )
        task_v = f"{params.task_id}-v{params.version}"
        traj_ids = {f"traj-{task_v}-wrist-left", f"traj-{task_v}-wrist-right"}
        subject_sets = records.get("subjects", {})
        overlay_ids = {f"pose-overlay-{task_v}", f"pose-overlay-{task_v}-slow2"}
        if subject_sets:
            overlay_ids |= {f"pose-overlay-{task_v}-all", f"pose-overlay-{task_v}-all-slow2"}
            for label in subject_sets:
                overlay_ids |= {
                    f"pose-overlay-{task_v}-{label}",
                    f"pose-overlay-{task_v}-{label}-slow2",
                }
        traj_requested = {eid for eid in wanted_ids if eid.startswith("traj-")}
        # validate ids BEFORE rendering: an illegal id must not leave an
        # orphan artifact behind (phase4 review D2)
        if traj_requested:
            if not traj_requested == wanted_ids or not traj_requested <= traj_ids:
                raise ContractViolation(
                    "none of the requested evidence ids is published for this task",
                    code="RENDER_EVIDENCE_UNKNOWN",
                )
        elif wanted_ids - overlay_ids:
            raise ContractViolation(
                "none of the requested evidence ids is published for this task",
                code="RENDER_EVIDENCE_UNKNOWN",
            )
        if traj_requested:
            # the wrist keypoint index comes from the PUBLISHED topology, not
            # a hardcoded COCO guess (phase4 review D4)
            name_to_index = {
                str(keypoint["name"]): int(keypoint["index"])
                for keypoint in skeleton_spec.get("keypoints", [])
            }
            outcomes = []
            for side in ("left", "right"):
                if f"traj-{task_v}-wrist-{side}" not in traj_requested:
                    continue
                index = name_to_index.get(f"{side}_wrist")
                if index is None:
                    raise ContractViolation(
                        f"published topology has no {side}_wrist keypoint",
                        code="POSE_RECORDS_MISSING",
                    )
                outcomes.append(
                    render_wrist_trajectory(
                        source_video=self._standardized_path(ctx, params.task_id, params.version),
                        pose_records=overlay_records,
                        keypoint_index=index,
                        part="wrist",
                        root=self._public,
                        relative_dir=relative_dir,
                        task_id=params.task_id,
                        version=params.version,
                        side=side,
                    )
                )
            return {
                "resources": [r for o in outcomes for r in o.result["resources"]],
                "evidence": [],
                "simulation": False,
            }
        # which overlay the id asks for: the flat primary set (historical),
        # ALL analyzed subjects in one picture, or one named subject
        slow = "-slow2"
        requested_overlay = next(iter(wanted_ids)) if wanted_ids else ""
        base_id = requested_overlay.removesuffix(slow)
        tag = base_id.removeprefix(f"pose-overlay-{task_v}")  # "", "-all", "-track-33"
        subjects_arg: list[tuple[str, list[dict[str, Any]]]] | None = None
        if tag == "-all":
            subjects_arg = [
                (
                    label,
                    sorted(
                        [
                            *sets.get("pose-frames.jsonl", []),
                            *sets.get("pose-frames-context.jsonl", []),
                        ],
                        key=lambda record: record["frame"],
                    ),
                )
                for label, sets in subject_sets.items()
            ]
        elif tag:
            label = tag.removeprefix("-")
            sets = subject_sets[label]
            overlay_records = sorted(
                [*sets.get("pose-frames.jsonl", []), *sets.get("pose-frames-context.jsonl", [])],
                key=lambda record: record["frame"],
            )
        overlay = render_skeleton_overlay(
            source_video=self._standardized_path(ctx, params.task_id, params.version),
            pose_records=overlay_records,
            skeleton_spec=skeleton_spec,
            root=self._public,
            relative_dir=relative_dir,
            task_id=params.task_id,
            version=params.version,
            mapping=[point.model_dump() for point in media.time_mapping],
            slow_factor=2.0 if params.mode == "slow_motion" else 1.0,
            subjects=subjects_arg,
            file_tag=tag,
        )
        return overlay.result

    def _render_swing_sheets(
        self,
        params: Any,
        records: dict[str, Any],
        media: Any,
        wanted_ids: set[str],
        prefix: str,
        ctx: dict[str, Any],
    ) -> dict[str, Any]:
        """Render swing contact sheets: ``sheet-<task>-v<n>-swing-<seg>-<k>``
        (flat primary set) or ``sheet-<task>-v<n>-<track-N>-swing-<seg>-<k>``.

        A sheet shows several moments of ONE swing on the STANDARDIZED video
        (the frames the pose was extracted from), skeleton drawn only where
        pose exists. All ids are validated against the published swings
        BEFORE anything is rendered, so a bad id leaves no orphan file.
        """
        from tennis_backend.analysis.swings import sheet_frames
        from tennis_backend.evidence.render import render_contact_sheet

        skeleton_spec = records["skeleton_spec"]
        if skeleton_spec is None:
            raise ContractViolation(
                "no skeleton spec published for this task", code="POSE_RECORDS_MISSING"
            )
        # id -> (swing summary, records to draw)
        catalog: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        flat_records = sorted(
            [*records["pose_records"], *records["context_records"]], key=lambda r: r["frame"]
        )
        for swing in records["swings"].get("swings", []):
            catalog[f"{prefix}{swing['swing_id']}"] = (swing, flat_records)
        for label, sets in records.get("subjects", {}).items():
            own = sorted(
                [*sets.get("pose-frames.jsonl", []), *sets.get("pose-frames-context.jsonl", [])],
                key=lambda r: r["frame"],
            )
            for swing in sets.get("swings.json", {}).get("swings", []):
                catalog[f"{prefix}{label}-{swing['swing_id']}"] = (swing, own)

        unknown = sorted(wanted_ids - set(catalog))
        if unknown:
            raise ContractViolation(
                "none of the requested evidence ids is published for this task",
                code="RENDER_EVIDENCE_UNKNOWN",
            )
        source = self._standardized_path(ctx, params.task_id, params.version)
        mapping = [point.model_dump() for point in media.time_mapping]
        relative_dir = f"projects/{params.task_id}/v{params.version}/evidence"
        out_resources: list[dict[str, Any]] = []
        for evidence_id in sorted(wanted_ids):
            swing, draw_records = catalog[evidence_id]
            outcome = render_contact_sheet(
                source_video=source,
                pose_records=draw_records,
                skeleton_spec=skeleton_spec,
                cells=sheet_frames(swing),
                root=self._public,
                relative_dir=relative_dir,
                sheet_id=evidence_id,
                mapping=mapping,
            )
            out_resources.extend(outcome.result["resources"])
        return {"resources": out_resources, "evidence": [], "simulation": False}

    def _target(self, request: ValidatedRequest) -> dict[str, Any]:
        from tennis_backend.contracts.validation import TargetInput
        from tennis_backend.evidence.render import generate_target_skeleton

        params = request.typed_params(TargetInput)
        return generate_target_skeleton(
            root=self._public,
            relative_dir=f"projects/{params.task_id}/v{params.version}/target",
            task_id=params.task_id,
            version=params.version,
            features=list(params.features),
        )

    def _compare(self, request: ValidatedRequest) -> dict[str, Any]:
        from tennis_backend.contracts.validation import CompareInput

        params = request.typed_params(CompareInput)
        with self._services() as ctx:
            # Phase 5: real tasks have no comparability gate yet (V1 metrics
            # are camera-relative 2D proxies without device/mirror/protocol
            # configuration) — an honest stable refusal, never a fixture
            # comparison posing as the user's result
            if (
                self._task_analysis_mode(ctx, params.task_id, params.version) == "real_video"
                or self._task_analysis_mode(ctx, params.other_task_id, params.other_version)
                == "real_video"
            ):
                raise ContractViolation(
                    "real-video comparison needs the comparability gate "
                    "(device/mirror/protocol configuration), not implemented in V1",
                    code="COMPARE_REAL_UNSUPPORTED",
                )
            adapter: FixtureAnalysisAdapter = ctx["adapter"]
            baseline = self._session_snapshot(ctx, adapter, params.task_id, params.version)
            retest = self._session_snapshot(
                ctx, adapter, params.other_task_id, params.other_version
            )
            return compare_sessions(baseline, retest)

    def _export(self, request: ValidatedRequest) -> dict[str, Any]:
        from tennis_backend.contracts.validation import ExportInput

        params = request.typed_params(ExportInput)
        with self._services() as ctx:
            resources: ResourceStore = ctx["resources"]
            service: TaskService = ctx["service"]
            # the wire consent must cover diagnostics for this exact version
            consent = self._consent_snapshot(params.consent, params.task_id, params.version)
            service.check_consent(
                consent=consent,
                purpose="diagnostics",
                task_id=params.task_id,
                version=params.version,
            )
            materials: list[SelectedMaterial] = []
            for resource_id in params.selected_resource_ids:
                resource = resources.resource(resource_id)
                materials.append(
                    SelectedMaterial(
                        resource_id=resource.resource_id,
                        path=resources.resource_path(resource),
                        relative=resource.path,
                        kind=resource.kind,
                        mime=resource.mime,
                    )
                )
            outcome = build_diagnostic_package(
                package_id=params.operation_id,
                root=self._public,
                relative_dir=f"projects/{params.task_id}/v{params.version}/export",
                task_id=params.task_id,
                version=params.version,
                selected=tuple(materials),
            )
            return outcome.result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _neutral_probe(self, summary: dict[str, Any] | None = None) -> MediaProbe:
        """A metadata stand-in built from the stored summary when available."""
        from tennis_backend.domain.models import MediaProbe

        if summary is None:
            return MediaProbe(
                container="mp4",
                codec="h264",
                duration_s=3.0,
                fps=30.0,
                width=1920,
                height=1080,
                rotation="0",
                slow_motion="unknown",
                hdr="no",
                device_source="unknown",
                panorama="flat",
                observation_kind="measured",
            )
        return MediaProbe(
            container="mp4",
            codec="h264",
            duration_s=3.0,
            fps=30.0,
            width=320,
            height=240,
            rotation="0",
            slow_motion="unknown",
            hdr="no",
            device_source="unknown",
            panorama="flat",
            observation_kind="measured",
        )

    def _services_open(self) -> None:  # pragma: no cover - documentation anchor
        return None

    def _task_fixture(self, ctx: dict[str, Any], task_id: str, version: int) -> str:
        """Read the declared fixture from the durable task-config record."""
        resources: ResourceStore = ctx["resources"]
        for resource in resources.resources(task_id, version):
            if resource.kind == "record" and resource.path.endswith("task-config.json"):
                payload = json.loads(resources.resource_path(resource).read_text(encoding="utf-8"))
                fixture_id = str(payload.get("fixture_id", ""))
                self._fixtures.sample(fixture_id)  # unknown: refused
                return fixture_id
        raise DomainViolation(
            "TASK_CONFIG_MISSING",
            f"task {task_id} has no durable task-config record; re-create the task",
        )

    def _input_copy_path(self, resources: ResourceStore, task_id: str, version: int) -> Path:
        for resource in resources.resources(task_id, version):
            if resource.kind == "input_copy":
                return resources.resource_path(resource)
        raise DomainViolation(
            "INPUT_COPY_MISSING",
            f"task {task_id} has no registered input copy; media ingest is a separate step",
        )

    # ------------------------------------------------------------------
    # Real-video query/render support (Phase 4)
    # ------------------------------------------------------------------

    def _task_analysis_mode(self, ctx: dict[str, Any], task_id: str, version: int) -> str:
        """The durable analysis mode of this task version (fixture default)."""
        payload = self._task_config_payload(ctx, task_id, version)
        return str(payload.get("analysis_mode", "fixture"))

    def _ball_config_payload(
        self, ctx: dict[str, Any], task_id: str, version: int
    ) -> dict[str, Any]:
        """Task config merged with the ball-evidence and detector-prompt records.

        The evidence/estimate handlers need the detector, its origin (expert
        or fallback), the frame geometry and the prompt state; those live in
        records published at different pipeline stages, so they are layered
        here in publication order.
        """
        payload = dict(self._task_config_payload(ctx, task_id, version))
        resources: ResourceStore = ctx["resources"]
        known = {resource.path for resource in resources.resources(task_id, version)}
        base = f"projects/{task_id}/v{version}/records"
        for name in ("detector-prompt.json", "ball-evidence.json"):
            relative = f"{base}/{name}"
            if relative not in known:
                continue
            path = self._public / relative
            try:
                payload.update(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return payload

    # ------------------------------------------------------------------
    # Ball evidence (contract 2.0.0): detector prompts and retrieval
    # ------------------------------------------------------------------

    def _detector_prompts(self, media_ref: str) -> int:
        """How often the expert has been asked for this video's detector."""
        path = self._internal / "detector-prompts.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        return int(data.get(media_ref, 0)) if isinstance(data, dict) else 0

    def _bump_detector_prompts(self, media_ref: str, *, reset: bool) -> None:
        """Count one unanswered prompt, or clear the count after an answer."""
        path = self._internal / "detector-prompts.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        if reset:
            data.pop(media_ref, None)
        else:
            data[media_ref] = int(data.get(media_ref, 0)) + 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def _record_detector_prompt(self, media_ref: str) -> None:
        self._bump_detector_prompts(media_ref, reset=False)

    def _clear_detector_prompt(self, media_ref: str) -> None:
        self._bump_detector_prompts(media_ref, reset=True)

    def _evidence_resource(
        self, path: Path, relative: str, kind: str, resource_id: str, *, simulation: bool
    ) -> dict[str, Any]:
        import hashlib

        return {
            "resource_id": resource_id,
            "path": relative,
            "mime": "image/jpeg",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
            "kind": kind,
            "simulation": simulation,
        }

    def _evidence_get(self, request: ValidatedRequest) -> dict[str, Any]:
        """``evidence.get``: whole intervals of stored frames, never verdicts."""
        from tennis_backend.contracts.validation import EvidenceInput

        params = request.typed_params(EvidenceInput)
        with self._services() as ctx:
            config = self._ball_config_payload(ctx, params.task_id, params.version)
            detector = str(config.get("detector") or "coco")
            # caps are backend constants (user decision 2026-11-16): near-view
            # action review 200, far-view trajectory 600
            frame_limit = 600 if detector == "wasb" else 200

            def empty(reason: str, message: str) -> dict[str, Any]:
                return {
                    "frames": [],
                    "candidates": [],
                    "trajectory": [],
                    "window_level": None,
                    "truncated": False,
                    "reason": reason,
                    "message": message,
                    "simulation": request.simulation,
                }

            store_path = self._internal / "evidence.sqlite"
            if not store_path.is_file():
                return empty("not_retained", "该任务未保存球证据（检测未运行或任务较早）")
            from tennis_backend.storage.evidence_store import EvidenceStore

            with EvidenceStore(store_path) as store:
                interval = store.retrieve(
                    params.task_id,
                    params.version,
                    now=self._now(),
                    candidate_id=params.candidate_id,
                    frame_start=params.frame_start,
                    frame_end=params.frame_end,
                    frame_limit=frame_limit,
                )
                stored_candidates = store.candidates(params.task_id, params.version)
            if interval.reason is not None and not interval.frames:
                return empty(interval.reason, interval.message)

            fps = float(config.get("fps") or 30.0)
            width = int(config.get("width") or 0)
            height = int(config.get("height") or 0)
            frames_out: list[dict[str, Any]] = []
            trajectory: list[dict[str, Any]] = []
            zoom_wanted = params.include_zoom
            for item in interval.frames:
                image_path = self._public / (
                    f"projects/{params.task_id}/v{params.version}/evidence/frames/"
                    f"f{item.frame:06d}.jpg"
                )
                if not image_path.is_file():
                    continue  # metadata without its image is not returned
                base = f"ev-{params.task_id}-v{params.version}-f{item.frame:06d}"
                zoom_rel: str | None = None
                zoom_payload = None
                if zoom_wanted:
                    zoom_path = self._public / (
                        f"projects/{params.task_id}/v{params.version}/evidence/frames/"
                        f"f{item.frame:06d}-zoom.jpg"
                    )
                    if zoom_path.is_file():
                        zoom_rel = (
                            f"projects/{params.task_id}/v{params.version}/evidence/frames/"
                            f"f{item.frame:06d}-zoom.jpg"
                        )
                        zoom_payload = self._evidence_resource(
                            zoom_path,
                            zoom_rel,
                            "evidence_zoom",
                            f"{base}-zoom",
                            simulation=request.simulation,
                        )
                frames_out.append(
                    {
                        "frame": item.frame,
                        "time_s": round(item.time_s, 3),
                        "image": self._evidence_resource(
                            image_path,
                            f"projects/{params.task_id}/v{params.version}/evidence/frames/"
                            f"f{item.frame:06d}.jpg",
                            "evidence_frame",
                            base,
                            simulation=request.simulation,
                        ),
                        "zoom": zoom_payload,
                        "ball_box": None if item.ball is None else [round(v, 1) for v in item.ball],
                        "ball_confidence": (
                            None if item.ball_confidence is None else round(item.ball_confidence, 3)
                        ),
                        "racket_box": (
                            None if item.racket is None else [round(v, 1) for v in item.racket]
                        ),
                        "racket_confidence": (
                            None
                            if item.racket_confidence is None
                            else round(item.racket_confidence, 3)
                        ),
                        "pose_valid": item.pose_valid,
                        "markers": list(item.markers),
                        "candidate_id": item.candidate_id,
                        "frames_to_anchor": item.frames_to_anchor,
                    }
                )
                if item.ball is not None and width > 0 and height > 0:
                    trajectory.append(
                        {
                            "frame": item.frame,
                            "original_s": round(item.time_s, 3),
                            "x": round(
                                max(0.0, min(1.0, (item.ball[0] + item.ball[2]) / 2 / width)), 4
                            ),
                            "y": round(
                                max(0.0, min(1.0, (item.ball[1] + item.ball[3]) / 2 / height)), 4
                            ),
                            "source": item.ball_source or "detected",
                            "peak": (
                                None
                                if item.ball_confidence is None
                                else round(item.ball_confidence, 3)
                            ),
                        }
                    )
            candidates_out = []
            for candidate in stored_candidates:
                seconds = candidate.interval_start / fps
                end_seconds = candidate.interval_end / fps
                candidates_out.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "interval_start_frame": candidate.interval_start,
                        "interval_end_frame": candidate.interval_end,
                        "original_s": {
                            "start": round(seconds, 3),
                            "end": round(end_seconds, 3),
                        },
                        "basis": candidate.basis,
                        "basis_error_frames": candidate.basis_error_frames,
                        "confidence": round(candidate.confidence, 3),
                        "window_level": candidate.window_level,
                    }
                )
            return {
                "frames": frames_out,
                "candidates": candidates_out,
                "trajectory": trajectory,
                "window_level": interval.window_level,
                "truncated": interval.truncated,
                "reason": interval.reason,
                "message": interval.message,
                "simulation": request.simulation,
            }

    def _estimate_storage(self, request: ValidatedRequest) -> dict[str, Any]:
        """``analysis.estimate_storage``: frames and bytes for the user's choice."""
        from tennis_backend.application.evidence_run import estimate as run_estimate
        from tennis_backend.contracts.validation import StorageInput

        params = request.typed_params(StorageInput)
        with self._services() as ctx:
            config = self._ball_config_payload(ctx, params.task_id, params.version)
            media_ref = config.get("media_ref")
            if not media_ref:
                raise ContractViolation(
                    "task has no media_ref; storage needs the input video",
                    code="INGEST_VIDEO_MISSING",
                )
            video = (self._data_root / str(media_ref)).resolve()
            if not video.is_relative_to(self._data_root) or not video.is_file():
                raise ContractViolation(
                    f"input video {media_ref} is missing from the data root",
                    code="INGEST_VIDEO_MISSING",
                )
            estimate = run_estimate(
                video,
                route=str(config.get("primary_route") or "technical_diagnosis"),
                retain_mode=params.retain_mode,
                detector=config.get("detector"),
                subject_height_ratio=config.get("subject_height_ratio"),
            )
            return {
                "mode": estimate["mode"],
                "retained_frames": estimate["retained_frames"],
                "estimated_bytes": estimate["estimated_bytes"],
                "ranges": estimate["ranges"],
                "requires_confirmation": estimate["requires_confirmation"],
                "simulation": request.simulation,
            }

    def _real_records(self, ctx: dict[str, Any], task_id: str, version: int) -> dict[str, Any]:
        """Load the published pose records for one real task version.

        A missing pose-frames.jsonl is a stable refusal (FR-POSE-006): the
        real path never falls back to fixture data.
        """
        from tennis_backend.domain.models import MediaAsset

        resources: ResourceStore = ctx["resources"]
        paths: dict[str, Path] = {}
        flat_dir = f"projects/{task_id}/v{version}/records"
        for resource in resources.resources(task_id, version):
            name = Path(resource.path).name
            # ONLY the flat primary set: per-subject sets live under
            # records/subjects/<id>/ with the same file names and must not
            # shadow it (联调 multi-subject — they are read via subject_records)
            if (
                resource.kind == "record"
                and resource.path == f"{flat_dir}/{name}"
                and name
                in (
                    "pose-frames.jsonl",
                    "pose-frames-context.jsonl",
                    "motion-trajectories.json",
                    "motion-angles.jsonl",
                    "action-selection.json",
                    "segment-states.json",
                    "media-asset.json",
                    "skeleton-spec.json",
                    "swings.json",
                )
            ):
                paths[name] = resources.resource_path(resource)
        if "pose-frames.jsonl" not in paths:
            raise DomainViolation(
                "POSE_RECORDS_MISSING",
                f"task {task_id} v{version} has no published pose records; "
                "the real query path never falls back to fixture data",
            )
        pose_records = [
            json.loads(line)
            for line in paths["pose-frames.jsonl"].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        angles_per_frame = (
            [
                json.loads(line)
                for line in paths.get("motion-angles.jsonl", Path())
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            if "motion-angles.jsonl" in paths
            else []
        )
        trajectories: dict[str, Any] = {}
        summary: dict[str, Any] = {}
        if "motion-trajectories.json" in paths:
            traj_payload = json.loads(paths["motion-trajectories.json"].read_text(encoding="utf-8"))
            trajectories = traj_payload.get("trajectories", {})
            summary = traj_payload.get("summary", {})
        selection = (
            json.loads(paths["action-selection.json"].read_text(encoding="utf-8"))
            if "action-selection.json" in paths
            else {"selected_segments": []}
        )
        segment_states = (
            json.loads(paths["segment-states.json"].read_text(encoding="utf-8"))
            if "segment-states.json" in paths
            else []
        )
        media = (
            MediaAsset.model_validate(json.loads(paths["media-asset.json"].read_text("utf-8")))
            if "media-asset.json" in paths
            else None
        )
        skeleton_spec = (
            json.loads(paths["skeleton-spec.json"].read_text("utf-8"))
            if "skeleton-spec.json" in paths
            else None
        )
        # idle head/tail context records (separate file so pose-frames.jsonl
        # stays scoped to the action segments the bridge validates against)
        context_records = (
            [
                json.loads(line)
                for line in paths["pose-frames-context.jsonl"]
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            if "pose-frames-context.jsonl" in paths
            else []
        )
        # per-subject sets (联调 multi-subject): records/subjects/<id>/...
        per_subject: dict[str, dict[str, Any]] = {}
        subjects_dir = f"{flat_dir}/subjects/"
        for resource in resources.resources(task_id, version):
            if resource.kind != "record" or not resource.path.startswith(subjects_dir):
                continue
            label, _, rest = resource.path[len(subjects_dir) :].partition("/")
            if rest not in (
                "pose-frames.jsonl",
                "pose-frames-context.jsonl",
                "action-selection.json",
                "segment-states.json",
                "metrics.json",
                "swings.json",
            ):
                continue
            text = resources.resource_path(resource).read_text(encoding="utf-8")
            entry = per_subject.setdefault(label, {})
            if rest.endswith(".jsonl"):
                entry[rest] = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                entry[rest] = json.loads(text)
        return {
            "_task_id": task_id,
            "_version": version,
            "subjects": per_subject,
            "swings": (
                json.loads(paths["swings.json"].read_text("utf-8"))
                if "swings.json" in paths
                else {"swings": [], "swing_count": 0}
            ),
            "pose_records": pose_records,
            "context_records": context_records,
            "motion": {
                "trajectories": trajectories,
                "angles_per_frame": angles_per_frame,
                "summary": summary,
            },
            "selection": selection,
            "segment_states": segment_states,
            "media": media,
            "skeleton_spec": skeleton_spec,
        }

    def _real_adapter(self, records: dict[str, Any]) -> Any:
        from tennis_backend.analysis.pose import RealPoseAdapter

        return RealPoseAdapter(
            pose_records=records["pose_records"],
            motion=records["motion"],
            selection=records["selection"],
            segment_states=records["segment_states"],
        )

    def _real_quality(self, ctx: dict[str, Any], records: dict[str, Any]) -> dict[str, Any]:
        """Honest media:quality for a real task: measured probe + pose state."""
        from tennis_backend.media.probe import probe_video

        resources: ResourceStore = ctx["resources"]
        source = self._input_copy_path(resources, records["_task_id"], records["_version"])
        summary = probe_video(source).summary
        deferred = [
            state
            for state in records["segment_states"]
            if state.get("status") == "deferred_multi_subject"
        ]
        issues: list[dict[str, Any]] = [
            {
                "code": "missing_ball",
                "object": "ball",
                "impact": "bounce and ball-dependent timing events cannot be judged",
                "evidence_ids": [],
                "reshoot_guidance": "Frame the ball trajectory inside the view and reshoot.",
            },
            {
                "code": "missing_racket",
                "object": "racket",
                "impact": "racket-phase timing events cannot be judged",
                "evidence_ids": [],
                "reshoot_guidance": "Keep the racket visible through the swing and reshoot.",
            },
        ]
        if deferred:
            issues.append(
                {
                    "code": "subject_selection_required",
                    "object": "subject",
                    "impact": f"{len(deferred)} segment(s) deferred: tracker identity unstable",
                    "evidence_ids": [],
                    "reshoot_guidance": "Shoot a single-person clip, or select one subject.",
                }
            )
        return {
            "status": "limited",
            "issues": issues,
            "unavailable_objects": ["ball", "racket"],
            "subject_candidates": [],
            "time_mapping_valid": bool(
                records["media"].time_mapping_valid if records["media"] else False
            ),
            "media": summary,
            "evidence": [],
            "simulation": False,
        }

    def _standardized_path(self, ctx: dict[str, Any], task_id: str, version: int) -> Path:
        resources: ResourceStore = ctx["resources"]
        for resource in resources.resources(task_id, version):
            if resource.path.endswith("media/standardized.mp4"):
                return resources.resource_path(resource)
        raise DomainViolation(
            "STANDARDIZED_MISSING",
            f"task {task_id} v{version} has no standardized derivative; re-create the task",
        )

    def _session_snapshot(
        self, ctx: dict[str, Any], adapter: FixtureAnalysisAdapter, task_id: str, version: int
    ) -> SessionSnapshot:
        from tennis_backend.domain.models import MediaProbe

        fixture_id = self._task_fixture(ctx, task_id, version)
        sample = self._fixtures.sample(fixture_id)
        request = StageRequest(
            task_id=task_id,
            version=version,
            fixture_id=fixture_id,
            media=MediaProbe(
                container="mp4",
                codec="h264",
                duration_s=3.0,
                fps=30.0,
                width=320,
                height=240,
                rotation="0",
                slow_motion="unknown",
                hdr="no",
                device_source="unknown",
                panorama="flat",
                observation_kind="measured",
            ),
        )
        token = CancellationToken()
        strokes = adapter.run("strokes", request, cancel=token)
        metrics = adapter.run("metrics", request, cancel=token)
        record = self._task_config_payload(ctx, task_id, version)
        return SessionSnapshot(
            task_id=task_id,
            version=version,
            fixture_id=fixture_id,
            device_scenario=str(sample["device_scenario"]),
            stroke_kind=str(strokes["events"][0]["kind"]),
            subject_id=record.get("subject_id"),
            config_version="fixture-config-1",
            contract_version=_FROZEN_CONTRACT_VERSION,
            mirror_confirmed=bool(record.get("mirror_confirmed", True)),
            safety="clear",
            protocol=tuple(sorted(record.get("protocol", [("camera", "flat")]))),
            metrics=tuple(metrics["metrics"]),
        )

    def _task_config_payload(
        self, ctx: dict[str, Any], task_id: str, version: int
    ) -> dict[str, Any]:
        resources: ResourceStore = ctx["resources"]
        for resource in resources.resources(task_id, version):
            if resource.kind == "record" and resource.path.endswith("task-config.json"):
                payload: dict[str, Any] = json.loads(
                    resources.resource_path(resource).read_text(encoding="utf-8")
                )
                return payload
        raise DomainViolation(
            "TASK_CONFIG_MISSING", f"task {task_id} has no durable task-config record"
        )

    def _consent_snapshot(self, wire: Any, task_id: str, version: int) -> ConsentSnapshot:
        """Map the wire consent onto the domain snapshot for authorization."""
        return ConsentSnapshot(
            consent_id=wire.consent_id,
            task_id=task_id,
            version=version,
            purposes=list(wire.purposes),
            text_version=wire.text_version,
            confirmed=wire.confirmed,
            guardian_required=wire.guardian_simulated,
            guardian_simulated=wire.guardian_simulated,
            confirmed_at=self._now() if wire.confirmed else None,
            recipient_digest=wire.recipient_digest,
            materials_digest=wire.materials_digest,
        )

    @staticmethod
    def _user_input(params: CreateInput) -> UserInput:
        user = params.user
        return UserInput(
            # the frozen wire create_input carries no question field; the
            # absence is recorded honestly instead of invented
            question=Grounded.absent("not_provided"),
            age_band=Grounded.observed(user.age_band),
            handedness=Grounded.observed(user.handedness),
            target_stroke=Grounded.observed(user.target_stroke),
            goal=Grounded.observed(user.goal),
            exercise_background=Grounded.observed(user.exercise_background),
            safety=Grounded.observed(user.safety),
        )

    def _stores(self) -> contextlib.AbstractContextManager[tuple[MetadataStore, OperationLedger]]:
        @contextlib.contextmanager
        def pair() -> Iterator[tuple[MetadataStore, OperationLedger]]:
            metadata = MetadataStore(self._internal / "metadata.sqlite")
            ledger = OperationLedger(self._internal / "operations.sqlite")
            try:
                yield metadata, ledger
            finally:
                ledger.close()
                metadata.close()

        return pair()


def _now() -> datetime:
    """Wall-clock UTC stamp for task bookkeeping."""
    return datetime.now(UTC)


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.version:
        print(f"tennis-backend {__version__} contract {CONTRACT_VERSION}")
        return 0
    if args.stdio:
        return _run_stdio()
    parser.print_help(sys.stderr)
    return 2


def _run_stdio() -> int:
    root_env = os.environ.get("TENNIS_DATA_ROOT")
    if not root_env:
        print(f"tennis-backend: {_STDIO_ROOT_REQUIRED}", file=sys.stderr)
        return 2
    root = Path(root_env).expanduser().resolve()
    # the chosen root is the only place this process writes to
    (root / "internal").mkdir(parents=True, exist_ok=True)
    (root / "public").mkdir(parents=True, exist_ok=True)
    registry = ContractRegistry.load()
    handler = StdioHandler(registry, root)
    return serve_stdio(registry, handler)


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    raise SystemExit(main())
