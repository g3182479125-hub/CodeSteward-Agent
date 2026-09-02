# FC Agent Sandbox 真实账号复审

日期：2026-09-02
决策：approve-with-follow-ups

## 结论

P3 沙箱接入的核心交付可以结束。当前北京地域账号已通过最小连通性检查，并通过
真实 Control Plane 完成 `Apply -> FC python-run -> Rollback` 端到端复验。剩余事项属于
生产治理和后续增强，不阻断当前 P3 交付。

## 证据

- `uv sync --extra fc-sandbox` 成功安装锁定的 FC 可选依赖。
- `uv run devpilot sandbox-check` 返回 `status=ok`、`backend=fc-agent-sandbox`、退出码 `0`。
- 脱敏临时探针通过 `python-run` 在 FC 中执行，返回退出码 `0` 和
  `devpilot-live-fc-ok`。
- Apply 返回 `applied`，验证返回 `verified`，Rollback 返回 `reverted`。
- 临时探针回滚后不存在；原有 fixture 的用户修改被保留。
- 真实结果未记录 API Key；FC 适配器仍通过环境变量取凭据，未把凭据放入 Tool、Prompt、
  Session、前端或 Git。

## 边界检查

- 验证继续使用服务端 Profile 和命令白名单，模型不能注入任意命令。
- 快照仍受文件数量、单文件大小、总大小和输出大小限制。
- Apply、`r2.verify` 和 Rollback 使用独立授权，未绕过人工确认。
- FC 是远程执行后端，不等同于本机 OS 隔离；本机项目不会被直接挂载到云端。
- 真实复验不证明生产配额、成本、长期可用性或绝对网络隔离。

## 后续建议

保留验证取消、外部存储策略、网络策略细化和成本监控作为后续节点；在进入生产化前，
补充最小权限 API Key、配额告警和定期凭据轮换。它们不是当前 P3 沙箱接入的阻断项。
