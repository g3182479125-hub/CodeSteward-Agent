# H10 产品交付评审计划

日期：2026-09-15。范围：H10 全链路与换机交付，不重新认证 H1-H9。

## 用户结果与明确非目标

用户应能在原生 nanobot WebUI 中完成受控项目提案、聊天确认、真实 FC 验证、结果查询、
受保护撤销和刷新重连，并能在另一台电脑按交接文档继续开发。评审不把本地 fake 当真实
FC，不把 provider-default 当网络隔离保证，不要求新增页面、第二 Runtime 或生产 SLA。

## 验收问题与所需证据

| 节点 | 评审问题 | 可观察证据 |
| --- | --- | --- |
| H10-A | H2/H6/H8 网页证据和本轮真实 FC feature-delivery 链路是否可追溯？ | WebUI chat/时间、Profile、ToolResult、ChangeSet/Run、FC sandbox/模板/地域/退出码、rollback 与回滚后文件哈希 |
| H10-B | 原有能力和本轮安全门控是否回归通过？ | 聚焦 pytest、Ruff、WebUI lint/build、`git diff --check` |
| H10-C | 换机/故障恢复是否隔离且 fail-closed？ | 原地 `git pull --ff-only`、隔离迁移、缺失/不兼容/未结算恢复测试与预期终态 |
| H10-D | 文档是否逐项区分完成、限制、历史证据和未实现项？ | CURRENT_PLAN、扩展计划、H10 evidence、handoff、review report |

## 安全、隐私与兼容性检查

- 不读取、提交或展示 API key、bootstrap token、授权 token、完整配置或完整模型日志。
- 模型只能登记 `r1.rollback` 审批意图，控制面用户动作才签发 token；不允许本机 Shell
  fallback、路径越界或旁路写入。
- 只使用隔离 demo、数据库和 WebUI 运行态；保留用户已有 `package-lock.json` 修改及既有
  未跟踪临时目录，不执行 reset、覆盖或删除。
- 保留 provider-default 的真实限制标记；不把回滚报告中的应用后 manifest 误称为回滚后
  磁盘内容。

## 严重度与决策

- blocker：真实链路不可验证、越权/秘密泄露、用户数据被覆盖或关键回滚事实缺失。
- must-fix-before-next-node：验收条款未满足但不构成越权或数据损坏。
- follow-up：不阻断 H10，但需要后续增强的可观测性或测试。
- observation：已知边界、测试样本限制或工具链观察项。

评审结论使用 `APPROVE`、`APPROVE_WITH_FOLLOW-UPS`、`REQUEST_CHANGES` 或 `BLOCKED`。
