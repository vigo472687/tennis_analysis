---
name: tennis-report
display_name: 网球证据报告
display_name_en: Tennis Evidence Report
description: Render a traceable static tennis report from schema-validated structured results and owned local resources.
description_zh: 将结构化结果和可追溯证据生成手机、桌面和离线可读的HTML报告。
description_en: Generate a static local report with explicit status, evidence, sources and practice limits.
version: 0.1.0
author: Vigo
---

先汇总主理人批准的结果，严格按包内contracts/v1/report-input.schema.json的report定义，不新增职业评分、绝对力、球速、真人生成图或未经确认个人信息。字段变更先走共享契约变更，不让模板绕过校验。

状态使用draft、complete、partial、reshoot_required、unsupported、safety_stop、failed、cancelled。只要有请求目标无法可靠完成，就说明partial及可靠项/未知项/补充办法；重拍、不支持、失败和取消不能写完整完成。安全停止不生成常规训练；rehab/uncertain也不生成个体负荷。无痛普通疲劳按安全Skill，不自动安全停止。

每个问题含有效evidence_ids、source_ids、影响、充分程度、限制、替代解释和优先原因，最多三个。来源字段从来源卡转换为accessed_on/supports/limitations等冻结字段，并解释支持内容与范围。无个人证据只输出一般教学与限制；不能用公开球星视频代替用户证据。

先看到状态、最多三问题和本周练习。再展示原证据帧/慢动作/轨迹、相对指标和11阶段步伐同轴、来源支持解释、限制、复测、反馈与本地数据边界。当前证据和通用目标示意用不同标签；P2真人概念图关闭，目标示意异常时不展示并降级文字。ATP/WTA-like仅成年正手辅助文字，青少年不按性别默认。

渲染前验证同task/version的公开资源、哈希、原始时间、问题回链和来源链接。失效资源不能继续支撑问题；返回可恢复渲染失败，修复或删掉受影响问题再生成partial，不能直接显示空图并保留确定结论。渲染失败只修复报告，不重新跑媒体推理。

源代码API是scripts/render.mjs中的renderReport(report,context,{pluginRoot})。context包含dataRoot、task_id、version、workspace_ref、simulation，指向当前task/version公开目录。构建完成后从包根运行：

```sh
node bin/tennis-report.mjs --input '<当前任务公开目录中的report-input.json>' --data-root '<授权项目根>'
```

脚本只读取指定任务中的输入/资源，将HTML与其样式脚本写到同版本公开report目录，不打开后端SQLite、不覆盖原媒体、不发送网络请求。报告输入和产物需随项目删除；导出/用户转发副本不受本地删除控制。

HTML正文不用远程字体、CDN或外部JS，核心信息离线可读。证据资源引用使用本地相对路径，外部来源只在用户点击链接后打开。手机和桌面、键盘、打印均检查；控件不可用时仍保留证据时间和来源文字。所有用户/来源文本必须转义，链接仅HTTPS，禁止把材料中的HTML/脚本作为可执行内容。

在报告中持续标注固定样本“模拟，非真实动作识别”；给出AI辅助、未有合作教练专业签署及可讨论限制。反馈可先复制文字，不自动打包或发邮件。完成意味着生成并检查了报告文件，不等于模型准确、训练有效或WorkBuddy客户端已验证。
