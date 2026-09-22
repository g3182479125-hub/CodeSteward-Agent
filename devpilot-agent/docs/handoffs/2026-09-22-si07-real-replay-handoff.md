# SI-07 真实 Replay 换机交接

日期：2026-09-22（Asia/Shanghai）  
仓库：`https://github.com/g3182479125-hub/devflow-agent.git`  
分支：`codex/h11-complete-handoff`  
最新提交：`4bb8a63`（SI-07 代码候选为 `dd171a6`）

## 当前进度

三 Profile Skill 受控迭代已经完成 SI-01–SI-07。当前已具备：

1. 三个 Profile 的受信目标注册和 Case/Skill/hash 绑定；
2. Git snapshot 与独立 worktree Replay；
3. baseline/candidate 对照与三个 deterministic grader；
4. ReplayTrial/EvaluationReport 持久化、预算门禁和失败闭合；
5. repo-analysis 反馈与真实 `read_code` 路径+SHA-256 证据绑定；
6. 新 Run 自动清理旧读取证据；
7. Replay 通过后仍停在 `pending_confirmation`，没有自动启用。

证据和审查报告：

- `devpilot-agent/docs/evidence/phase-8/si-07-real-replay-2026-09-22.md`
- `devpilot-agent/docs/reviews/2026-09-22-si07-standards-review.md`
- `devpilot-agent/docs/reviews/2026-09-22-si07-spec-review.md`
- `devpilot-agent/docs/plans/THREE_PROFILE_SKILL_IMPROVEMENT_EXTENSION.md`

## 换机拉取

```powershell
git clone -b codex/h11-complete-handoff https://github.com/g3182479125-hub/devflow-agent.git
Set-Location .\devflow-agent
git status --short
```

已有仓库则执行：

```powershell
git fetch origin
git switch codex/h11-complete-handoff
git pull --ff-only origin codex/h11-complete-handoff
```

使用新电脑自己的模型配置、FC 配置和本地数据；不要复制另一台电脑的密钥、数据库、session、临时目录或未跟踪用户文件。个人临时训练项目也不在本次提交范围内。

## 下一步：SI-08

下一台电脑继续实现：

1. 用真实 A/B Replay 结果驱动 `pending_confirmation` 的用户确认；
2. 复用现有 enable/ignore/restore 保护，确认后只影响新任务，活动任务继续使用旧快照；
3. 增加 Cron 独立通知、取消、失败恢复和无确认零激活验证；
4. 用自有合成 Git fixture 完成一次端到端演练，并明确标注为合成证据；
5. 按 SI-08 完成测试、证据、Standards/Spec 双审后再更新交接状态。

不要在 SI-08 之外重做 P0–P8、H1–H11，也不要把 Cron 自动串联当作 SI-07 的遗漏修补。

## 本地验证

仓库根目录可复现：

```powershell
nanobot\.venv\Scripts\python.exe -m pytest -q `
  nanobot/tests/agent/test_skill_replay.py `
  nanobot/tests/agent/test_replay_evaluation.py `
  nanobot/tests/agent/test_task_profile_session.py `
  nanobot/tests/command/test_skill_command.py `
  devpilot-agent/tests/test_skill_improvement_profiles.py `
  devpilot-agent/tests/test_p2b_control_plane.py
```

预期：`83 passed`。完整类型检查仍有历史基线错误；以节点证据中的 focused 检查和 Ruff 结果为准，不要误判为全仓类型清零。
