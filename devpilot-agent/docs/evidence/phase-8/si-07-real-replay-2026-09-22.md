# SI-07 真实 Replay 与受信 grader 节点证据

日期：2026-09-22（Asia/Shanghai）  
分支：`codex/h11-complete-handoff`  
候选提交：`dd171a6`  
固定审查点：`41df363`

## 结论

SI-07 已完成。实现了隔离的 AgentRunner Replay、基线/候选独立 worktree、三个 deterministic grader、ReplayTrial/EvaluationReport 持久化，以及预算、证据和失败闭合门禁。候选仍只能进入 `pending_confirmation`，不会因 Replay 通过自动激活。

SI-08 尚未完成：用户确认/恢复、Cron 独立通知和端到端合成 fixture 演练不属于本节点。

## 本节点实际改动

- `nanobot/nanobot/agent/skill_replay.py`：AgentRunner Replay、隔离 worktree、调用前 token 预留、unknown usage/timeout/infrastructure fail-closed、三个 deterministic grader。
- `devpilot-agent/src/devpilot/replay_evaluation.py`：绑定 Case、snapshot、Skill hash、trial 参数和结果引用。
- `devpilot-agent/src/devpilot/skill_improvement.py`：Trial/Evaluation 持久化边界的单次和聚合硬预算门禁。
- `nanobot/nanobot/command/builtin.py`、`nanobot/nanobot/agent/loop.py`：repo-analysis 反馈只使用真实 `read_code` 路径+SHA-256；`--expected` 必须指向本次 Run 读取的文件；新 Run 清理旧证据。

## 可复现验证

仓库根目录执行：

```powershell
nanobot\.venv\Scripts\python.exe -m pytest -q `
  nanobot/tests/agent/test_skill_replay.py `
  nanobot/tests/agent/test_replay_evaluation.py `
  nanobot/tests/agent/test_task_profile_session.py `
  nanobot/tests/command/test_skill_command.py `
  devpilot-agent/tests/test_skill_improvement_profiles.py `
  devpilot-agent/tests/test_p2b_control_plane.py
```

结果：`83 passed in 10.38s`。两组 Ruff 和 `git diff --check` 通过；本次新增代码的 focused Pyright/BasedPyright 检查通过。完整类型检查仍有历史基线错误，不把它们冒充为本节点已清零。

## 审查

- Standards：PASS，无 P1/P2；见 `docs/reviews/2026-09-22-si07-standards-review.md`。
- Spec：PASS，无 P1/P2；见 `docs/reviews/2026-09-22-si07-spec-review.md`。

