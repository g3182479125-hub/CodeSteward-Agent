# P6-03/P6-04 验收与启用决定

状态：**FC provider-default 验证执行能力已启用；网络隔离不属于本项目目标。**

本记录把 FC 北京真实证据、已有控制面回归和网页验收放在同一份清单中。当前收口候选为
代码 `65cf72c`，证据与集成测试补丁为 `dd7a253` 及其后续审查记录。`provider-default`
只表示本项目采用 FC 的默认网络策略；本项目不提供也不要求严格断网保证。

## 能力矩阵

| 项目 | 证据 | 结论 |
|---|---|---|
| 文件边界 | 适配器只上传显式 UTF-8 快照；不挂载主机目录 | 通过 |
| 敏感文件 | `.env`、密钥/证书、依赖和构建目录被排除 | 通过（代码+契约测试） |
| 网络 | 请求固定 `network_allowed=False`；北京 FC 使用 `provider-default`，适配器省略不支持的参数 | **按项目范围通过；不提供断网保证** |
| 资源与时限 | 真实 FC 超时返回 `FC_TIMEOUT`，单次运行最终清理 | 通过 |
| 取消 | 真实 FC 取消调用 `kill()`，状态为 `cancelled` | 通过 |
| 退出码与输出 | 真实 Python 语法错误返回 `exit_code=1` 和 stderr | 通过 |
| 清理与恢复 | 清理失败保留错误；已结束沙箱恢复为 `unknown`，不创建替代任务 | 通过 |
| 网页链路 | nanobot 原生 WebUI：提案→聊天确认→Apply→FC 验证；现场闭环 ChangeSet `eb6f1dbb-7d50-4ff3-b6db-84928c2086e8` | 通过（降级模式） |

## 受控场景

- Python bug：失败检查 → 生成提案 → 聊天中明确“执行” → FC 回归通过；未确认、内容
  变化和旁路调用仍由 P6-01/P5 回归拒绝。
- Python 小功能：沿用同一确认和验证链路；应用失败、应用成功但验证失败/未运行、
  验证通过三种结果分开。
- Node/React：保留现有 Node/React fixture；`node-syntax` 属于服务器固定 profile，
  React 静态 profile 仍不启动任意 npm 命令。
- 恢复演练：断开事件连接后通过已有状态查询恢复；服务重启后由持久 ChangeSet/审批
  和幂等键阻止重复 Apply；回滚冲突在目标文件发生外部变化时失败关闭。

对应自动化覆盖包括 `test_p2b_durable_control_plane.py`、`test_p5_action_broker.py`、
`test_p5_event_bridge.py`、WebSocket confirmation tests、`test_fc_sandbox_contract.py` 和
`test_p3_execution_boundary.py`。本轮新增
`test_control_plane_routes_python_regression_to_fc_pytest_bootstrap`，覆盖控制面到 FC 的
profile、固定命令和 bootstrap 顺序。
本次真实 FC 证据与 Fake 契约证据分列，Fake 不冒充云端隔离证明。
逐场景命令、版本和脱敏供应商关联记录见 [P6 真实执行证据清单](p6-real-evidence-manifest.md)。

## P6-04 启用决定

**启用范围**：开发者显式设置 `DEVPILOT_FC_NETWORK_POLICY_MODE=provider-default` 时，
对已确认的、受服务器固定 profile 约束的验证快照提供“降级验证执行”。结果必须带有
`network_policy=provider-default` 和降级状态；不对模型开放任意命令、任意项目或本机 Shell
回退。每次运行仍上传显式快照并在 finally 中清理。

**保持关闭**：将 provider-default 当作断网沙箱、自动修复、无人值守 Apply。
网络隔离不是本项目交付目标；项目 allowlist、固定 profile 和显式确认仍是普通项目范围的
执行边界。

**当前边界**：跨进程供应商运行状态查询已补齐为显式恢复动作：FC 运行元数据以脱敏快照
持久化，服务重启后可按原 `sandbox_id` 调用供应商 `connect`，不会创建替代沙箱或重放命令；
若供应商不可查询则返回 `unknown` 并失败关闭。真实 React 静态执行证据和更细的供应商能力
证明不纳入本次交付。

审查状态：此前 P6 候选的自动化质量门与双轴审查记录属于历史候选，不等同于本轮收口候选
的审查结论。当前收口证据、测试版本和后续复审结果统一记录在 [本轮全系统测试记录]
(../test-full-system-2026-09-07.md) 和交接文档中；独立人工复核见 [P6-04 人工复核记录]
(p6-04-human-review-2026-09-07.md)。
因此 P6 以“启用 provider-default 验证执行”的范围结项；自动修复和无人值守 Apply
仍保持关闭。P7-01 可在此边界内继续开发；本结论不提供严格沙箱保证。
