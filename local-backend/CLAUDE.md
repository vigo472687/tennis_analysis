# Tennis local backend

You implement only the Python local backend. Codex owns WorkBuddy experts, Skills, Node MCP Bridge, contracts and integration. Current delivery is a fixed-fixture prototype, not a measured tennis model.

## Start

1. Read [handoff](<../003 SDD/claude-handoff.md>), [execution rules](<../003 SDD/execution-guide.md>), [constitution](<../003 SDD/constitution.md>), [spec](<../003 SDD/spec.md>) and [plan](<../003 SDD/plan.md>).
2. Read [contract README](../contracts/README.md), all nine schemas and [frozen hashes](../integration/reports/contract-baseline.json). Verify hashes before changing code. Resolve references locally; do not edit contracts.
3. Start only the requested batch's individual BE task files in ../003 SDD/tasks/. Obey prerequisites and each task's 1-2 delivery-file scope. B0 is BE-001/002/003, then BE-029.
4. Tests first where practical. Record actual commands, results and limitations. Use ready_for_review for task submission; Codex fills review/passed and checkpoint results. Update HANDOFF_RESULT.md only in the dedicated batch-record task.

## Boundaries

- Code/config/tests only in local-backend/. Progress exception: own BE task metadata status/updated_at/execution and execution notes only. Do not edit task definitions, dependencies, review, stages, human gates or dashboard.
- Python 3.11, uv locked dependencies, Pydantic 2, SQLite, local FFmpeg/ffprobe and OpenCV. A JSON Schema validator may be used alongside typed models to honor the frozen wire contracts exactly; refs stay local. No remote backend or WorkBuddy SDK.
- Entry: uv run --frozen --project local-backend tennis-backend --stdio, independent of cwd. One JSON line per request/response, stdout protocol only. Status/cancel must remain responsive during long work.
- Backend emits structured observations, resources, restrictions and state. Final teaching prose, routing, research, email and real external delivery belong elsewhere.
- All fixture analysis is simulation=true. Probe metadata may be genuinely measured; do not turn that into a real pose claim. Do not infer missing feet/ball/racket, physical force, professional ratings or medical outcomes.
- Paths stay inside authorized project/public-output roots. Preserve originals. Operation IDs, checkpoints, repeated cancellation, per-item deletion and public report/record cleanup are required.
- A contract gap becomes a CONTRACT_CHANGE_REQUEST in HANDOFF_RESULT.md, never a local incompatible workaround.

## 开箱即用宪法（最高优先级，不得放宽、不得绕过）

**凡是推送到 GitHub 的后端，必须做到：专家（或任何人）`git clone` 之后，不做任何手工配置，直接能运行。**
典型场景：用户上传视频并问"击球点是否偏后"，专家判断需要调后端，只需从 GitHub 拉取后端并启动，无需任何额外步骤。

1. **依赖全配好**：运行时依赖必须在 `pyproject.toml` 的 `dependencies`（或随仓库的 extra）里并锁进 `uv.lock`；
   启动时 import 的包不得只放在 dev 组。新增 import 即同步更新依赖与锁文件。
2. **权重全随仓库**：所有模型权重在 `models/` 下并入 git（超过 100 MB 走 Git LFS，规则写进 `.gitattributes`）；
   `models/README.md` 记录大小、sha256、来源与许可。禁止运行时联网下载权重，禁止依赖 `~/` 下的缓存。
3. **启动所需文件全入库**：后端启动、`check.sh` 通过所需的契约、fixtures、哈希基线、`pyproject` 引用的文件，一律入库；
   不得被 `.gitignore` 排除。测试视频与开发过程文档不入库，但后端不得依赖它们才能启动。
4. **环境步骤写成命令**：根 `README.md` 的"环境要求"列出全部前置（Python、uv、Git LFS、FFmpeg 等）及一条可复制的安装命令；
   缺失时后端必须以稳定错误码拒绝并说明修复命令（如 `git lfs pull`），不得静默失败或中途崩溃。
   第三方二进制因许可不能分发时（如 FFmpeg），必须在用户同意后由安装命令获取，并写进 README。
5. **离线可运行**：全部就绪后，运行期间不需要联网。发布版另提供离线包（`packaging/build_bundle.py`，Mac 与 Windows）。
6. **推送前必须验证**：在干净目录 `git clone`（含 LFS）后，用空环境实际执行 `--version`、`runtime.status` 与一次真实检测，
   结果写进提交说明。没有做过这一步，就不得声称"开箱即用"，也不得推送。未验证的平台必须明写"未验证"。
7. **违反即阻断**：任何提交若引入"要手工装/下/配"的步骤，视为缺陷，先修复再继续其他工作；
   不得以"后续再补文档"为由合入。新增后端能力时，同步检查本条 1–6。

## Checks

Run the batch's available focused tests, then ruff format --check, ruff check, mypy src and pytest tests with uv run --frozen. Missing later-batch tests are not success claims. Keep caches/test media under /tmp or this backend, and do not expose credentials.
