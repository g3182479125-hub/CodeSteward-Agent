# DevFlow Agent 换机交接文档（2026-09-08）

## 本次已完成

- 定位并修复一次真实失败：旧的 `devpilot_propose_changeset` 只生成 P2B Preview，
  没有 verification contract，因此后续 `apply_verify` 被安全地拒绝为
  `APPROVAL_BROKER_UNAVAILABLE`。
- `feature-delivery` 的标准入口统一为 `devpilot_feature_delivery_propose`；它会生成带
  `contract_hash`、`check_ids` 等验证契约的 FeatureDelivery Proposal。
- 从 nanobot 的 `feature-delivery` 受限工具视图移除旧提案入口；旧 entry point 仍保留在全局
  及 bug-fix/test-repair 兼容路径中。
- `feature-delivery` profile 版本从 `@1` 升为 `@2`。旧会话不会静默继承新权限，需新建话题
  或使用 `/new` 后重新开始。
- 补充了受限 ToolRegistry 行为测试、真实 `FeatureDeliveryProposeTool` 响应契约测试、旧会话
  stale 绑定测试和 Skill 版本断言。
- 保留当前 Node fixture 中的 `formatGreeting(name)` 示例及测试，作为换机后的快速验证样例。

## 验证结果

以下结果对应最终代码候选，均已实际执行：

- nanobot 受影响测试：`81 passed`
- DevPilot feature-delivery 相关测试：`3 passed`
- nanobot 与 DevPilot 相关 Ruff 检查：通过
- `git diff --check`：通过
- nanobot Gateway：`http://127.0.0.1:18790/health` 返回 `{"status":"ok"}`
- DevPilot 控制面：`http://127.0.0.1:18800/healthz` 返回 `{"status":"ok"}`

本次双轴增量 Review 已通过：

- Standards：PASS
- Spec：PASS
- 最终候选：`80ec5367cd29bdd80210292bc3d7828ab6a1fba5`

## 当前状态与注意事项

- 最后发布提交会包含本交接文档和 Node fixture 的现有未提交修改；这两处 fixture 修改不是
  API Key，也不包含真实项目数据。
- 旧 Changeset `99078b81-0c82-4ffd-84c5-eb989e78f07b` 没有验证契约，不要重试；应重新
  生成一个新的提案。
- 本次修复没有执行真实 LLM/WebUI/FC 全链路验收；已经完成的是离线路由、工具隔离和响应
  契约修复。FC 后端此前的 `sandbox-check` 已通过，但真实验收仍以当前计划与本机凭据为准。
- 不要提交 `.env`、API Key、`local-data/`、SQLite 数据库、运行日志、虚拟环境或
  `node_modules/`。

## 另一台电脑的继续方式

```powershell
cd E:\workspace
git clone https://github.com/g3182479125-hub/devflow-agent.git
cd devflow-agent
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
```

然后按以下文档配置本机模型和 FC 凭据，再启动服务：

- `devpilot-agent/docs/runbooks/local-nanobot-devpilot.md`
- `devpilot-agent/docs/plans/CURRENT_PLAN.md`

启动后先确认 DevPilot 工具已加载，再在 nanobot 中新建话题。建议使用短提示词：

```text
在当前项目新增 formatFarewell(name)，返回 Goodbye, {name}!
先生成 Changeset 和 Diff，等我确认后再执行。
```

提案必须展示 `devpilot_feature_delivery_propose` 生成的验证信息。确认时在同一聊天中发送：

```text
执行
```

如果仍出现 `APPROVAL_BROKER_UNAVAILABLE`，先确认本次是新生成的 Changeset、Gateway 已重启、
当前聊天与提案一致；不要让模型携带 token，也不要绕过 nanobot 原生 WebUI 直接写文件。

## 后续开发边界

- 继续以 nanobot 原生 WebUI 为唯一用户入口，不新增 DevPilot 管理页面。
- 继续使用当前冻结计划和 ADR-0004 的批量修改、双轴 subagent review、增量复审规则。
- 不重新打开 P0–P5；后续行为变化先读取 `AGENTS.md`、相关工程 Skill、ADR-0004 和
  `CURRENT_PLAN.md`。
- 未完成的可选定时批处理 P8-03 不得当作已实现功能。
