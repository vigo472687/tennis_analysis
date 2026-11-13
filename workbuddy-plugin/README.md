# 网球成长专家团 · 原型0.1.0

> 2026-11-06：当前r2报告视觉交付已被用户退回，撤回动作分析验收提交。方块人物、固定轨迹和预设时序仅为测试资源，不具备真实动态骨架能力。旧包仅作历史与回归测试材料；修正与下一次提交门槛见[CR-001](<../003 SDD/reviews/CR-001-真实视频动态骨架修正.md>)。下列测试通过不表示真实动作识别通过。

当前是可检查的固定样本原型：6个角色、8个Skill、Node MCP Bridge、17个工具、静态HTML报告与本地记录。不依赖项目自建远程服务。**固定样本分析不是对真实上传视频做姿态识别；青少年监护、邮件发送均为模拟。**

## 本次可检查的内容

| 内容 | 实现与验证边界 |
|---|---|
| 基础技术 | 正手、单反、双反、发球、正反手截击、基础步伐；用户范围含4.0，库深度限基础3.0及以下，不要求NTRP自评 |
| 合力与时机 | 解释相对顺序、连续性、蹬转、降拍头和移动中准备；不测真实力、速度或职业等级 |
| 画面 | 合成当前帧、模拟慢动作/轨迹、独立通用目标示意；不复制球员身份或证明真实改善 |
| 训练与教练 | 候选剂量、停止/复测、训练记录、两轮以上教练讨论与少变量实验；无合作教练专业签署 |
| 反馈 | 先文字草稿，主动选脱敏附件；模拟成功/失败/未知，未知不自动重发，无真实邮件或工单系统 |
| 平台 | 官方SDK客户端对真实stdio子进程已验证；真正WorkBuddy安装/附件交接/角色协作/相对cwd解析仍待实机验证 |
| Python | Claude单独实现；完整后端未验收前不会打包半成品为就绪artifact。缺失时明确PYTHON_ARTIFACT_MISSING |

## 从源码构建与检查

在项目根运行。开发需要Node20+、本地FFmpeg/ffprobe；首次npm安装需网络，生成的HTML与fixture运行不需要网络。

```sh
npm --prefix workbuddy-plugin/bridge ci
npm --prefix workbuddy-plugin/bridge test
npm --prefix workbuddy-plugin/bridge run build
node --test workbuddy-plugin/tests/training-records.test.mjs workbuddy-plugin/tests/coach-records.test.mjs workbuddy-plugin/tests/feedback-state.test.mjs workbuddy-plugin/tests/report-render.test.mjs workbuddy-plugin/tests/package.test.mjs
node --test integration/contract-tests/schema.test.mjs integration/contract-tests/fixture.test.mjs
```

浏览器QA单独运行，需要已安装Chrome与Node22+原生WebSocket，不改变交付Bridge的Node20基线。可用TENNIS_TEST_CHROME指定浏览器绝对位置。

```sh
TENNIS_UI_EVIDENCE_DIR=integration/reports/ui node --test workbuddy-plugin/tests/report-ui.test.mjs
```

证据目录包含手机/桌面截图、打印PDF和检查JSON。静态提示词场景走读不等同于真实LLM/WorkBuddy执行；实际训练效果和多设备兼容也未验证。

## 连接本地MCP

`.mcp.json`只声明一个stdio服务，cwd必须为解压后的包根。WorkBuddy中若相对cwd没有按包根解析，将cwd和args分别改为该用户自己的解压目录与bin/tennis-bridge.mjs绝对位置。这里不使用未经证实的插件根变量，也不携带开发者机器路径。

```json
{"mcpServers":{"tennis-local":{"type":"stdio","command":"node","args":["<用户包目录>/bin/tennis-bridge.mjs"],"cwd":"<用户包目录>","runtime":{"type":"node","version":">=20"},"env":{"TENNIS_BACKEND":"fixture","TENNIS_DATA_ROOT":"<用户选择的数据根目录>"}}}}
```

连接后仅启动Node协议入口，不拉起Python、不准备组件。纯文字问答无需本地分析。需要媒体能力时先查runtime_status，再由Skill解释范围、取得确认并prepare/start。fixture的scope_digest为fixture-local-v1，版本0.1.0；拒绝可继续通用问答。数据根默认用户Library/Application Support/tennis-workbuddy，也可显式配置。

生产安装、系统组件卸载和冷启动下载不在当前原型验证内；fixture“卸载”只关闭/重置模拟运行状态，不删除系统Python/uv/FFmpeg或包本身。项目删除与组件卸载分开；外部导出副本不受控。

## 生成报告

Skill从结构化工具输出整理report-input契约；正式技术问题必须额外有经证据推理确认的problem_rules映射。报告上下文只描述本次媒体和公开工作区，不是数据库结构。

```json
{"task_id":"demo","version":1,"workspace_ref":"projects/demo/v1","simulation":true,"media_duration_s":3,"subject_id":"person-1","problem_rules":{"p1":["GS-TIMING"]}}
```

将上下文保存到当前项目的public/report-context.json，report输入保存到同public目录（媒体必须由用户明确选择，context需与工具结果一致）。随后执行：

```sh
node workbuddy-plugin/bin/tennis-report.mjs --input '<数据根>/projects/demo/v1/public/report-input.json' --data-root '<数据根>'
```

产物为projects/demo/v1/public/report/index.html及本地样式/脚本，直接浏览器打开。校验失败只恢复渲染，不重跑分析。目录已删除或资源越界/失效/哈希改变时不生成正式报告。当前证据、目标示意分开标识；外部来源需用户点击才联网打开。

训练/教练/反馈脚本只写当前任务的公开records目录，不访问SQLite；项目删除覆盖这些记录。脚本为ESM API，具体函数见Skill。无关联任务的纯文字草稿先保留在对话，不强迫创建分析任务。

模拟发送必须先有持久化回执工作区，不能仅在内存草稿上尝试发送。已有任务使用FeedbackController(context)；独立文字反馈可显式使用FeedbackController({feedbackRoot:用户选择的已有工作区})，不启动Python或创建视频任务。该独立目录是用户选择的外部记录范围，删除分析项目不清除它；需要删除时单独选择该记录目录。发送中断恢复为unknown_result，不能自动重试。材料仍由用户逐项选择，脚本从不真实发送邮件。

## 提交包与已知限制

仅打包`.codebuddy-plugin`、`.mcp.json`、settings.json、agents、skills、avatars、bin、contracts、fixtures与本README；不包含bridge/node_modules、开发缓存、.venv、密钥或真实用户材料。bin文件已经bundle，无需将开发依赖分发给用户。头像是原创通用几何角色标记，不代表真实教练身份。

市场分类Sports、作者邮箱prototype@example.invalid为本地原型占位；发布前需替换为平台接受的分类和真实作者联系方式。当前不宣称通过市场审核或真实客户端支持矩阵。

fixture模拟任务元数据目前仅进程内保存，进程关闭后不能依赖fixture恢复分析；持久化检查点属于Claude后端及后续联调。公开资源与记录有文件持久化，但不能将其误述为完整后端恢复通过。Node20是类型/构建目标，当前实际执行环境为Node24，Node20需另验。

最终验收以003 SDD/progress.md及各task/checkpoint的证据为准。HG-EXPERTS、HG-BACKEND等人工门槛只能由用户明确确认，文件存在不代表已验收。
