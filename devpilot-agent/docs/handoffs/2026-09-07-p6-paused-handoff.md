# 2026-09-07 P6 暂停交接

## 当前结论

本次工作停在 P6，不进入 P7。P7-01 之前误启动过，但新增的 task-profile 代码已经撤回；当前仓库没有 P7 实现。P6 的实现批次已提交并推送，自动化检查通过，但最后一次 Standards/Spec 双轴增量复审需要明天继续收口，不能把当前状态写成 P6 完成。

## 已完成并提交

- FC 杭州 `provider-default` 兼容：不再发送杭州不支持的 `SessionNetworkPolicy`，结果标记 `network_policy=provider-default`，不宣称网络隔离。
- FC 真实执行生命周期：显式快照、服务器固定命令、超时、取消、结果收集、清理、清理失败重试和重启后未知状态。
- FC 真实能力按 profile 收紧：当前只开放有真实证据的 `python-syntax`、`python-run`、`node-syntax`；`python-regression` 和 `react-static` 仍关闭/未证明。
- provider-default 远程执行增加 `DEVPILOT_FC_ALLOWED_PROJECTS` 精确项目 allowlist；未命中时拒绝上传。
- nanobot 当前请求上下文只读取本次 WebSocket 确认，不复用旧 inbound confirmation。
- CLI 输出 `status=degraded`、`network_policy` 和 `network_isolation_proven=false`；runbook 已区分 strict 与 provider-default。
- 增加无独立审批页的清理重试入口：`POST /v1/change-sets/{change_set_id}/verification-cleanup`。
- 证据清单：`docs/evidence/phase-6/p6-real-evidence-manifest.md`、`p6-02-fc-hangzhou-diagnosis.md`、`p6-03-04-acceptance.md`。

## 最后提交

- 代码候选：`1161722eb369799c44ac8befc30f708806dc5343`（取消状态最终收敛修复）。
- 当前本地提交：`5e489de1b16dcd4cdd1dfdde40cb1902c8d092d0`（证据文档提交，祖先包含代码候选）；本次暂停前还会再提交 profile 收紧和本交接文档。
- 远程仓库：`https://github.com/g3182479125-hub/nanobot-devpilot-agent.git`，分支 `main`。
- 证据清单中的真实 CLI 记录：杭州 `code-interpreter-v1`，sandbox `sbx-dc40e18d-87b0-4c57-9847-2621053c04ef`，退出码 0，`status=degraded`，`network_isolation_proven=false`。

## 已验证

在 E 盘集成环境中：

- DevPilot 全套测试：通过（含既有 skip）。
- nanobot 请求上下文、WebSocket 确认和通道集成：28 passed。
- FC 合同测试：最终 profile 收紧前 21 passed；收紧变更只减少 provider profile 声明，需明天再跑一次最终候选的同一命令确认。
- Ruff、secret scan、Markdown link check：通过。
- 真实杭州 FC CLI：通过，但仅证明 provider-default 降级验证执行，不能证明断网。

## 明天第一步

1. `git clone https://github.com/g3182479125-hub/nanobot-devpilot-agent.git`；已有副本则 `git pull --ff-only origin main`。
2. 读取 `AGENTS.md`、`agent-skills/mattpocock-skills/README.md`、相关 engineering skills、`docs/plans/CURRENT_PLAN.md`、ADR-0004 和本交接文档。
3. 在新机器本地填写 `E2B_API_KEY`、`E2B_API_URL`、`E2B_DOMAIN`，设置 `DEVPILOT_FC_NETWORK_POLICY_MODE=provider-default`、`DEVPILOT_FC_TEMPLATE=code-interpreter-v1`，并把 `DEVPILOT_FC_ALLOWED_PROJECTS` 指向明确的脱敏 fixture；不要把 key、`.env`、SQLite 或日志提交。
4. 用 nanobot 集成环境重跑 DevPilot 全套测试、nanobot 28 项集成测试和真实 `devpilot sandbox-check`。
5. 只针对“移除 `python-regression` provider 支持”做一次 Standards/Spec 并行 subagent review；两轴 PASS 后，更新 `CURRENT_PLAN.md` 和本阶段记录为 P6 完成，再由用户决定是否进入 P7。

## 当前明确未完成/未证明

- 最后一次 Standards/Spec 复审尚未因 profile 收紧变更重新执行；这是当前唯一的 P6 流程卡点。
- provider-default 不是网络隔离；严格网络策略仍需供应商可验证能力。
- 真实 React 静态执行、跨进程供应商状态查询/清理未启用。
- P7-01 未开始，不要先实现任务 profile、上下文优化或评测功能。

## 不要做的事

- 不要把 provider-default 写成断网沙箱。
- 不要移除项目 allowlist、profile 能力门禁或 nanobot 当前请求确认绑定。
- 不要把未跟踪的 pytest 临时目录上传。
- 不要用 `git reset --hard`、覆盖用户 fixture 或删除本地数据来“清理”工作区。
