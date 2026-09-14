# 2026-09-15 H10 实时 Harness 验证交接

## 当前基线

- 仓库根目录：`E:\codex-local\nanobot-devpilot-agent`
- 分支：`handoff-2026-09-01`
- 本次提交完成后，以该分支最新 HEAD 为准；不要直接覆盖用户的本地运行态、`.env`、数据库或聊天记录。
- 历史 H1-H9 已完成。H10 **尚未完成**：本次只提交可复现的 H10 测试基础、两个真实链路缺陷修复和当前证据，不能在计划中标记 H10 done。

## 本次已完成

### 1. H10 本地运行与性能基础

- 新增 `scripts/start-integrated-harness.ps1`：仅允许 E: 路径，检查 H9 完整性密钥，迁移 DevPilot 数据库，等待控制面健康后启动原生 nanobot WebUI。
- 新增 `scripts/h10_lifecycle_benchmark.py`：连续 50 次 PluginManager load/stop 后检查残留路由、drain 任务。最近结果：P50 `1.465 ms`、P95 `2.059 ms`、三类残留均为 `0`。
- 新增 H10 证据模板和本地运行手册；明确临时隔离运行态不能覆盖用户真实 `config.json`、数据库或聊天记录。

### 2. 原生 WebUI 实测暴露并修复的缺陷

两项都先补了会失败的回归测试，再修复：

1. **P8 数据库 schema 向后兼容错误**
   - 现象：已经迁移到 H9 的数据库在 feature-delivery 的只读检查阶段被旧的 P8 schema head 校验拒绝。
   - 修复：`storage.py` 明确允许 `0011` 的顺序后继 `0012` 至 `0016`，未知分叉历史仍 fail-closed。
   - 测试：`test_p8_schema_requirement_accepts_the_current_h9_head`。
2. **Feature Delivery 返回的预览与验证 Profile 不一致**
   - 现象：提案请求 `python-regression`，但预览仍绑定默认 `python-syntax`；确认执行会因完整 tuple 不一致被安全拒绝。
   - 修复：先持久化含 `verification_profile` 的 proposal 事件，再从 manager 重读 preview，返回服务器绑定后的预览。
   - 测试：`test_feature_delivery_preview_binds_requested_regression_profile`。
3. **弱模型容易把 `project_id` 错传为 evidence**
   - 修复：FeatureDelivery Tool schema 与 `feature-delivery` Skill 均明确：`evidence_ids` 只能是同一 `devpilot_inspect_project` 的 `repo-analysis run_id`，绝不能传 `project_id`。

### 3. 原生 WebUI 协议实测

- 使用的是 nanobot 自身 `/webui/bootstrap` + WebSocket 网关协议，不是 AG-UI，也没有改用户的真实聊天服务。
- 已在隔离 WebUI 会话完成：`feature-delivery` Profile → inspect → search/read → proposal → `devpilot_confirmed_action(action=apply_verify)` → delivery report/run state 查询。
- 最新隔离会话 ID：`27504cd3-cf72-4c9e-906b-86048e0071ac`；对应原始事件记录位于本机隔离状态目录，不纳入 Git。
- Codex 内置浏览器自动点击 Edge 时被客户端策略拦截（无法可靠识别当前页面 URL），因此**不能**把这次协议级实测表述成已经完成了人工网页点击验收。下次应由操作者在原生 WebUI 新聊天中按下文清单点测，或使用可控浏览器环境。

## 当前 H10 卡点（下一位开发者必须先处理）

### A. 全链路尚缺 FC 验证成功和 Rollback 成功的同一会话证据

- 控制面 `/v1/security/backends` 会把 provider-default FC 显示成 `status=degraded, verified=false`；这里的 `verified=false` 是“未做主动健康探针”的展示语义，**不直接等于不可用**。
- 第一轮使用了 `python-regression`，FC 只支持 `python-syntax`、`python-run`、`node-syntax`，因此预览正确地标为不可验证。H10 演示必须用 `python-syntax`，并在文档说明它是语法验证，不是单元测试回归。
- 最新原生会话已经让模型调用 `devpilot_confirmed_action(action=apply_verify)`，但交接前未提取该调用的最终 ToolResult，也未完成随后的独立 `撤销` 成功回执。不得假定成功。
- 隔离 H10 demo 项目及运行态位于 `E:\codex-local\nanobot-state\workspace\h10-live-demo` 与 `E:\codex-local\nanobot-state\h10-isolated-runtime`，均不提交；若不存在则按 `docs/evidence/live-harness/h10-delivery.md` 重建。只信任这一个临时项目，绝不向用户真实项目写入。

### B. 用户可见 WebUI 点测仍待完成

建议使用新聊天、选择 `feature-delivery` 后依次输入：

1. 请求分析隔离 demo 的 `main.py`，提出将 `Decimal("10")` 改为 `Decimal("100")`，指定 `python-syntax`；检查 Diff 与 ChangeSet。
2. 单独发送精确文本 `执行`；确认工具轨迹含 `devpilot_confirmed_action("apply_verify")`，并显示 FC 验证结果。
3. 请求 `devpilot_get_delivery_report`，核对 Applied/verified 结果和 sandbox/run 引用。
4. 单独发送精确文本 `撤销`；确认只有 rollback、没有重新 Apply，代码恢复。
5. 刷新或断开 WebUI 后重连，确认聊天/运行状态可查询。

任何一步失败，应保留秘密外的错误码、ChangeSet ID、chat ID 和事件时间；不要打印 API Key、bootstrap token 或完整 config。

## 推荐继续顺序

1. 先运行本次的 focused tests、Ruff、WebUI build/lint；修复失败再进入真实 FC。
2. 在隔离环境用 `python-syntax` 重跑同一聊天链路并提取实际 ToolResult；验证后再做 rollback，检查 demo `main.py` 已恢复为 `/ Decimal("10")`。
3. 完成用户可见 WebUI 点测或明确记录浏览器自动化限制和手工证据。
4. 追加 `h10-delivery.md` 的真实时间、命令、结果、已知边界；再运行 H10-B/H10-C 回归/恢复清单。
5. 最后向原 Standards、Spec 两个 reviewer subagent 发送**本次增量**复核请求。两者 PASS 后才可将 H10 标记完成、进入后续工作。

## 建议验证命令

在仓库根目录 PowerShell：

```powershell
$env:PYTHONPATH = 'E:\codex-local\nanobot-devpilot-agent\nanobot;E:\codex-local\nanobot-devpilot-agent\devpilot-agent\src'
& 'E:\codex-local\nanobot-devpilot-agent\nanobot\.venv\Scripts\python.exe' -m pytest `
  devpilot-agent/tests/test_p2a_migration.py `
  devpilot-agent/tests/test_p4_feature_delivery.py `
  devpilot-agent/tests/test_h6_model_plugins.py `
  devpilot-agent/tests/test_h8_remote_strategy_catalog.py `
  devpilot-agent/tests/test_h9_harness_selection.py `
  devpilot-agent/tests/test_h9_harness_oracle.py `
  nanobot/tests/agent/test_loop_handoff.py `
  nanobot/tests/command/test_plugin_commands.py -q

& 'E:\codex-local\nanobot-devpilot-agent\devpilot-agent\.venv\Scripts\ruff.exe' check devpilot-agent/src devpilot-agent/tests
Set-Location nanobot/webui
npm run lint
npm run build
```

`nanobot/webui/package-lock.json` 是用户本地修改，始终不要 stage、reset 或提交。根目录和各模块的历史 `.pytest-*` 临时目录也不提交。
