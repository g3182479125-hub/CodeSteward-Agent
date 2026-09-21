# R1–R4 implementation evidence: resilient runtime

日期：2026-09-21
分支：`codex/h11-complete-handoff`
实施基线：`d2950a56a8ba63afc90dcdb110fcdb4c45fe0bc1`

## 本轮交付

本轮把 R1–R4 接到 nanobot 已有的 turn、delivery、WebSocket transcript 和 WebUI
活动轨迹中，没有增加第二个 Runtime、数据库、状态页或完整工具输出入口。

### R1：当前 Turn 可恢复

- 新增 `nanobot/nanobot/agent/turn_outcomes.py`，把内部 stop reason、工具/模型
  失败、路径拒绝、未知副作用和取消统一为安全的 `TurnOutcome`。
- guard、预算和工具失败只生成当前 Turn 的 outcome，不把普通聊天永久变成
  session 级暂停。
- 未知副作用仍然是 operation 级 `query_only`；不会因为“允许继续聊天”而重放
  原来的副作用调用。
- 可用的部分回答会保留；空回答和内部暂停/异常文本会替换为简短中文恢复提示。

### R2：安全 outcome 投影

- 当前 Turn 的 redacted outcome 写入 session metadata，刷新或重新加载时仍有同一
  个最新事实可解释。
- outbound bus 增加版本化 `TurnOutcomeEvent`；WebSocket 先 append transcript activity
  再发送 `turn_outcome`，随后仍可发送普通回答或结束帧。
- operation reference 只允许受限字符和长度，内部异常、路径和配置内容不会进入
  用户可见事件。

### R3：复用现有活动轨迹

- 临时工具的校验、启用、失败和清理状态映射到已有 tool activity。
- WebUI 将 `turn_outcome` 显示为 activity trace；它不会把失败事件当作流锁或要求
  用户新开会话。
- 清理未完成时显示“清理待重试，聊天仍可继续”，不暴露 artifact/source 细节。

### R4：统一用户提示

- 普通工具、任务 profile、模型和 provider 失败使用统一中文 catalog。
- 用户看见的是当前步骤的结果、可重试/换问法/查状态提示和“对话仍可继续”，不看见
  Python 异常、诊断预算术语或旧英文兜底。

## 自动验证

在 `nanobot` 目录使用项目虚拟环境：

```text
Ruff affected files: All checks passed
pytest targeted: 114 passed, 3 deselected
WebUI targeted Vitest: 146 passed
WebUI lint: passed
WebUI production build: passed
```

本轮定向 pytest 排除了一个已有环境前置失败：
`test_h7_default_driver_runs_each_task_profile_main_chain[bug-fix]` 当前机器的
默认 Skill Improvement store 没有有效的 `skills/bug-fix/SKILL.md` 快照，因此在
进入本轮 outcome 逻辑前就抛出 `SkillImprovementError`。没有写入用户目录或伪造该
快照；这不是本轮代码回归，R5 会把它作为环境前置单独记录。

此前完整 WebUI 测试还存在 17 个已有 locale 基线断言失败（实际中文标签与英文期望
不一致），本轮没有为了让数字变绿而改动无关的 locale 行为。

## 尚未宣称完成的部分

R5 的 Standards/Spec 双轴审查、候选固定点记录和最终验收尚未完成；因此本文件不是
“全部完成”声明。审查通过后再决定是否把计划状态改为 complete。
