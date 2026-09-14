# 2026-09-14 H8 所有权与恢复交接

## 远端检查点

- 仓库：`https://github.com/g3182479125-hub/devflow-agent.git`
- 分支：`main`
- 本交接提交将包含：H8 的远端策略执行、持久化接管、FC 取消、CAS/恢复修复及迁移 `0015`。
- 本地未提交且**不属于本轮**：`nanobot/webui/package-lock.json`，以及仓库根目录、`nanobot/`、`devpilot-agent/` 下的 `.pytest-*` 临时目录；不要提交、不要删除或覆盖。

## 当前真实进度

- H1–H7：保留既有完成状态，勿重做。
- H8：已实现远端 Loop 策略的 FC-only 执行、不可变 artifact + hash + trial receipt、红化投影、运行中 Driver 切换、JSONL/SQLite 检查点对账、工具派发前所有权复核、FC 取消转发，以及 `switch_generation` CAS 围栏。
- H8 尚**不能标记完成**：尚缺 H8-A–I 的真实同任务/WebUI/FC 全场景验收、最终批量双轴审查证据与正式 evidence 文档。下一位开发者必须先完成这些，而非直接开始 H9。
- H9/H10：未开始。

## 本轮关键设计

1. `loop_handoff_state` 新增 `switch_generation`（迁移 `0015_h8_switch_generation`）。它把“切换请求产生的相邻版本”与普通不安全事实更新区分开，避免旧 producer 通过相邻 generation 清除 uncertainty。
2. `ManagedLoopDriver` 仅在 Host 自己成功 CAS 后通过 `note_checkpoint_committed()` 前进 token；读取当前 Driver 名称不会刷新 token。模型/远端决策与每一个工具实际执行前都会检查当前所有权。
3. 运行快照缺失时也校验 durable `session_prefix_hash`；数据库与 JSONL 的 step、位置、完成工具或 pending 工具不一致时暂停，不混合旧展示 payload 与新 durable facts。
4. 远端策略用同一 `CandidateTrialRequest` 运行和取消。取消事件会在派发 FC 前阻止启动；已登记 operation 则继续走 Candidate Runner/FC 的取消与清理路径。

## 已验证

```powershell
cd E:\codex-local\nanobot-devpilot-agent\nanobot
& ..\nanobot.venv\Scripts\python.exe -m pytest --basetemp .pytest-h8-final-batch-11 tests\agent\test_loop_handoff.py tests\command\test_plugin_commands.py -q -k 'handoff or switch_loop'
# 24 passed, 4 deselected

& ..\nanobot.venv\Scripts\ruff.exe check nanobot\agent\runner.py nanobot\agent\loop.py nanobot\agent\loop_handoff.py tests\agent\test_loop_handoff.py

$env:PYTHONPATH='E:\codex-local\nanobot-devpilot-agent\nanobot;E:\codex-local\nanobot-devpilot-agent\devpilot-agent\src'
& ..\nanobot.venv\Scripts\python.exe -m pytest --basetemp .pytest-h8-final-batch-12 ..\devpilot-agent\tests\test_h8_remote_strategy_catalog.py ..\devpilot-agent\tests\test_h5_candidate_runner.py -q
# 46 passed
```

迁移验证也已通过：`devpilot-agent/tests/test_p2a_migration.py`，4 passed。

## 本次续作证据（2026-09-14）

- 真实 FC：使用 `code-interpreter-v1` 与 `provider-default` 网络策略完成一次候选 trial（`passed`、`fc-agent-sandbox`、清理完成）；同一不可变策略完成 live decision，并在重建 Catalog 后以原 trial id 执行 recovery 查询，未重放决策。
- Windows 路径：远端策略 artifact 改为紧凑哈希布局，同时兼容既有 legacy 布局；深路径回归测试通过。
- H8 Host 全链路：新增 `test_h8_agent_loop_commits_a_pending_switch_on_the_next_final_response`，覆盖 `AgentLoop`（WebUI 所用层）从 pending 到 active 的最终响应检查点。当前批次 `41 passed`，Ruff 和 `git diff --check` 均通过。
- WebUI 现场：历史运行中的 WebUI 进程能够接受并持久化 `switch_pending`，但在新只读响应后未写 checkpoint；对同一源码的隔离 `AgentLoop` 复现正常，判定为该旧长驻进程未重载到当前 H8 checkpoint 接线，而非状态机回归。重新部署/重启验证实例前，不得将该旧进程的 pending 状态当作产品失败证据。

## 下一步（严格顺序）

1. 阅读 `AGENTS.md`、`devpilot-agent/docs/plans/CURRENT_PLAN.md`、`devpilot-agent/docs/plans/LIVE_HARNESS_EXTENSION.md`、`devpilot-agent/docs/adr/ADR-0004-batched-review-and-incremental-recheck.md`，以及本文件。
2. 用现有 task 内的子智能体做一次针对最新提交的 Standards / Spec 双轴 delta review；不要创建新的用户可见会话。
3. 补 H8-A–I 的端到端案例，至少覆盖 default→plan→default、工具执行中的切换、旧 producer 不能请求模型/工具、三类 crash window、状态计划重启和 FC 远端策略取消。
4. 用真实 nanobot WebUI + FC 运行记录完成 H8 现场验收，写入 `docs/evidence/live-harness/`；其中 FC 不传网络隔离参数，沿用 `provider-default`。
5. 只有双轴 review 通过且证据齐全，才能将 H8 标记完成并进入 H9。

## 换机启动

```powershell
cd E:\workspace
git clone https://github.com/g3182479125-hub/devflow-agent.git
cd .\devflow-agent
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
```

已克隆仓库则先执行：

```powershell
cd E:\workspace\devflow-agent
git status
git pull --ff-only origin main
```

不要通过 `reset --hard` 覆盖本地修改。API Key、`.env`、SQLite 数据库、日志、虚拟环境与 pytest 临时目录都不会上传。
