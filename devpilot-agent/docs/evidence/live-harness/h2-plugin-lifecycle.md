# H2 动态插件生命周期验收证据

日期：2026-09-10
代码候选：`b8141f0d618119efd7680864f9f7d185557c5d24`
初始 H2 候选：`de03e73cf884eb7f7c6ebcfeee0fba2193299f03`
比较基线：H1 文档提交 `9813301`

## 范围

本节点只实现已登记的 developer-owned、`in_process`、`read_only` Tool 插件的
session/project 级在线 `list`、`load`、`stop`、`status`。它叠加在原有
`ToolRegistry` 视图上，不替换旧 `ToolLoader`，不做模型生成代码、持久化、依赖求解、
FC 执行或 Loop Driver 替换。

## 实现位置

- `nanobot/nanobot/agent/plugins/manager.py`：清单校验、作用域授权、动态路由、generation、
  lease、停用排空、操作状态和 Registry overlay。
- `nanobot/nanobot/agent/plugins/contracts.py`：动态插件策略和 H2 错误码。
- `nanobot/nanobot/agent/loop.py`：在每轮恢复工具视图时接入 manager；基础工具装配不变。
- `nanobot/nanobot/command/plugin_commands.py`：原生聊天命令适配器。
- `nanobot/nanobot/command/builtin.py`：只增加 `/plugins` 命令规格和注册。
- `nanobot/tests/agent/test_plugin_manager.py`、
  `nanobot/tests/command/test_plugin_commands.py`：生命周期、隔离、策略和命令测试。

## 可复现实验

在 `E:\codex-local\nanobot-devpilot-agent\nanobot` 执行：

```powershell
$env:TEMP='E:\codex-local\pytest-temp-h2'
$env:TMP='E:\codex-local\pytest-temp-h2'
& .venv\Scripts\python.exe -m pytest `
  tests/agent/test_plugin_contracts.py `
  tests/agent/test_plugin_manager.py `
  tests/command/test_plugin_commands.py `
  tests/agent/test_loop_runner_integration.py `
  tests/agent/test_loop_session_policy.py `
  tests/agent/test_loop_tool_context.py `
  tests/agent/test_task_profile_session.py `
  tests/command
```

结果：最终候选 `164 passed`。Ruff 对 H2 变更通过；basedpyright 对 manager、contracts、
plugin command 通过。

## 行为证据

1. 测试先把 `devflow-math@0.1.0` 的开发者清单和 SHA-256 工件登记到 manager，随后同一个
   `AgentLoop`、同一个 `session_key` 和同一个进程 PID 执行 `/plugins load`。
2. load 返回新的 `operation_id` 和 generation；不修改基础 Registry，下一次动态视图能看到
   `demo_add` 并执行 `2 + 3 = 5`。
3. `/plugins stop devflow-math` 先从动态视图撤掉路由并返回 `operation_id`；已经开始的调用
   通过 lease 继续，停用后的新派发返回 `TOOL_VERSION_RETIRED` 或明确的 not-found 错误。
4. 阻塞工具测试证明 stop 不取消已有调用：释放 lease 后 operation 进入 `inactive`。
5. 两个 session 的登记和路由相互隔离；没有显式授权或 Profile 未开启时，返回原始基础 Registry，
   动态工具不会因为历史消息或模型输出自行出现。

## 当前边界

- H2 的注册入口是宿主代码/测试使用的 `register_version()`；模型自动生成、试用和晋升留给 H5/H6。
- manager 状态当前在进程内，重启后的恢复和持久工件引用留给 H3。
- H2 的测试验证同一进程和同一会话对象不重建；浏览器真实长任务、60 秒并行任务和视频证据属于 H10，
  不能把本文件的单元/集成测试计数写成网页实测。
- 只接受 `read_only`、非 exclusive、concurrency-safe 工具；写入、服务、Loop 和 FC proxy 均不会
  通过 H2 动态加载。

## H2 状态

代码和测试候选已完成；初始候选发现的问题已集中修复，并通过同一审查员的 Standards / Spec
增量复核。H2 已完成，下一节点是 H3。
