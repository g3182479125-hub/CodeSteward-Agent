# 2026-09-15 H10 实时 Harness 验证交接

## 当前基线

- 本次验证机仓库根目录：`E:\codex-local\nanobot-devpilot-agent`（历史现场路径；换机后以
  `git rev-parse --show-toplevel` 得到的当前仓库根目录为准，不要照抄盘符）。
- `start-integrated-harness.ps1` 对隔离状态和允许根目录保留 E: 盘安全门禁；新电脑没有 E: 时，
  不要直接放宽门禁，需先按计划调整并重新完成双轴复核。
- 分支：`handoff-2026-09-01`
- 本次提交完成后，以该分支最新 HEAD 为准；不要直接覆盖用户的本地运行态、`.env`、数据库或聊天记录。
- 历史 H1-H9 已完成。本交接文档创建时 H10 尚未完成；本轮已补齐 H10-A 的真实 FC
  网页闭环，H10-B/H10-C 已通过，当前只剩本轮 Standards / Spec 双轴增量复核后封存 H10-D。

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

## 本轮真实 FC 收尾结果（2026-09-15）

- 用户既有 FC 配置已被正确发现并实际使用；本轮没有使用 fake 代替真实 FC，也没有输出配置
  中的任何密钥。运行态、数据库和 demo 项目均为隔离副本。
- 真实 WebUI chat：`be3cfa0e-039f-4966-b5c8-c2715542162a`；项目：`h10-live-demo`，
  `project_id=dc2629d1-e9ff-466c-ba68-efa96fc1e12b`；ChangeSet：
  `bf33c510-de9f-47ac-93cf-194669c42382`；Run：`9b9b6e19-78a5-49f9-b3f2-24fcd28bb053`。
- 12:36 的 `apply_verify` 真实回执为 `applied + verified`，FC 为
  `fc-agent-sandbox / code-interpreter-v1 / cn-beijing`，固定检查为 `python-syntax`，
  `exit_code=0`，耗时 1417ms，策略为 `provider-default`。
- 12:36 的“撤销”先得到 `approval_pending`，未返回 token；12:39:10 由控制面完成
  `r1.rollback` 审批和 rollback，最终 `status=reverted`。这证明模型不能绕过控制面权限。
- 刷新 WebUI 后，12:41 同一会话只读读取 `main.py`，确认内容恢复为 `Decimal("10")`，
  当前 SHA-256 为 `85917786c8f56a32aba9c5b005cecdee4ddab184d0995f16b0315f3a93ade54e`，
  与 rollback/before manifest 一致。注意 delivery report 的 `after_manifest` 保留的是
  应用后版本，不能当作回滚后的磁盘哈希。
- H10-B：后端聚焦测试通过，Ruff 通过，WebUI lint/build 通过；H10-C：隔离恢复分支已通过。

本轮真实证据的完整记录已追加到 `docs/evidence/live-harness/h10-delivery.md`。此前“真实 FC
仍待完成”的段落是本交接创建时的历史状态，以上收尾结果优先。

## 原 H10 卡点（本轮已解决）

### A. 真实 FC 验证和 Rollback 同一会话证据：已解决

- 控制面 `/v1/security/backends` 会把 provider-default FC 显示成 `status=degraded, verified=false`；这里的 `verified=false` 是“未做主动健康探针”的展示语义，**不直接等于不可用**。
- 第一轮使用了 `python-regression`，FC 只支持 `python-syntax`、`python-run`、`node-syntax`，因此预览正确地标为不可验证。H10 演示必须用 `python-syntax`，并在文档说明它是语法验证，不是单元测试回归。
- 本轮已提取真实 DeepSeek + WebSocket 会话的最终 ToolResult；真实 FC 的 Apply/Verify、控制面
  rollback 审批与回滚后只读哈希复核均已完成，详见上面的真实 FC 收尾结果。
- 隔离 H10 demo 项目及运行态只应放在当前电脑的临时状态根目录下，均不提交；历史验证使用过
  `E:\codex-local\nanobot-state\workspace\h10-live-demo` 与
  `E:\codex-local\nanobot-state\h10-isolated-runtime`，换机后不要照抄盘符，按
  `docs/evidence/live-harness/h10-delivery.md` 重建。只信任这一临时项目，绝不向用户真实项目写入。

### B. 用户可见 WebUI 与真实 FC 点测：已完成

本轮已使用新聊天、选择 `feature-delivery` 后完成以下步骤：

1. 请求分析隔离 demo 的 `main.py`，提出将 `Decimal("10")` 改为 `Decimal("100")`，指定 `python-syntax`；检查 Diff 与 ChangeSet。
2. 单独发送精确文本 `执行`；确认工具轨迹含 `devpilot_confirmed_action("apply_verify")`，并显示 FC 验证结果。
3. 请求 `devpilot_get_delivery_report`，核对 Applied/verified 结果和 sandbox/run 引用。
4. 单独发送精确文本 `撤销`；确认只有 rollback、没有重新 Apply，代码恢复。
5. 刷新或断开 WebUI 后重连，确认聊天/运行状态可查询。

任何复跑失败时，仍应只保留秘密外的错误码、ChangeSet ID、chat ID 和事件时间；不要打印 API Key、bootstrap token 或完整 config。

## 推荐继续顺序（H10 收尾）

1. 先运行本次的 focused tests、Ruff、WebUI build/lint；修复失败再进入真实 FC。
2. 真实 FC 清单、时间、结果和已知边界已追加到 `h10-delivery.md`。
3. H10-B/H10-C 回归、Ruff、WebUI lint/build 已完成并保留本轮结果。
4. 最后向原 Standards、Spec 两个 reviewer subagent 发送**本次增量**复核请求。两者 PASS 后将 H10 标记完成、进入后续工作。

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
