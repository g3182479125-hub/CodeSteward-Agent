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
- 普通工具、DevPilot/任务配置、子代理、模型和 provider 失败按来源映射到不同的
  中文 catalog；异常类型和内部文本只参与服务端分类，不进入用户可见 payload。
- 用户看见的是当前步骤的结果、可重试/换问法/查状态提示和“对话仍可继续”，不看见
  Python 异常、诊断预算术语或旧英文兜底。

## 自动验证

以下命令均在仓库根目录
`D:\Users\tianmin.li\.codex\worktrees\6401\remote-integrate` 执行；每条命令的退出码
均记录在结果中。

```powershell
& .\nanobot\.venv\Scripts\python.exe -m pytest -q `
  nanobot/tests/agent/test_turn_outcomes.py `
  nanobot/tests/utils/test_progress_events.py `
  nanobot/tests/utils/test_webui_transcript.py `
  nanobot/tests/bus/test_outbound_events.py `
  nanobot/tests/agent/test_loop_progress.py `
  nanobot/tests/agent/test_loop_save_turn.py `
  nanobot/nanobot/channels/websocket/tests/test_websocket_channel.py `
  -k 'not test_h7_default_driver_runs_each_task_profile_main_chain' --tb=short
```

结果：`355 passed, 3 deselected`，退出码 `0`。

```powershell
& .\nanobot\.venv\Scripts\python.exe -m ruff check `
  nanobot/nanobot/agent/loop.py `
  nanobot/nanobot/agent/turn_outcomes.py `
  nanobot/tests/agent/test_loop_save_turn.py `
  nanobot/tests/agent/test_turn_outcomes.py
```

结果：`All checks passed`，退出码 `0`；`git diff --check` 退出码 `0`。

```powershell
Set-Location .\nanobot\webui
npm test -- --run src/tests/useNanobotStream.test.tsx src/tests/tool-traces.test.ts
npm run lint
npm run build
```

结果：目标 Vitest `75 passed`，lint 通过，production build 通过，整段命令退出码
`0`。

```powershell
Set-Location D:\Users\tianmin.li\.codex\worktrees\6401\remote-integrate
$env:PYTHONPATH = (Join-Path (Get-Location) 'nanobot')
Set-Location .\devpilot-agent
& ..\nanobot\.venv\Scripts\python.exe -m pytest -q tests/test_temporary_tools.py --tb=short
```

结果：`28 passed, 1 failed, 1 skipped`，退出码 `1`。同一测试在实施基线中得到相同
失败，失败断言是持久化加载结果 `persisted_payload["ok"] is True`；本轮没有修改该
路径，因此将其保留为已知环境/历史失败，不能把 DevPilot 全部测试写成通过。
```

失败来源分类回归：`8 passed`。候选代码在修改前红灯的同一工具失败恢复用例上，
已不再返回 `Error: RuntimeError: tool exploded`，并且同一 session 的下一条普通消息
仍能得到正常回答。

DevPilot 临时工具测试从仓库集成环境启动后为 `28 passed, 1 failed, 1 skipped`；
唯一失败是基线同样存在的持久化加载断言，不属于本轮改动。直接使用 DevPilot 自己
的虚拟环境还会因为缺少 nanobot 依赖而无法收集，这属于启动方式问题，证据中没有
把它伪装成通过。

本轮定向 pytest 排除了一个已有环境前置失败：
`test_h7_default_driver_runs_each_task_profile_main_chain[bug-fix]` 当前机器的
默认 Skill Improvement store 没有有效的 `skills/bug-fix/SKILL.md` 快照，因此在
进入本轮 outcome 逻辑前就抛出 `SkillImprovementError`。没有写入用户目录或伪造该
快照；这不是本轮代码回归，R5 会把它作为环境前置单独记录。

此前完整 WebUI 测试还存在 17 个已有 locale 基线断言失败（实际中文标签与英文期望
不一致），本轮没有为了让数字变绿而改动无关的 locale 行为。

## 尚未宣称完成的部分

R5 的 Standards/Spec 双轴审查和真实 WebUI 录制场景仍需完成；因此本文件不是“全部
完成”声明。自动回归和修改前红灯证据已经补齐，最终是否标记计划 complete 取决于
R5 的增量审查和实际 WebUI 验收，不会用单元测试替代录制前验证。
