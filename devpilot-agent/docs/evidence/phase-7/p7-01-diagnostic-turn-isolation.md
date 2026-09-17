# P7-01 Diagnostic Guard Turn Isolation

日期：2026-09-17
范围：P7-01 任务级无效循环检测的回合边界修正

## 问题

`TaskLoopGuard` 原本会把某一轮工具失败后的连续错误、已见错误和诊断分支数写入会话。下一条普通用户消息会恢复这份状态，因此一次路径/授权等工具错误可能把整个聊天会话停在：

> Task paused: the bounded diagnostic-branch budget was exhausted.

这不符合 P7-01 的边界：护栏应限制当前诊断尝试，而不是阻止用户提供新证据、修正路径、查询状态或开始另一种处理方式。

## 修正

- 普通入站用户回合创建新的 `TaskLoopGuard`，不恢复上一回合的诊断计数。
- 只有同一任务的系统内部自动续跑回合恢复计数，继续保留相同错误/分支的有限预算保护。
- `/new`、任务 profile 切换和清空 profile 的既有清理逻辑保持不变。
- 这次修正没有放开执行授权、确认、项目路径边界或 H7/H8 未知副作用保护。

## 验收证据

| 检查 | 结果 |
|---|---|
| `tests/agent/test_task_profiles.py`（当前仓库源码路径） | 10 passed |
| `tests/agent/test_loop_runner_integration.py` | 17 passed |
| `tests/agent/test_loop_handoff.py` | 36 passed |
| `tests/agent/test_loop_save_turn.py` | 77 passed |
| Ruff（变更文件） | passed |
| `git diff --check` | passed |

测试命令需要把当前 checkout 的 `devpilot-agent/src` 和 `nanobot` 放在 `PYTHONPATH` 前面，并在本次回归中设置 `DEVPILOT_ENABLE_SKILL_IMPROVEMENT=0`；否则本机遗留环境可能导入另一份旧的 `devpilot` 源码，或让临时测试项目进入未准备 P8 Skill 快照的试验路径。

示例：

```powershell
$env:PYTHONPATH = "<checkout>\devpilot-agent\src;<checkout>\nanobot"
$env:DEVPILOT_ENABLE_SKILL_IMPROVEMENT = "0"
python -m pytest -q tests/agent/test_task_profiles.py
python -m pytest -q tests/agent/test_loop_runner_integration.py tests/agent/test_loop_handoff.py tests/agent/test_loop_save_turn.py
```

## 预期用户行为

一轮工具调用失败后，当前回合仍可被护栏暂停并给出事实和下一步；用户随后发送“继续”“我改好路径了”“先看看状态”或其他普通问题时，不会再被上一轮的诊断预算错误拦截。若模型在新的回合中再次重复失败，新的回合仍会独立受到有限循环保护。
