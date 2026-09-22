# SI-08 用户确认、恢复与 Cron 端到端证据

日期：2026-09-22
分支：`codex/h11-complete-handoff`
节点：三 Profile Skill 受控迭代 SI-08

## 验收范围

本节点把 SI-07 的真实 Replay 结果接入现有候选生命周期：

1. 候选必须处于 `pending_evaluation`（或允许重试的 `evaluation_failed`）才会调用模型；
2. 生产 `/skill-improvement <candidate-id> evaluate` 使用 Case 保存的原始任务输入，绝不把
   `expected_behavior` 这个 grader 断言当成用户问题；旧 Case 没有原始输入时明确拒绝回放；
3. 基线和候选各使用一个独立 Provider 实例，并使用同一份 Git snapshot 建立不同私有 worktree；
4. 真实 A/B 结果只会把候选推进到 `pending_confirmation`，不会自动激活；
5. 用户确认后只影响新任务，旧任务继续使用自己的 Skill snapshot；`restore` 恢复到稳定版本；
6. Cron 正常完成和租约恢复都发送独立系统通知，通知带有 candidate ID 元数据，不把通知混入普通模型回复。
   候选生成后、Replay 前崩溃时，租约恢复会重新接管 `pending_evaluation` 候选并继续 Replay；
   Replay 异常会收敛为批次失败并释放租约，不留下永久 claimed 状态。

## 自有合成 Git fixture 演练

测试文件：`devpilot-agent/tests/test_skill_improvement_profiles.py`。

fixture 在临时目录中创建 Git 仓库和 `tests/test_api.py`，提交后捕获 snapshot。合成 Provider
不再通过一个外部 `candidate=True/False` 开关决定结果，而是读取 Replay 注入的 Skill 内容：
候选 Skill 包含“输出测试入口”时才发起 `devpilot_read_code`，基线不会发起该调用。

实际断言：

- 基线 `0` 个成功，候选 `1` 个成功；
- EvaluationReport 标记为 eligible/pending confirmation；
- 私有 Replay worktree 清理完成；真实 fixture 工作区没有写入；
- 一次 confirmation 只能消费一次；
- 激活后新 snapshot 使用候选内容，旧任务 snapshot 仍是旧内容；
- restore 后稳定版本 hash 恢复。

## 测试证据

在仓库根目录执行：

```powershell
nanobot\.venv\Scripts\python.exe -m pytest -q `
  nanobot/tests/cli/test_commands.py `
  nanobot/tests/command/test_skill_command.py `
  nanobot/tests/agent/test_replay_evaluation.py `
  nanobot/tests/agent/test_skill_replay.py `
  devpilot-agent/tests/test_skill_improvement_profiles.py `
  devpilot-agent/tests/test_p8_02_skill_improvement.py `
  devpilot-agent/tests/test_p8_03_skill_improvement.py
```

结果：`208 passed, 1 skipped`。

变更范围 Ruff 和 `git diff --check` 均通过。仓库全量测试仍包含历史环境相关失败，不能用它们
否定本节点的受影响回归；完整限制记录在交接文档中。

## 明确边界

- 合成 fixture 证明的是生命周期和隔离契约，不等于第三方真实模型质量验收。
- `feature-delivery`、`bug-fix` 的 Case 创建继续只接受服务端受信 Verification；本节点没有把
  模型文本升级成 Verification，也没有扩大工具权限。
- 未确认的候选不会进入活动 Skill；候选文件仍在私有 DevPilot 数据根，不在 SkillsLoader 扫描目录。
- A/B Provider 无法安全复制时会 fail-closed，不会退化成共享有状态 Provider。
