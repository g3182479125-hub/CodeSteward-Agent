# FC Agent Sandbox 接入端口产品复审计划

## 目标与非目标

目标是证明 DevPilot 能通过唯一的 `ExecutionBackend` 端口，把受限的验证文件包发送到
阿里云 FC Agent Sandbox，执行服务端选择的验证 Profile，返回有界结果，并在成功或失败
后销毁临时 Sandbox；同时保留 Apply、验证和 Rollback 的独立授权边界。

非目标包括：把 FC 暴露为 nanobot Tool、允许模型提供任意 Shell 命令、把本机目录挂载到
云端、自动部署或推送 Git、宣称 Windows 本机具备 OS 级隔离，以及宣称生产级成本或
可用性指标。

## 验收问题

1. `sandbox-check` 是否能使用目标地域和模板完成真实创建、写入、执行、销毁？
2. Control Plane 是否在人工批准后才 Apply，并使用独立 `r2.verify` 授权执行验证？
3. 验证是否只使用完整但有界的快照、服务端命令白名单和禁止网络策略？
4. 验证成功后是否能通过独立 `r1.rollback` 授权恢复本机状态？
5. 失败、超限、密钥缺失和不合法路径是否 fail closed，且不泄露凭据？

## 必须检查的证据

- 本地契约测试、类型检查、静态检查、密钥扫描和 Markdown 检查。
- 真实账号 `sandbox-check` 的脱敏输出。
- 一条真实的 `Apply -> FC verify -> Rollback` fixture 链路。
- 结果中的 backend、sandbox 标识存在性、Profile、退出码和有界 stdout/stderr。
- Git diff 中无 API Key、无本机项目快照和无独立审批页面回归。

## 决策规则

- `approve`：核心链路和边界均有证据，无未解决的阻断问题。
- `approve-with-follow-ups`：核心链路通过，但生产治理或增强能力仍需后续处理。
- `request-changes`：实现或证据不满足核心边界。
- `blocked`：外部账号、资源或权限阻止核心验收，且无法通过替代证据完成。

## 审查输出

审查报告记录决策、检查过的文件和命令、真实验收结果、未覆盖边界、敏感信息处理、
兼容性风险和后续建议；不修改应用代码或配置。
