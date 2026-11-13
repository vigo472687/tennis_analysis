# 网球视频体验工作台

## Phase 1 后端基座验收（当前交接入口）

对应 `005 后端重构/handoffs/phase-1.md` 的新工作台：

```sh
cd "/Users/vigo/Documents/18.网瘾社区/workbuddy-plugin/bridge"
npm run phase1-console
```

默认入口 `http://127.0.0.1:4180/`，端口可用 `TENNIS_PHASE1_PORT` 指定。页面配置数据根后实际启动 Python stdio 子进程，提供原始 NDJSON/Params 编辑、响应树、13 项真实健康检查、fixture create/status/幂等、错误恢复及脱敏 stderr。未配置测试会实际验证 exit 2，不写模拟回执。

“运行协议检查”串联九项实际检查；结果不等于用户目视或 Windows 实机验收。选型页播放既有的 RF-DETR/ByteTrack/RTMW-x 隔离验证资料，样本与无人反例分别读取同源 summary；Phase 1 的业务协议仍是 fixture，真实姿态接线等待后续交接。

旧 4178 视频入口与 `/debug` 保留。此次不修改 Python 代码或冻结 Contract 1.0.0，不启动 Phase 2–5。

---

首页现在为真实视频体验入口：`http://127.0.0.1:4178/`。

1. 选择或拖入自己的 MP4/MOV，填写训练背景，分别确认分析、保存和复测授权。
2. 上传至本机，执行真实媒体检查、180 秒限制、方向校正与可播放副本生成。
3. 本机运行 MediaPipe Pose Landmarker Full，在当前视频上查看人体关键点与可见率。
4. 切换骨架、手腕轨迹、0.5/0.25 倍速和前后帧；从运动峰值定位回放。
5. 生成真实证据 JPG，下载测量 JSON 或内嵌证据图片的离线 HTML 观察记录。
6. 比较已授权且同动作、惯用手、机位的视频；输入文件名后可删除本机视频与派生文件。

真实视频目录为 `tmp/debug-console-data/experience/<video-id>/`。未选择保存的任务在服务正常关闭时清理，保存的任务支持刷新和重启恢复。文件最大 512 MiB、最长 180 秒，最高 4K/240 fps；人体可用性按实际检测质量判断。

模型与依赖已安装：MediaPipe `1.2.2`、OpenCV contrib `5.0.0.93`；模型来源、版本、大小和 SHA-256 见 `models/pose/manifest.json`。模型运行前校验哈希，推理时不联网；小尺寸、躯干不可见和关键点分布不足的结果不能生成个体指标。

动作类型由用户选择。投影角和位移是模型画面代理；球、球拍、精确触球、完整动力链诊断和 WorkBuddy 专家解读尚未接入真实视频页。

启动仍使用下方 `npm run debug-console`。真实链路可用以下脚本验证：

```sh
node scripts/verify-video-experience.mjs /path/to/authorized-person-video.mp4 person
node scripts/verify-video-experience.mjs /path/to/empty-scene.mp4 empty
```

脚本实际检查上传、媒体处理、推理、证据、范围播放、JSON 和离线 HTML。验收与修订记录见 [真实视频验收](../../003%20SDD/debug-console/真实视频体验工作台-验收记录.md)、[修订计划](../../003%20SDD/debug-console/真实视频体验工作台-修订计划.md)。

## 原研发调试台

以下内容仅描述 `/debug`，其固定场景和冻结 Contract v1 入口保留用于研发检查。真实视频通过独立 `/api/experience/videos` 处理。

这是一个仅在本机运行的研发与验收工作台，用于逐步验证
[`网球姿态捕捉与训练建议应用-用户故事-V02.md`](../../002%20PRD/%E7%BD%91%E7%90%83%E5%A7%BF%E6%80%81%E6%8D%95%E6%8D%89%E4%B8%8E%E8%AE%AD%E7%BB%83%E5%BB%BA%E8%AE%AE%E5%BA%94%E7%94%A8-%E7%94%A8%E6%88%B7%E6%95%85%E4%BA%8B-V02.md)
中不依赖 WorkBuddy 对话、研究、报告或专家编排的能力。

工作台不是生产环境，也不证明真实视频模型效果。Fixture 分析始终显示
`SIMULATION`；Python 模式当前用于验证真实本地运行环境和冻结契约往返，创建的分析任务不会被伪装为已完成的模型分析。

## 启动

前置条件：Node.js 20+，项目依赖已经安装；Python 后端检查还需要项目锁定的
`uv`、FFmpeg 和 ffprobe 环境。

```sh
cd "/Users/vigo/Documents/18.网瘾社区/workbuddy-plugin/bridge"
npm run debug-console
```

默认体验入口：`http://127.0.0.1:4178/`；研发调试入口：`http://127.0.0.1:4178/debug`。

默认调试数据目录：`/Users/vigo/Documents/18.网瘾社区/tmp/debug-console-data`

可选环境变量：

| 变量 | 用途 |
|---|---|
| `TENNIS_REPO_ROOT` | 指定项目根目录 |
| `TENNIS_DEBUG_DATA_ROOT` | 指定独立调试数据目录 |
| `TENNIS_DEBUG_PORT` | 修改本地端口 |

服务固定监听 `127.0.0.1`，不接受非回环地址。

## 原调试台使用顺序

1. 打开“用户故事”，按“可执行 / 部分 / WorkBuddy/人工”筛选 V02 的 41 条故事。
2. 选择故事后，在“场景步骤”中逐步运行。每一步保留独立结果和原始 JSON。
3. 标准分析、兼容性、授权、多人对象、安全停止、复测比较等分析场景默认使用 Fixture，并持续显示模拟标识。
4. 本地运行环境场景使用 Python，验证真实进程、版本、Contract 和 13 项环境检查。
5. 在“契约工具”中使用 Contract Registry 动态加载的 17 个工具；结构化表单和原始 JSON 共用同一服务端校验。
6. 在“任务资源”中检查当前会话任务、证据 PNG、慢动作 MP4、目标姿态和诊断 JSON。
7. 在“运行诊断”中查看最近 200 条脱敏调用记录、Fixture 故障模式和危险操作确认门槛。

## 原调试台能力边界

| 范围 | 当前状态 |
|---|---|
| Python 运行环境启动、停止、重启、健康检查 | 真实执行 |
| Contract v1 的 17 个工具与输入输出校验 | 真实 Bridge 往返 |
| 固定样本分析、证据、比较、故障对照 | Fixture 模拟 |
| 真实任意视频姿态、球和球拍识别 | 未实现 |
| WorkBuddy 对话、公开资料研究、报告编排、训练计划、教练共识和邮件发送 | 不在本工作台执行 |
| 目标姿态 | 教学示意，不是当前动作证据或真实改善证明 |

## 检查

```sh
cd "/Users/vigo/Documents/18.网瘾社区/workbuddy-plugin/bridge"
npm test

cd "/Users/vigo/Documents/18.网瘾社区/local-backend"
./scripts/check.sh
```

详细 API、安全边界和验收证据见：

- [`API与安全边界.md`](../../003%20SDD/debug-console/API%E4%B8%8E%E5%AE%89%E5%85%A8%E8%BE%B9%E7%95%8C.md)
- [`Phase-2-6-验收记录.md`](../../003%20SDD/debug-console/Phase-2-6-%E9%AA%8C%E6%94%B6%E8%AE%B0%E5%BD%95.md)

# Phase 2 增量工作台

`npm run phase2-console` 启动同一入口 http://127.0.0.1:4180/ 。选择数据目录并启动后端，进入“找人跟踪”：上传 MP4/MOV 或复制既有获准样本，发送 real_video 请求，再查看本次标注视频、逐帧 PTS/track_id/LOST 和任务说明。检测期间可以独立查询健康与任务状态。

当前接收 ≤180 秒视频、只检测前 30 秒；找人与跟踪是真实测量，骨架与指标待后续阶段。任务停在 analyzing，协议 simulation 暂保留 true。上传暂限 512 MiB，复制文件到当前目录 ingest，不修改用户原件；相同文件名加唯一前缀避免覆盖。边界检查包含损坏文件、路径越界、缺失视频和混入 fixture_id。

后端需要 real-pose 依赖。RF-DETR 日志须走 stderr，notes 字段按 Phase 2 交接纳入 1.1.0 可选字段。验收记录见 `005 后端重构/handoffs/workbench-phase2-acceptance.md`；旧 Phase 1 基座检查与归档验证保留。

# Phase 3 姿态提取工作台

> 以下 Phase 2/3 段落为历史交付记录。最新行为见本文件末尾 Phase 4：检测窗口已扩为≤180秒，最多10个候选动作段，支持逐段状态；指标与渲染已接入真实路径。

在 bridge 目录运行 `npm run phase3-console`，打开 http://127.0.0.1:4180/ 。配置当前数据目录并启动后端，“姿态提取”可发现已有真实任务，读取标准化视频、pose-frames.jsonl 与 skeleton-spec.json；133 关键点和 65 连线由后端工件提供。支持 0.25×/0.5×慢放、逐帧定位、骨架/无效点开关、有效掩码与 JSONL/拓扑下载。

新视频在“上传与跟踪”页提交，单人跟踪稳定后后端自动提取姿态。30 秒视频参考约 7 分钟，工作台允许真实 create 最长等待 30 分钟，控制查询仍为 15 秒。旧 Phase 2 任务缺少姿态工件时不会伪造骨架；需新建任务。失败/主体丢失/不匹配帧不沿用上一帧，也不平滑或插值。

回放以标准化视频 PTS 为准，当前未声称校验原片双向映射；任务仍为 analyzing，指标推导待 Phase 4。用户验收与真实运行证据见 `005 后端重构/handoffs/workbench-phase3-acceptance.md`。原 Phase 1/2 页面保留。

# Phase 4 指标与证据渲染工作台

运行 `npm run phase4-console`，入口仍为 http://127.0.0.1:4180/ 。默认进入“指标与渲染”。选择当前授权目录内的真实任务，查询 quality/strokes/timing/metrics；空值显示证据不足或不可见，真实零值仍显示0。每条指标显示定义、原单位、参考与limitations；strokes限制读该任务analysis-notes.json，缺文件如实提示。

点击骨架叠加、2×慢放、取证帧、左/右手腕轨迹会实际调用 evidence.render。只有该任务本次返回、simulation=false、hash/大小与磁盘一致且位于其evidence目录的资源能获得临时本地预览链接；切目录后旧链接失效。PNG可回看，MP4支持Range播放与下载。2×慢放不插值，手腕轨迹不是球拍轨迹，2D代理不跨机位评级。任务state仍analyzing，不代表专家报告完成。

旧任务没有media-asset.json/analysis-notes.json等新工件时可能无法完成Phase 4查询或渲染，需要使用新版后端新建真实任务；幂等回放不会自动补工件。create与evidence.render允许最长30分钟，普通控制15秒；渲染中可独立查健康。运行证据及限制见 `005 后端重构/handoffs/workbench-phase4-acceptance.md`，用户验收另记。

# Phase 5 全链路验收

运行 `npm run phase5-console`，入口仍为 http://127.0.0.1:4180/ ，默认“全链路验收”。上传本地视频并确认分析授权后创建真实任务，后台执行并发现本次task_id；status/cancel/health独立响应，不以重复create轮询长任务。同一服务会话中刷新页面可恢复最近任务；服务重启后仍可从任务列表查看已有任务，但不承诺恢复中断推理。

“查询指标与渲染”跳到同一任务Phase4页；导出的全链路记录含匹配任务的查询/渲染记录和逐项治理请求响应。diagnostics授权独立，只有勾选的固定诊断JSON可进入本地脱敏包，下载manifest及各项文件；不包含原片，不发送。治理接口原有simulation=true如实保留，真实测量与渲染仍要求simulation=false。真实比较预期COMPARE_REAL_UNSUPPORTED。

删除先生成当前任务版本的文件/哈希清单与ingest原片哈希；完整输入task_id才能执行不可恢复的版本删除。清单10分钟有效、文件变化须重建；删除后显示逐项结果、残留文件与原片哈希核验，不提供all_demo一键清空。自动删除回归只使用独立临时目录，历史任务不用于删除测试。取消保留已有记录和cancellation.json；崩溃恢复目前为删除后新操作重建，不支持断点续跑。

Phase5方案与验收：`005 后端重构/handoffs/workbench-phase5-plan.md`、`workbench-phase5-acceptance.md`。AC-POSE观察项逐条列出，未执行/用户未签不自动通过；目标示意通道本页保持未验。专家团、训练报告、GitHub发布不属于本轮。

## 视频分析首页（最新界面）

首页现已整理为“上传视频 → 等待处理 → 播放核对 → 查看结果 → 导出保存”。选择文件并勾选分析授权，点击一次“开始分析”即上传并创建；“停止本次分析”独立响应。历史记录按文件名和时间显示。选中记录后点击“加载分析视频”，在同页读取真实指标并生成骨架视频，支持慢放、关键画面、左右腕轨迹；中文测量名称保留原值及证据不足状态，不输出教练结论。

“下载当前视频”和“下载分析结果（JSON）”保存当前记录。诊断授权、协议、哈希、删除及比较检查移入默认关闭的“开发者详情”；进程/目录设置在页尾，旧Phase页面在侧栏“开发者工具”。本次界面不改变后端协议或测量算法，验收见`005 后端重构/handoffs/workbench-review-ui-acceptance.md`。

