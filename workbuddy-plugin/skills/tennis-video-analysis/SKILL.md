---
name: tennis-video-analysis
display_name: 网球视频分析
display_name_en: Tennis Video Analysis
description: Request structured local analysis and interpret compatibility, quality, task stages and recoverable partial output.
description_zh: 通过本地工具处理视频分析任务、质量限制、取消和部分结果。
description_en: Use local tools for simulated fixture analysis with explicit visibility and recovery boundaries.
version: 0.1.0
author: Vigo
---

先完成intake与runtime Skill。当前fixture模式只允许清单中的固定样本，所有分析simulation=true；真实上传可用于讨论拍摄条件，但不能当作固定样本已完成真实识别。Python模式也只有Fixture Adapter，不宣称真实姿态推理。

输入门槛：视频不超过180秒；容器、编码、时长、旋转与能否解码以内容探测为准，不按扩展名猜。手机MP4/MOV、GoPro/Insta360平面导出均为候选输入，合成设备场景测试不等于真实设备兼容；原生全景/专有格式先建议导出平面标准视频。保留原视频，不覆盖或删除用户原件；慢动作和标准化时间必须能够回到原视频。

创建任务用create_video_analysis，参数严格按包内 `contracts/v1/task.schema.json` 的create_input，不能临时发明字段。需稳定operation_id、fixture_id、授权媒体引用、用户最小信息、分用途consent、主辅路由、主体与镜像确认。request_id由Bridge生成；操作重试保持operation_id，改变输入不能复用同一ID。大媒体只传资源引用，不编码进JSON或LLM上下文。

主体选择/镜像未确认先停在输入检查；可根据工具返回的候选ID补问，用户新确认形成新输入版本或显式新操作，不能偷换原任务身份。未知动作返回unable；不要强制当成正手。

调用顺序：

1. create_video_analysis快速返回task_id/version/workspace_ref，不等待长任务结束；向用户说明当前阶段。
2. get_analysis_status查询阶段和检查点；不要高频忙轮询。非终态允许cancel_analysis，重复取消无新副作用，完成/已取消任务不被改回运行。
3. 质量可用后get_quality_report，交视觉解释员检查容器/时长/可见性/时间映射；严重损坏、超时或原生全景为unsupported，关键对象不足为reshoot_required或partial。
4. 仅在适用阶段读取get_stroke_events、get_groundstroke_timing、get_motion_metrics。结果没就绪先说明等待，不用自己的推测代填。
5. render_evidence_overlay可请求frame/clip/trajectory，使用有效evidence_ids；资源须同task/version、哈希一致、可回到原时间。缺资源标不可用与修复，不以目标图替换。
6. 把可靠观察交tennis-evidence-reasoning，再进入训练/报告。技术问题不超过三项。

脚不见排除步伐/支撑/蹬转，球不见排除落地/来球时机/精确触球，拍不见排除拍头/降拍头。每项质量问题都有代表证据、影响和针对性重拍机位，不能只给“画面差”。遮挡/串线/跳变区间排除；时间映射失效不发布对应问题。

取消保留检查点，不删除项目。中断先查状态和已完成阶段；相同输入恢复已完成阶段，报告失败不重跑媒体，工具失败不表述完整完成。partial报告区分可靠项、无法判断项与补拍方法。资源保存限public workspace_ref，不能读SQLite或后端私有目录；生成报告/记录须留在该task/version公开输出范围便于删除。
