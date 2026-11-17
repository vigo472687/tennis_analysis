# Codex-Claude Contract V2

Status: V2 frozen on 2026-11-17. Contract version: `2.0.0`. Configuration: `fixture-config-1`. See integration/reports/contract-baseline.json for the 20 frozen hashes.

JSON Schema draft-07 files in `v1/` are authoritative. Each schema has a stable `$id`; references resolve locally and never fetch the network. Codex owns changes. Claude requests changes rather than editing these files.

## Transport

One UTF-8 JSON object per line on stdin/stdout. Diagnostics go to stderr. Requests and responses follow `common:request` and `common:response`. Validate the envelope, resolve method, then validate the method-specific params/result. Return structured errors, not stack traces. A request ID is unique to an attempt; operation ID stays stable across retries of the same side effect and must match params.operation_id. Duplicate operation with different inputs is rejected. Default limits are bounded local configuration and are not accuracy claims.

`x-tools` is the machine-readable registry. `file:definition` addresses another schema; a bare definition belongs to the registry file. No second tool registry should be manually maintained.

| MCP tool | NDJSON method | Input definition | Output definition | Side effect | Purpose |
|---|---|---|---|---|---|
| runtime_status | runtime.status | runtime:status_input | runtime:result | false | none/new consent not implied |
| runtime_prepare | runtime.prepare | runtime:prepare_input | runtime:result | true | none/new consent not implied |
| runtime_start | runtime.start | runtime:start_input | runtime:result | true | none/new consent not implied |
| runtime_repair | runtime.repair | runtime:repair_input | runtime:result | true | none/new consent not implied |
| runtime_uninstall | runtime.uninstall | runtime:uninstall_input | runtime:result | true | none/new consent not implied |
| create_video_analysis | analysis.create | task:create_input | task:summary | true | analysis |
| get_analysis_status | analysis.status | task:lookup | task:summary | false | none/new consent not implied |
| cancel_analysis | analysis.cancel | task:cancel_input | task:summary | true | none/new consent not implied |
| get_quality_report | analysis.quality | task:lookup | media:quality | false | none/new consent not implied |
| get_stroke_events | analysis.strokes | task:lookup | analysis:strokes_result | false | none/new consent not implied |
| get_groundstroke_timing | analysis.timing | task:lookup | analysis:timing_result | false | none/new consent not implied |
| get_motion_metrics | analysis.metrics | task:lookup | analysis:metrics_result | false | none/new consent not implied |
| get_evidence | evidence.get | analysis:evidence_input | analysis:evidence_result | false | none/new consent not implied |
| estimate_storage | analysis.estimate_storage | analysis:storage_input | analysis:storage_estimate | false | none/new consent not implied |
| render_evidence_overlay | evidence.render | analysis:render_input | analysis:render_result | true | analysis |
| generate_target_pose | target.generate | analysis:target_input | analysis:target_result | true | analysis |
| compare_sessions | sessions.compare | analysis:compare_input | analysis:compare_result | false | retest |
| export_review_package | review.export | deletion:export_input | deletion:export_result | true | diagnostics |
| delete_analysis_data | analysis.delete | deletion:delete_input | deletion:delete_result | true | none/new consent not implied |

## Lifecycle and authorization

- Bridge owns runtime methods. Python implements business methods; it does not start itself or call WorkBuddy. `runtime_prepare.intent` distinguishes prepare, upgrade and rollback, without adding tools. Runtime uninstall never silently deletes task data; call separately confirmed analysis.delete.
- `analysis.create` returns immediately with task_id/version/state and a public `workspace_ref`. Work happens off the input loop: status and cancel remain responsive. Process exit uses the last complete checkpoint. A retry with the same operation/input returns the same task rather than creating a duplicate.
- Analysis consumes only selected media and a fresh consent snapshot. Match consent.task_version to task version, confirmed=true and required purpose. Youth requires guardian_simulated=true for sensitive operations. Purpose, recipient or material changes invalidate the relevant confirmation; new tasks do not inherit external-send consent.
- Media references are relative to explicitly authorized roots. Never concatenate a user shell command. Do not send raw media or large artifacts in JSON. Original user-owned files are never deleted as task copies.
- Tasks can be cancelled at every nonterminal stage. Repeated cancellation returns the same result. Existing terminal states are unchanged. Cancellation is not deletion.
- analysis.delete scope=task uses task_id/version. scope=all_demo resets all demo projects and demo state, not installed runtime or user originals. It blocks further writes before deleting and returns per-item failures. Exported external copies remain uncontrolled.
- metadata, evidence resources and public report/training/coach outputs must belong to the task version and be enumerated for deletion. WorkBuddy Skills may use the public workspace only; SQLite and backend-internal storage remain private.

## Evidence semantics

All times are seconds on the original timeline unless explicitly named normalized_s. Validate nonnegative ordered intervals against duration and selected subject. Exclude unmappable intervals. Missing feet prohibit support/drive/footwork claims; missing ball prohibits bounce/incoming timing/contact; missing racket prohibits head trajectory/drop. Unknown actions stay unable. Confidence and thresholds are fixture configuration, not measured accuracy.

Evidence.kind and its resource.kind exclude target_pose. Target resources have their own result type. relative metric units exclude physical force/torque/speed. Each formal report problem references valid evidence, with impact, limitations and alternative explanations; max three. External sources explain knowledge and never substitute for personal evidence. Safety-stop/rehab/uncertain reports cannot carry individual training loads.

`evidence.get` retrieves a complete retained interval by candidate ID or by an inclusive frame range. Callers cannot set window size or frame count: the backend fixes the widening ladder at 20/40/80 frames and caps near-view/far-view returns at 200/600 frames. Whole-frame images use `evidence_frame`; each optional ball close-up uses the distinct `evidence_zoom` resource kind. Boxes, confidence, markers and candidates describe detections only and never assert a hit. `estimated` trajectory points and missing trajectory spans stay distinguishable from measured points and do not enter metrics.

`create_input.retain_mode` defaults to `route_trimmed`. Before a caller submits `retain_mode=full`, it must call `analysis.estimate_storage`, present the estimated frames/bytes/ranges to the user, and obtain explicit confirmation. The estimate is not a storage guarantee. `detector` accepts only `coco` or `wasb`; absent values follow the bounded backend prompt/fallback path and must be labelled as fallback when applicable.

`simulation=true` is mandatory for fixed analysis or platform substitutes. Probe metadata may be measured but must not imply real pose inference. Distinguish synthetic device scenarios from real device captures.

## Runtime entry and modes

Python dev launch: `uv run --frozen --project <local-backend> tennis-backend --stdio`. Paths are configured by the Bridge, not assumed from cwd. Default mode is fixture. No real visual-model adapter, network research, email or final training prose belongs to the backend. Missing prerequisites return a repairable capability error.

## Response example

```json
{"contract_version":"2.0.0","request_id":"req-1","ok":false,"result":null,"error":{"code":"TASK_NOT_FOUND","stage":"input","retryable":false,"user_action":"none","retained":[],"details_ref":null,"simulation":true},"simulation":true}
```

## Versions and sources

Optional additions are minor; semantic/required-field/removal changes are major. Frozen schemas and fixture inputs are fingerprinted in integration/reports/contract-baseline.json. The current docs describe prototype behavior; real WorkBuddy/devices/models are deferred.

- `2.0.0` (2026-11-17): evidence retrieval and storage-estimation tools; hit-candidate and ball-trajectory definitions; dedicated evidence zoom resources; create-time retention and detector selection. The real backend must wire these methods before V2 live integration is claimed.

- [Official MCP SDK](https://github.com/modelcontextprotocol/typescript-sdk/tree/v1.x)
- [WorkBuddy expert team](https://open.workbuddy.cn/docs/expert-team)
- [WorkBuddy skills](https://open.workbuddy.cn/docs/skill)


All four synthetic device scenarios may share identical generated bytes. Their scenario and stroke labels are fixture inputs, never classifier measurements. Production camera compatibility and model effectiveness are unverified.

