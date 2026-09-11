# H3：插件版本更新与完整资源清理验收证据

## 候选与边界

- 最终候选：`b0d8d89`
- H3 实现基线：`409eef8`
- 运行时仍是 nanobot 单 Loop；没有新增 WebUI、第二套运行时或第二个生产数据库。
- DevPilot 已配置时，插件状态写入现有 `DEVPILOT_DATA_DIR/devpilot.db` 或
  `DEVPILOT_DATABASE_URL` 指向的 SQLite 数据库；Alembic 迁移为
  `0011_p8_skill_improvement_records -> 0012_h3_plugin_runtime_state`。
- 未配置 DevPilot 时，AgentLoop 不创建 `.nanobot/plugins.db`；插件管理保持进程内状态。

## 验收结果

| 条款 | 实际验证 | 结果 |
|---|---|---|
| H3-A 版本切换与排空 | `test_update_switches_route_and_drains_the_old_generation`：旧调用返回 v1，新调用返回 v2，旧资源清理完成 | 通过 |
| H3-B 反复装卸无资源残留 | `test_resource_cleanup_returns_to_baseline_after_repeated_stop`：50 次循环，监听器、任务、服务计数回到 0 | 通过 |
| H3-C 失败可区分、可恢复 | 语法失败、激活失败、退休清理失败、初始化清理失败分别有测试；清理失败由 `cleanup-retry` 接续 | 通过 |
| H3-D 并发管理 | `test_concurrent_updates_commit_only_one_generation`：并发更新仅一个代次提交，另一请求返回冲突 | 通过 |
| H3-E 重启恢复 | `test_persisted_retirement_can_be_reconciled_after_restart`：持久化清理引用可由新 Manager 接续核对 | 通过 |
| H3-F 准备阶段隔离 | `test_preparing_context_does_not_open_resources_before_activation`：激活前不注册监听、不运行任务 | 通过 |
| H3-G 停用/移除边界 | 生命周期示例验证插件任务和监听器随 stop 清理；inactive 版本可 remove，active/排空引用会拒绝 remove | 通过 |

## 执行记录

在 `nanobot` 目录执行：

```powershell
$env:DEVPILOT_DATA_DIR = $null
$env:DEVPILOT_DATABASE_URL = $null
uv run pytest tests/agent/test_plugin_h3.py tests/agent/test_plugin_manager.py tests/agent/test_plugin_contracts.py tests/command/test_plugin_commands.py tests/command/test_model_command.py tests/command/test_router_dispatchable.py -q
```

结果：`80 passed`。

静态检查：

```powershell
uv run ruff check nanobot/agent/plugins nanobot/command/plugin_commands.py nanobot/command/builtin.py
uv run basedpyright nanobot/agent/plugins
```

结果：Ruff 通过，`basedpyright 0 errors, 0 warnings, 0 notes`。

在 `devpilot-agent` 目录执行迁移/兼容验证：

```powershell
$env:DEVPILOT_DATABASE_URL = "sqlite:///local-data/devpilot.db"
uv run alembic upgrade head
uv run alembic current
```

结果：数据库为 `0012_h3_plugin_runtime_state (head)`，`plugin_runtime_state` 表存在；旧
`0011` 数据库也由兼容测试验证可安全写入，后续 Alembic 升级使用幂等建表。

跨项目回归（排除既有的 `tests/agent/test_context_prompt_cache.py` 基线失败项）：
`5908 passed, 42 skipped`。DevPilot P2A/P8-01/P8-02/P8-03 回归：`31 passed`。

最终审查前补充的代码门禁：`basedpyright nanobot/command/plugin_commands.py` 为 0 errors；
`update/stop` 的持久化顺序已固定为“先建立 retired operation 关联，再提交路由状态”，避免
进程中断留下无法重试的排空引用。

## 已知说明

- `devpilot-agent/tests/fixtures/node-project/src/index.js` 是用户未提交的 fixture 改动，未纳入本次提交。
- 测试期间生成的 `nanobot/MagicMock/` 未纳入提交，需继续按用户工作区改动处理。
- 本证据只覆盖 H3 本地契约与持久化/恢复语义，不把 FC 真实云端或网络隔离写成 H3 结论。
