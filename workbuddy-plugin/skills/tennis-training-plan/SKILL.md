---
name: tennis-training-plan
display_name: 网球训练与复测
display_name_en: Tennis Practice and Retest
description: Build one-to-two-week practice plans with explicit safety gates, dependencies, dose and comparable retest conditions.
description_zh: 依据可训练问题生成一至两周计划，覆盖步骤、剂量、停止和复测。
description_en: Plan bounded practice and comparable retests without promising improvement or prescribing rehabilitation.
version: 0.1.0
author: Vigo
---

读取 @references/groundstrokes.json、@references/footwork.json、@references/serve-volley.json 与 @references/drills.json。候选问题须已通过tennis-evidence-reasoning；没有个人证据时只给一般练习解释，不能冒充针对性诊断计划。当前无合作教练专业签署。

先处理安全：疼痛、受伤、眩晕、症状恶化进入safety_stop，不继续个性化技术负荷；康复诉求给一般信息与可供线下沟通的观察清单，不给个性化康复计划；安全状态不确定先补问并采用保守路径。仅普通无痛疲劳且无其他风险时不自动safety_stop，可在休息与用户意愿确认后提供低负担基础计划，每项写停止条件；若无法区分疲劳与疼痛先不安排负荷。新不适在每轮训练记录后重新检查，旧的clear不能覆盖新症状。

每份计划聚焦最多三个当前优先问题，通常只激活1至3项练习。先处理安全与稳定、移动空间和准备时机，再追求连续挥拍、外观。若练习冲突或缺前置能力，延期并说明；不同时改握拍、站位、引拍与节奏来证明单个原因。

每个练习必须包含：

- 对应问题与目标、rule_id、来源解释和适用限制。
- 可操作步骤，器材/场地/喂球要求及需要同伴或线下指导之处。
- 频率、次数或时长、组间休息；明确是产品候选剂量，不是来源文章处方。
- 进入条件、进阶条件、减量与停止条件；用户可不做或减少，不自动加量。
- 首次复测时间、需要的机位/来球条件、观察定义、样本和保留失败次的方法。

计划按1至2周安排：第一次先低速熟悉并记录困难；后续重复相同变量；周期结束按可比条件复测，必要时继续观察。不得承诺若干天改好动作，也不把完成次数、单次进球、目标示意当作改善证据。

解释正反手要让用户能实践合力协同、蹬转、降拍头与连续前送。解释步伐要包括读球、第一步、距离覆盖、移动拉拍、小步调整、动态支撑、降拍头、蹬转、前挥和恢复。球不见不形成个人来球时机结论；脚不见不评支撑，拍不见不评拍头；可以给一般拍摄与练法说明。

首次复测前补齐动作/持拍手、机位/取景、喂球条件、身体反馈与目标。调用compare_sessions前核对两次任务retest用途与青少年模拟监护确认。条件不可比就并列说明差异，不给相对改善结论；可比时也只解释后端提供的相对变化和证据，不从合成固定样本推导真实训练效果。

训练记录通过本技能scripts/records.mjs保存到task/version公开records目录；记录完成、实际剂量、困难、感觉、身体反馈和是否停止，必须关联具体drill_id。记录新增不适立即切换安全分支。公开输出随项目删除，外部导出副本无法远程擦除。
