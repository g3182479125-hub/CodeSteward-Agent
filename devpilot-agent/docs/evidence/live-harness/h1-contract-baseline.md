# H1：插件契约与兼容基线

状态：COMPLETE（代码、验收和本节点双轴审查已完成）

## 固定点和范围

- 基线：`2368a19d53672ba9ad0edfa2f20e9c4bd6b6990d`
- 节点：`LIVE_HARNESS_EXTENSION.md` 的 H1
- 本批次目标：固定 manifest、作用域、版本、错误结果和资源归属接口；提供只读
  catalog；不接入运行中加载，不修改 Agent Loop、ToolLoader、ToolRegistry、会话或数据库。
- 保留行为：旧的 `ToolLoader` 启动时 package scan/entry-point discovery、任务 Profile
  白名单和既有 greeting entry point 不变。

## 实际入口核对

| 事实 | 现有实现 | H1 处理 |
|---|---|---|
| 工具发现 | `nanobot/nanobot/agent/tools/loader.py` 的 `discover()` / `_discover_plugins()` | 保持旧路径；新 manifest 不会被自动导入 |
| 工具派发 | `nanobot/nanobot/agent/runner.py` 的 `prepare_call` 路径 | 未改动；租约和在线路由留给 H2 |
| 任务视图 | `nanobot/nanobot/agent/task_profiles.py` 与 `ToolRegistry.restricted_to()` | 未改动；动态 capability 组合留给 H2 |
| 会话归属 | `nanobot/nanobot/session/` | H1 catalog 只做内存级 session/project 校验，不建第二存储 |

## 交付内容

- `nanobot/nanobot/agent/plugins/contracts.py`：不可变 `PluginManifest`，支持 H1 规定的
  11 个字段；校验未知字段、枚举、版本/哈希、相对 entry 和 artifact 越界；定义生命周期、
  来源、错误码及机器可读 `PluginOperationResult`。
- `nanobot/nanobot/agent/plugins/context.py`：窄的 `PluginContext` 协议，约定 tool、事件、
  task、service 和 disposer 的资源归属；当前没有实现，也不会产生后台副作用。
- `nanobot/nanobot/agent/plugins/catalog.py`：按 session/project 隔离的注册元数据和只读
  `list_view()` / `status()`；拒绝核心 capability 覆盖、重复版本、作用域错配和非开发者
  global 插件。它不持有 live Tool 对象。
- `devpilot-agent/examples/pluggable-tool/plugin-manifest*.json`：现有 greeting 与 addition
  两个独立只读工具实现的 manifest 示例；旧 greeting entry point 仍按兼容路径加载。

## H1 验收

| 验收 | 实际结果 |
|---|---|
| H1-A：旧 Profile/entry-point 行为 | `13 passed`（旧 loader entry-point、scope、task profile） |
| H1-B：无效 manifest、越界/NUL entry、同名/重复版本、候选状态分离 | 10 个专项用例覆盖；均返回明确错误码 |
| H1-C：两个 session 列举不串用 | `test_catalog_isolates_sessions_and_exposes_read_only_status` 通过 |
| 示例 manifest | 用实际 `PluginManifest.from_mapping()` 解析通过；entry hash 与示例源码一致 |

## 双轴审查

- Standards：PASS，报告见 `devpilot-agent/docs/reviews/h1-standards-review-2930119.md`
- Spec：PASS，报告见 `devpilot-agent/docs/reviews/h1-spec-review-2930119.md`
- 两轴均复核固定候选 `2930119`；H1 无待修复项。

## 可复现检查

工作目录：`E:\codex-local\nanobot-devpilot-agent\nanobot`；Python 环境：项目内
`.venv`（Python 3.12.6）。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/agent/test_plugin_contracts.py -q --basetemp E:\codex-local\nanobot-devpilot-agent\nanobot\pytest-h1-20260910c
# 10 passed

.\.venv\Scripts\python.exe -m pytest tests/agent/test_tool_loader_entrypoints.py tests/agent/test_tool_loader_scopes.py tests/agent/test_task_profiles.py -q --basetemp E:\codex-local\nanobot-devpilot-agent\nanobot\pytest-h1-compat-20260910
# 13 passed

.\.venv\Scripts\ruff.exe check nanobot/agent/plugins tests/agent/test_plugin_contracts.py
# All checks passed

.\.venv\Scripts\basedpyright.exe nanobot/agent/plugins
# 0 errors, 0 warnings, 0 notes
```

## 明确未完成

H1 不声称已经支持无需重启的 load/stop、版本更新、事件清理、模型生成插件、FC
试运行或 Loop 切换；这些分别属于 H2–H8。下一步只能进入 H2，先把这里的契约接到
受信 manager/routing 和原生命令，并继续保留默认关闭路径。
