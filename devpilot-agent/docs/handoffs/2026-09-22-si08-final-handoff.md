# 2026-09-22 SI-08 完成交接

## 当前状态

- GitHub 仓库：`https://github.com/g3182479125-hub/devflow-agent`
- 分支：`codex/h11-complete-handoff`
- 最后代码提交（交接文档提交不改代码）：`a280835241d56e2cbd273a3fcf4f496075d86ab2`
- 计划：P0–P8、H1–H11 历史完成；三 Profile 扩展 SI-01–SI-08 已实施。
- SI-08 证据：`devpilot-agent/docs/evidence/phase-8/si-08-user-confirmation-cron-2026-09-22.md`

## 这次完成了什么

1. 把生产 `/skill-improvement <id> evaluate` 接到真实 AgentRunner Replay；基线和候选使用同一
   Git snapshot 的不同私有 worktree。
2. 回放输入使用 `ImprovementCase.replay_prompt`；grader 的 `expected_behavior` 只用于判定，
   不再泄露给模型作为题目。
3. 候选不在 `pending_evaluation` 或 `evaluation_failed` 时，在模型调用前直接拒绝，避免重复评估
   消耗预算和产生孤儿 Trial。
4. 生产运行时为两条 Replay 提供独立 Provider；无法隔离时 fail-closed。
5. Cron 正常完成与崩溃/租约恢复都发送独立系统通知，并携带 candidate ID。
   未关联到批次的 `pending_evaluation` / `evaluation_failed` 孤儿候选也会按绑定范围恢复，避免重复生成。
6. 兼容旧 Case：旧记录没有 `replay_prompt` 时仍可读取，但生产真实 Replay 会明确拒绝，避免
   用虚构输入产生误导性 A/B 结果；应重新从带原始任务输入的任务创建 Case。
7. 更新 CURRENT_PLAN、三 Profile 计划和 SI-08 证据。

## 另一台电脑继续开发

```powershell
git clone -b codex/h11-complete-handoff https://github.com/g3182479125-hub/devflow-agent.git
Set-Location devflow-agent
git pull --ff-only origin codex/h11-complete-handoff
```

先阅读：

```text
AGENTS.md
devpilot-agent/docs/plans/CURRENT_PLAN.md
devpilot-agent/docs/plans/THREE_PROFILE_SKILL_IMPROVEMENT_EXTENSION.md
devpilot-agent/docs/evidence/phase-8/si-08-user-confirmation-cron-2026-09-22.md
```

## 本地验证

```powershell
nanobot\.venv\Scripts\python.exe -m pytest -q `
  nanobot/tests/cli/test_commands.py `
  nanobot/tests/command/test_skill_command.py `
  nanobot/tests/agent/test_replay_evaluation.py `
  nanobot/tests/agent/test_skill_replay.py `
  devpilot-agent/tests/test_skill_improvement_profiles.py `
  devpilot-agent/tests/test_skill_improvement_scheduling.py `
  devpilot-agent/tests/test_p8_02_skill_improvement.py `
  devpilot-agent/tests/test_p8_03_skill_improvement.py
```

当前结果：`218 passed, 1 skipped`。另行运行的 `nanobot/tests/cli/test_commands.py` 为
`142 passed, 1 skipped`。

## 已知限制与后续建议

- 全仓库测试中仍有既有环境/测试依赖问题：DevPilot 独立 venv 缺 `loguru`，使用 nanobot venv
  跑 DevPilot monitor 异步测试时缺少对应 pytest 异步插件；这不是本次 SI-08 受影响回归失败。
- 合成 Git fixture 覆盖的是 `repo-analysis` 的真实 Runner 生命周期；feature-delivery 与 bug-fix
  继续由受信 Verification 入口和回归测试覆盖，后续可分别补充同样的合成 feature/bug fixture。
- 不要提交本机配置、数据库、session、日志、临时 pytest 目录和密钥。当前工作区中这些用户文件保持
  未跟踪，未上传 GitHub。

## 下一步开发规则

若要继续扩展，先固定提交点，再按 ADR-0004 做 Standards/Spec 双轴审查。新增 Replay 必须保持：
同一 snapshot、不同私有 worktree、独立 Provider、服务端 grader、用户确认后才激活；不要把模型
输出当成受信 Verification，也不要恢复已明确排除的独立状态页、第二数据库或第二 Agent Loop。
