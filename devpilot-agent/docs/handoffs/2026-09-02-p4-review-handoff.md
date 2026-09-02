# 2026-09-02 P4-01 Review 交接记录

## 当前基线

- Git 仓库：`https://github.com/g3182479125-hub/nanobot-devpilot-agent`
- 当前分支：`handoff-2026-09-01`
- 当前实现提交：`92ef68a6cb2d52609cee770e788d06eef43dc8f9`
- 冻结计划：`DEVPILOT_FROZEN_PLAN.md` v2.2.0
- 冻结计划 SHA-256：`A36B88E8AE287DFE63A39B5EEF6DD6E5CA2954A20C8DAD48B3472CC604378459`
- 本次 Review：独立只读 subagent，基于 clean checkout `92ef68a`

## P4-01 结论

P4-01 `feature-delivery` Skill/Workflow 与 G-01 已完成主要实现，但独立 Review 结论为 `request-changes`，P4-02 暂不解锁。此次不是因为截图、文档排版或非关键性能数据，而是以下真实边界问题：

1. **Blocker：** nanobot 注册了 `devpilot_execute_changeset`、`devpilot_verify_changeset`、`devpilot_rollback_changeset`，并直接接收 `approval_token`；R1/R2 应由外部控制面用户动作触发，token 不应进入 nanobot Tool 参数。
2. **Major：** `feature-delivery` Skill 的允许 Tool 与实际 entry point/schema 不一致，缺少统一的 request-approval/diff contract。
3. **Major：** P4 manifest 仍绑定旧实现提交 `6d9c11a`、旧计划 v2.1.0，而不是当前 `92ef68a` 与 v2.2.0；其中 uv lock hash 也需重新核对。
4. **Major：** Project 持久化模型和策略尚未证明 `trust_state`、`repo_fingerprint`、`allowed_root_id` 以及 trusted gate 的完整闭环。

## 已验证通过

- P4 专项测试通过：14 项。
- 完整 Python 测试通过：118 passed，3 skipped（Windows 符号链接权限条件）。
- Ruff、Pyright、uv lock、Secret scan、Markdown/link check 通过。
- Alembic 从空内存 SQLite 升级至 `0006_p4_contract_binding` 通过。
- Fake 成功/非零失败、Artifact DB failure 兜底、终态保护、审批/哈希/幂等/回滚等已有测试证据。

## 下一步顺序

1. 移除或隔离 nanobot 侧 R1/R2 execute/verify/rollback Tool 的 token 直传路径。
2. 统一 Skill allowed list、entry point、schema 和 contract tests。
3. 重新生成 P4 evidence manifest，绑定当前实现提交与 v2.2.0 计划。
4. 补齐 Project trust/fingerprint/allowed-root 服务端 gate 及跨项目、跨 Run、跨 Task 拒绝测试。
5. 在新的 clean checkout 重跑 P4 测试和质量检查，再启动新的独立只读 Review。
6. 只有 Review 变成 `approve`，或仅剩明确登记的 minor follow-up 后，才能解锁 P4-02。

## 本地未提交文件

以下两个文件是用户在 P4 之前已有的 Node fixture 修改，本次没有修改、暂未提交，也不能删除：

- `tests/fixtures/node-project/src/index.js`
- `tests/fixtures/node-project/test/index.test.js`

## 启动提示

在另一台电脑克隆后，先进入 `devpilot-agent`：

```powershell
uv sync --dev
uv run alembic upgrade head
```

P4-01 尚未通过前，不要把 nanobot 对话中的执行工具当作最终安全契约；继续开发必须先处理本交接记录中的 blocker/major。
