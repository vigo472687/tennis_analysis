---
name: tennis-evidence-reasoning
display_name: 网球证据推理
display_name_en: Tennis Evidence Reasoning
description: Connect personal observations, applicable rules and verified teaching sources before publishing at most three issues.
description_zh: 检查个人证据、教学规则和来源的关联，阻止无依据的确定结论。
description_en: Check claim-evidence-source traceability, limits and alternatives before publishing.
version: 0.1.0
author: Vigo
---

先读 @references/sources.json 与 @references/source-map.json。它们是通用知识目录，不能代替个人视频证据。技术问题和普通问答均应给可打开的来源引用，并解释支持范围；来源未核验就标记，不编造教练原话或时间码。

对每个候选问题执行：

1. **证据身份**：task_id/version/主体一致，原时间非负且有序、在视频长度内，时间映射有效；资源存在且哈希一致。过期、跨任务或串线证据剔除。
2. **可见性**：脚不见不评步伐/支撑/蹬转，球不见不评落地/时机/精确触球，拍不见不评拍头/降拍头。不可见不当作零或错误。只保留可靠区间。
3. **来源适用**：rule_id在当前动作库中，source_id真实支持其原则；区分来源教学、产品观察设计和候选剂量。文章说某动作常用，不等于用户必须如此。
4. **指标解释**：写定义、参照、相对单位、证据、缺失状态和限制。仅相对时机/位置/轨迹/稳定性；不输出未标定绝对速度、真实力、力矩、专业等级或职业评分。
5. **因果克制**：描述看到什么，再写可能影响；加入会改变建议的替代解释，例如站位、来球、镜头透视、任务目标。证据不足或来源冲突时补问、补拍或降级，不给唯一确定判断。
6. **排序与上限**：按安全、影响、可改性和依赖排序，最多三项。多于三项合并/延期并保留原因，不为凑三项制造问题。

每个正式问题至少有：简明结论、有效evidence_ids、原时间、rule_id、source_ids、观察/影响、充分程度、限制、必要替代解释、行动与复测。渲染前保持包内report-input契约，禁止给MCP临时增加自创字段。

问题示例的表达方式：“模拟片段中，准备与前挥之间出现停顿（证据ID、原时间）。它可能打断连续性；也可能与喂球等待有关。来源X支持连续随挥的一般原则，不能证明你真实发力不足。可先用低速固定喂球复看这一段。”不要照抄示例当作用户事实。

当前证据和目标示意严格分开：target_pose不在Evidence集合，不能用于证明改善或技术错误。若唯一“证据”是AI目标图则阻止发布。无个人证据的问答仍可引用通用来源，但要说是一般教学解释。

若报告含安全停止、康复或状态不确定，不通过个性化负荷计划；若任何问题失去有效回链，删掉对应问题或将报告降级partial/重拍，不能保留完整完成文案。给主理人返回通过/剔除项、原因和下一步，不能自称专业教练终审。
