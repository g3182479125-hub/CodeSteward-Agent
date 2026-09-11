# H4：有限依赖和能力组合验收证据

## 候选与边界

- 实现候选：`78456c6`
- H4 实现基线：`7e495a3`
- 运行时仍为 nanobot 单 Runtime；未新增 WebUI、第二套 Agent Loop 或跨进程服务网格。
- 本节点只实现版本化 `requires/provides`、作用域内服务绑定、有限依赖图、等待/恢复和
  提供者更新协调；不实现复杂版本求解、负载均衡或跨项目依赖。
- 依赖接口必须使用 `namespace:name@version` 格式；同一作用域内同一接口只允许一个提供者。

## 验收结果

| 条款 | 实际验证 | 结果 |
|---|---|---|
| H4-A 先消费者后提供者 | 消费者首次加载进入 `waiting`；提供者随后加载后消费者自动激活并可执行 | 通过 |
| H4-B 提供者停止与恢复 | 停止提供者时递归暂停受影响消费者；提供者恢复后按依赖顺序重新激活 | 通过 |
| H4-B 受影响子图 | 根提供者停止会暂停中间层和叶子消费者，恢复后叶子可执行 | 通过 |
| H4-B 提供者替换 | 更新服务提供者时切换作用域绑定，消费者无需重启即可读取新版本 | 通过 |
| H4-B 整组租约 | 更新等待提供者、消费者及其依赖组的在途调用归零；旧组新调用和新依赖加载在切换期间被阻断 | 通过 |
| H4-C 循环依赖 | A↔B 在任何路由激活前返回 `PLUGIN_DEPENDENCY_CYCLE`，不留下空路由 | 通过 |
| H4-D 作用域隔离 | 不同项目可以使用同名接口且绑定不同服务值，互不影响 | 通过 |
| H4-E 接口兼容性 | 未满足精确版本的提供者不会激活消费者，状态保持 `waiting` | 通过 |
| 冲突与破坏性更新 | 同作用域重复提供同一接口被拒绝；移除活跃消费者所需接口的更新被拒绝并保留旧组合 | 通过 |

## 代码与机制核对

- `PluginManifest` 校验版本化接口标识，并区分工具标识与服务接口。
- `PluginManager` 按 session/project 作用域维护服务注册表，依赖图只在当前作用域解析。
- 缺依赖以结构化 `PLUGIN_DEPENDENCY_UNAVAILABLE` 暴露，不无限重试；依赖恢复后自动激活等待项。
- 提供者更新前预留受影响依赖组，阻断旧路由新调用和切换期间的新加载；整组租约归零后
  原子替换服务绑定，再退休旧路由。
- 失败时保留旧组合；依赖切换、循环、冲突和不可用均有独立错误码。

## 执行记录

在 `nanobot` 目录执行：

```powershell
$env:DEVPILOT_DATA_DIR = $null
$env:DEVPILOT_DATABASE_URL = $null
uv run pytest tests/agent/test_plugin_h4.py -q
```

结果：`11 passed`。

H4 与 H1–H3、插件管理、契约和命令回归一起执行，结果：`52 passed`。

全量回归（排除既有基线项 `tests/agent/test_context_prompt_cache.py`）执行：

```powershell
$env:DEVPILOT_DATA_DIR = $null
$env:DEVPILOT_DATABASE_URL = $null
uv run pytest -q --ignore=tests/agent/test_context_prompt_cache.py
```

结果：`5924 passed, 42 skipped`。

静态检查：

```powershell
uv run ruff check nanobot/agent/plugins nanobot/command/plugin_commands.py nanobot/command/builtin.py
uv run basedpyright nanobot/agent/plugins
git diff --check
```

结果：Ruff 通过；`basedpyright 0 errors, 0 warnings, 0 notes`；差异检查无内容错误。

## 审查门禁

最终候选 `78456c6` 已由同一批独立 Standards / Spec reviewer 增量复核，结论均为 `PASS`：

- `docs/reviews/h4-standards-review-78456c6.md`
- `docs/reviews/h4-spec-review-78456c6.md`

复核覆盖了前序发现的作用域服务绑定、版本化接口、提供者更新原子切换、整组租约以及
切换窗口内新加载竞态；未覆盖的跨进程 Runtime、网络隔离和复杂依赖求解不属于 H4 范围。

## 已知说明

- `devpilot-agent/tests/fixtures/node-project/src/index.js` 是用户未提交的 fixture 改动，未纳入本节点提交。
- 测试生成的 `nanobot/MagicMock/` 未纳入提交，继续按用户工作区内容保留。
- 本证据只证明本地契约、作用域和生命周期协调，不把 FC 真实云端能力或网络隔离写成 H4 结论。
