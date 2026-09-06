# P6-03/P6-04 验收与启用决定

状态：**降级验证执行能力已启用；网络隔离沙箱未启用。**

本记录把 FC 杭州真实证据、已有控制面回归和网页验收放在同一份清单中。`provider-default`
只表示适配器没有向 FC 发送不支持的 `SessionNetworkPolicy` 参数，不能证明供应商侧断网。

## 能力矩阵

| 项目 | 证据 | 结论 |
|---|---|---|
| 文件边界 | 适配器只上传显式 UTF-8 快照；不挂载主机目录 | 通过 |
| 敏感文件 | `.env`、密钥/证书、依赖和构建目录被排除 | 通过（代码+契约测试） |
| 网络 | 请求固定 `network_allowed=False`；杭州 FC 拒绝 `SessionNetworkPolicy`；兼容模式省略该参数 | **未证明隔离** |
| 资源与时限 | 真实 FC 超时返回 `FC_TIMEOUT`，单次运行最终清理 | 通过 |
| 取消 | 真实 FC 取消调用 `kill()`，状态为 `cancelled` | 通过 |
| 退出码与输出 | 真实 Python 语法错误返回 `exit_code=1` 和 stderr | 通过 |
| 清理与恢复 | 清理失败保留错误；已结束沙箱恢复为 `unknown`，不创建替代任务 | 通过 |
| 网页链路 | nanobot 原生 WebUI：提案→聊天确认→Apply→FC 验证；ChangeSet `edaa74b1-3cb2-4401-bfaa-0f0511109376` | 通过（降级模式） |

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
`test_p5_event_bridge.py`、WebSocket confirmation tests 和 `test_fc_sandbox_contract.py`。
本次真实 FC 证据与 Fake 契约证据分列，Fake 不冒充云端隔离证明。

## P6-04 启用决定

**启用范围**：仅在开发者显式设置 `DEVPILOT_FC_NETWORK_POLICY_MODE=provider-default` 时，
对已确认的、受服务器固定 profile 约束的验证快照提供“降级验证执行”。结果必须带有
`network_policy=provider-default` 和降级状态；不对模型开放任意命令、任意项目或本机 Shell
回退。每次运行仍上传显式快照并在 finally 中清理。

**保持关闭**：网络隔离执行、将 provider-default 当作断网沙箱、自动修复、无人值守 Apply。
在供应商提供可验证的隔离策略或替代隔离证据前，不扩大普通项目范围。

**阻断项**：跨进程后仍能查询供应商运行中任务的持久化凭据/状态证据、严格网络隔离证据、
真实 React 静态执行证据，均不伪造为已完成；它们属于后续能力增强，不影响当前降级验证
模式的失败关闭边界。

审查状态：自动化质量门已通过，正式双轴审查在本批次候选固定后执行；在 Standards/Spec
两轴都完成并记录前，P6 不写 complete，也不启动 P7。
