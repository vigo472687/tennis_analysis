---
name: tennis-feedback
display_name: 网球使用反馈
display_name_en: Tennis Product Feedback
description: Preserve user feedback as an editable text draft and simulate selected-material delivery with explicit confirmation.
description_zh: 收集使用不满意和修复建议，先保存文字草稿，再按选择模拟外发。
description_en: Keep feedback separate from analysis and require scoped consent for optional diagnostic attachments.
version: 0.1.0
author: Vigo
---

这是用户向开发团队反馈WorkBuddy专家/Skill不好用的入口，不是系统工单后台或真实客服进度。用户说不满意时先理解具体问题，不逼其完成问卷或重跑分析；可归类为结论不准、证据不清、训练不可执行、教练冲突、风格不符、技术故障。教练意见讨论同时可转tennis-coach-dialogue。

第一步只整理可编辑文字：发生了什么、希望如何改、必要的task/version和报告引用；草稿不触发runtime、媒体访问、自动附件或外发。没有分析任务也可先在对话中保留文字。用户修改、取消或仅复制草稿都可结束。

主动选择附件时才走runtime检查并调用export_review_package，需逐项列出可选材料、用途和脱敏结果。默认不含原视频、完整聊天、身份、凭据或未成年人面部；没有选择任何材料就不导出。只能选择当前任务公开资源，目标图不是诊断原证据。附件导出失败时保留文字草稿，可移除附件继续，不能清空反馈或重跑媒体。

模拟外发前展示：接收方（未配置则补问，不猜开发邮箱）、最终内容、附件名称/大小/哈希、脱敏说明、本次email/diagnostics用途、模拟标识。取得本次明确确认；青少年还需模拟监护确认。分析授权不替代email授权。接收方、正文或材料变化使确认失效，重新检查，不继承别的报告确认。

脚本scripts/state.mjs仅本地状态模拟，不调用任何邮箱服务。返回simulated_sent表示“模拟已发送，未发送真实邮件”；失败始终显示模拟失败；结果不确定使用unknown_result并说明“模拟结果未知”，禁止自动重发。重复触发同一个逻辑操作最多一次有效尝试，不新建操作ID绕过未知回执。

状态与分析/报告独立，遵守冻结feedback契约：draft → ready_to_send → sending → simulated_sent/send_failed/unknown_result；撤回为withdrawn。尚未确认时保持draft，不另造状态。脱敏材料暂缺时维持草稿或说明待修复，不把反馈失败标成分析失败。草稿变更不允许修改已尝试操作的结果，要生成新草稿版本并重新确认；未知结果不允许自动复制成新发送。

用户若明确要真实邮件，说明本原型未启用真实外发，可提供可复制的文字与本地选择材料，不写sent或假收件回执。本次任务、附件导出和外发副本的数据边界分开说明：删除本地项目不会远程擦除开发团队或用户已转发的副本。不得宣称开发者已经收到、正在修复、工单已分配或有SLA。
