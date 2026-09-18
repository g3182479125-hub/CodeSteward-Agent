# 2026-09-18：跨电脑回归中的 Runner 结果兼容修复

## 背景

从 `codex/h11-complete-handoff` 拉取 `74470a6` 后，按交接文档复查全量回归。已知的
`exec`、H7 Skill 和 Skill-improvement 环境前置问题在清洁测试环境下可通过；但 Loop
相关回归暴露了一个真实兼容问题。

## 复现与根因

回归用例：

```text
nanobot/tests/agent/tools/test_subagent_tools.py::test_agent_loop_syncs_updated_max_iterations_before_run
```

测试使用的轻量 Runner 结果包含旧版 `AgentRunResult` 字段，但没有新提案确认桥接使用的
`tool_outputs` 字段。`AgentLoop._run_agent_loop` 在提案元数据收尾阶段直接读取该可选字段，
导致普通回合抛出：

```text
AttributeError: 'types.SimpleNamespace' object has no attribute 'tool_outputs'
```

这不是用户模型或 FC 配置问题，而是 `3a03ab5` 引入提案跨传输修复时遗漏了旧结果形状兼容。

## 修复

`nanobot/nanobot/agent/loop.py` 在提案桥接入口统一将缺失或空的 `tool_outputs` 视为空序列。
有新字段的正式 Runner 仍照常提取服务端提案元数据；没有该字段的旧 Runner 或测试替身
仍能完成普通回合，不会因为可选确认元数据而失败。

## 验证证据

在 Windows 本机使用 nanobot 虚拟环境、仓库外独立 pytest 临时目录执行：

```powershell
$env:DEVPILOT_ENABLE_SKILL_IMPROVEMENT = '0'
python -m pytest tests/agent/tools/test_subagent_tools.py::test_agent_loop_syncs_updated_max_iterations_before_run -q
```

结果：`1 passed`。

受影响的 Loop/Runner/WebSocket 回归集合：

```text
tests/agent/tools/test_subagent_tools.py
tests/agent/test_loop_tool_context.py
tests/agent/test_loop_save_turn.py
nanobot/channels/websocket/tests/test_websocket_channel.py
```

结果：`279 passed`。

DevPilot 提案、交付和 Action Broker 回归集合：

```text
tests/test_p4_bug_fix.py
tests/test_p4_feature_delivery.py
tests/test_p5_action_broker.py
```

结果：`77 passed`。

Ruff 对变更文件检查通过。完整 nanobot 测试的本次运行不能作为全绿证据：Windows 下整套
`pytest` 进程在约 64% 处以退出码 0 结束但没有正常汇总；该仓库在相近测试范围包含会启动
独立强制退出子进程的 H3 测试，具体截断原因仍需拆分测试批次继续定位。因此本证据只报告
实际完成的针对性回归，不把未完成的全量扫描写成通过。

## 后续

- 这次修复不新增冻结计划节点，也不改变确认、Apply、Verify、Rollback 或权限边界。
- 换电脑录制前仍需按交接文档配置自己的模型、重新登记并 trust 训练项目。
- 若要声称全量测试通过，应先将全套测试拆成不会被强制退出子进程截断的可收集批次，分别
  保存退出码和汇总结果。
