# 2026-09-15 H10 实时 Harness 验证交接

## 当前基线

- 本次验证机仓库根目录：`E:\codex-local\nanobot-devpilot-agent`（历史现场路径；换机后以
  `git rev-parse --show-toplevel` 得到的当前仓库根目录为准，不要照抄盘符）。
- `start-integrated-harness.ps1` 对隔离状态和允许根目录保留 E: 盘安全门禁；新电脑没有 E: 时，
  不要直接放宽门禁，需先按计划调整并重新完成双轴复核。
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
- 已在隔离 WebUI 会话完成：`feature-delivery` Profile → inspect → search/read → proposal → `devpilot_confirmed_action(action=apply_verify)` → delivery report/run state 查询；本轮进一步完成了独立 `rollback` 和刷新重连。
- 本轮本地确定性闭环 chat：`4d763777-79b2-43de-b179-95feb8762ad8`；ChangeSet：`134ca8fc-0211-482c-8e84-f2cd6c25cc0c`；Run：`69a42b14-01fe-4ecd-b8df-9e6f9e45569e`。事件记录位于本机隔离状态目录，不纳入 Git。
- `apply_verify` 的最终 ToolResult 为 `application=applied`、`verification=verified`，delivery report 为 completed；随后独立 `rollback` 的最终 ToolResult 为 `reverted`，第二次 `read_code` 读回基线哈希 `85917786…3a93ade54e`。刷新后 WebUI 仍显示提案、执行、回滚三组工具轨迹。
- 以上后端是本地确定性 `FakeExecutionBackend`，回执标记 `backend=fake`，不是阿里云 FC；因此不能把该闭环写成真实 FC 验收或 H10-A 完成。
- Codex 内置浏览器之前的 Edge 识别限制不再阻塞本轮本地 WebUI 点测；本轮通过可控的本地 WebUI 完成了用户可见输入和重连验证。真实 FC 证据仍待具备凭据后由操作者运行。

## 当前 H10 卡点（下一位开发者必须先处理）

### A. 全链路仍缺真实 FC 验证成功和 Rollback 成功的同一会话证据

- 控制面 `/v1/security/backends` 会把 provider-default FC 显示成 `status=degraded, verified=false`；这里的 `verified=false` 是“未做主动健康探针”的展示语义，**不直接等于不可用**。
- 第一轮使用了 `python-regression`，FC 只支持 `python-syntax`、`python-run`、`node-syntax`，因此预览正确地标为不可验证。H10 演示必须用 `python-syntax`，并在文档说明它是语法验证，不是单元测试回归。
- 本轮已提取本地确定性会话的最终 ToolResult，并完成独立 `撤销` 成功回执；但该结果来自 `FakeExecutionBackend`，不满足真实 FC 证据要求，不能据此标记 H10-A 完成。
- 隔离 H10 demo 项目及运行态只应放在当前电脑的临时状态根目录下，均不提交；历史验证使用过
  `E:\codex-local\nanobot-state\workspace\h10-live-demo` 与
  `E:\codex-local\nanobot-state\h10-isolated-runtime`，换机后不要照抄盘符，按
  `docs/evidence/live-harness/h10-delivery.md` 重建。只信任这一临时项目，绝不向用户真实项目写入。

### B. 用户可见 WebUI 本地点测已完成，真实 FC 点测仍待完成

真实 FC 凭据可用后，仍需使用新聊天、选择 `feature-delivery` 后依次输入：

1. 请求分析隔离 demo 的 `main.py`，提出将 `Decimal("10")` 改为 `Decimal("100")`，指定 `python-syntax`；检查 Diff 与 ChangeSet。
2. 单独发送精确文本 `执行`；确认工具轨迹含 `devpilot_confirmed_action("apply_verify")`，并显示 FC 验证结果。
3. 请求 `devpilot_get_delivery_report`，核对 Applied/verified 结果和 sandbox/run 引用。
4. 单独发送精确文本 `撤销`；确认只有 rollback、没有重新 Apply，代码恢复。
5. 刷新或断开 WebUI 后重连，确认聊天/运行状态可查询。

任何一步失败，应保留秘密外的错误码、ChangeSet ID、chat ID 和事件时间；不要打印 API Key、bootstrap token 或完整 config。

## 推荐继续顺序

1. 先运行本次的 focused tests、Ruff、WebUI build/lint；修复失败再进入真实 FC。
2. 已完成本地 fake 的 `python-syntax` 聊天链路、实际 ToolResult、rollback 和刷新重连；真实 FC 凭据可用后按同一清单重跑并单独记录。
3. 追加 `h10-delivery.md` 的真实时间、命令、结果、已知边界；再运行 H10-B/H10-C 回归/恢复清单。
4. 最后向原 Standards、Spec 两个 reviewer subagent 发送**本次增量**复核请求。两者 PASS 后才可将 H10 标记完成、进入后续工作。

## 建议验证命令

在当前仓库根目录 PowerShell（不要假设固定盘符）：

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
$env:PYTHONPATH = "$repoRoot\nanobot;$repoRoot\devpilot-agent\src"
& "$repoRoot\nanobot\.venv\Scripts\python.exe" -m pytest `
  "$repoRoot\devpilot-agent\tests\test_p2a_migration.py" `
  "$repoRoot\devpilot-agent\tests\test_p4_feature_delivery.py" `
  "$repoRoot\devpilot-agent\tests\test_h6_model_plugins.py" `
  "$repoRoot\devpilot-agent\tests\test_h8_remote_strategy_catalog.py" `
  "$repoRoot\devpilot-agent\tests\test_h9_harness_selection.py" `
  "$repoRoot\devpilot-agent\tests\test_h9_harness_oracle.py" `
  "$repoRoot\nanobot\tests\agent\test_loop_handoff.py" `
  "$repoRoot\nanobot\tests\command\test_plugin_commands.py" -q

& "$repoRoot\devpilot-agent\.venv\Scripts\ruff.exe" check "$repoRoot\devpilot-agent\src" "$repoRoot\devpilot-agent\tests"
Set-Location "$repoRoot\nanobot\webui"
npm run lint
npm run build
```

`nanobot/webui/package-lock.json` 是用户本地修改，始终不要 stage、reset 或提交。根目录和各模块的历史 `.pytest-*` 临时目录也不提交。
