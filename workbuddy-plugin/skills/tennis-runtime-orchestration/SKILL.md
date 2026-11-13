---
name: tennis-runtime-orchestration
display_name: 网球本地能力编排
display_name_en: Tennis Local Runtime
description: Orchestrate status, confirmed preparation, startup and bounded recovery of the local tennis runtime.
description_zh: 按本轮任务检查、准备、启动和修复本地能力，保留原请求。
description_en: Check local capability only when needed and preserve the original operation across recovery.
version: 0.2.0
author: Vigo
---

## 入口与职责

只有业务确需本地媒体、证据、导出或治理时才到这里。not_required 直接返回业务，uncertain 只补阻塞项，不准备或启动 Python。主理人把运行检查交给 tennis-runtime-support；其他业务专家只解释资料，不直接修环境、执行命令或管理进程。

技术服务先通过受控工具查权威状态，不能用“已加载专家”或页面打开判断后端就绪。区分组件存在、进程、协议通信、能力健康及具体任务完成。当前后端可处理真实视频，按每项协议实际 simulation 标识解释，不能全局改成 true 或 false。固定样本仅用于明确标注的隔离测试。

## 状态与动作

| 状态 | 技术服务动作 |
|---|---|
| 未准备 | 展示来源、固定版本、磁盘/数据目录及联网范围；按已确认范围准备，不临时下载未经校验的代码 |
| 已安装未运行/项目进程已退出 | 检查已授权配置，通过受控启动工具拉起同一项目实例并验证 |
| ready | 返回业务，具体请求结果仍需单独核对 |
| busy/存在活动请求 | 展示实际阶段和耗时，保持处理，不因等待较久重启 |
| 状态不明 | 核对控制请求、任务进展与进程，单次超时不等于卡死 |
| 可识别的环境故障 | 仅使用登记配置/恢复模板，记录差异、执行、验证、失败回退；同故障最多两轮 |
| 超出运行权限 | 保留现场并形成 needs_developer 问题单，不改功能、算法、接口含义或专家职责 |

当前工作台技术服务只接入健康检查、已退出项目实例恢复和有界健康探测超时配置。任意启动脚本修补、自动安装或跨平台修复未实现时明确不支持；本 Skill 不是工具授权凭据。发布与真实 WorkBuddy 首次获取按后续阶段单独验证。

## 保留与恢复

保留原输入引用、主辅目标、task/version、operation_id 与已完成步骤。先查已有结果，再至多继续一次未执行操作；结果未知的收费请求不自动重发。报告失败只恢复报告，模型限流不重启 Python，取消任务不恢复。进程崩溃后不假称支持视频断点续跑。

控制范围、版本或联网用途改变时先说明新范围；已有授权且范围相同的启动/恢复无需重复询问。诊断不含密钥、原视频和完整聊天。数据删除、卸载、取消是不同动作，沿用各自用途授权和精确资源范围，不删除业务数据补救运行故障。

## 完成条件

真实健康响应与必需检查通过才说明运行环境恢复；任务完成另查任务及工件。返回检查项、实际动作、验证/回退、保留内容、限制和下一步。工作台验证不代表真实 WorkBuddy 的工具权限隔离或附件交接已验收。
