# P7-01 任务组合与上下文验收

状态：**实现候选已完成；等待本节点 Standards/Spec 双轴 review。**

本节点只复用 nanobot 原有 Runtime、Skill、Tool registry、Session 和上下文入口，
没有新建页面、数据库、插件市场或第二套 Agent Loop。nanobot 原生 WebUI 仍是唯一
用户入口；任务选择通过原生聊天命令 `/task-profile` 完成。

## 实际落点

| 要求 | 实际实现 | 边界 |
|---|---|---|
| 三类任务组合 | `repo-analysis`、`feature-delivery`、`bug-fix` 静态受信 Profile | Profile 由开发者代码定义，模型不能新增或扩大工具 |
| Skill/Tool 依赖校验 | `TaskProfileCatalog.validate_runtime` | 缺 Skill 或工具时明确返回 `task profile ... unavailable`，不自动安装、不静默放宽 |
| 会话隔离 | `SessionPolicy.allowed_tools` + `ToolRegistry.restricted_to` | 只影响绑定该 Profile 的会话；未列出的工具在可见列表和执行入口都不可用 |
| 配置版本 | Session metadata 保存 `task_profile` 与 `task_profile_version` | 版本不匹配即停止，不能从旧摘要重放动作 |
| 上下文契约 | ContextBuilder 注入 `profile@version`、要求输出字段和权限警告 | 不注入凭据、审批令牌或执行权 |
| 错误循环控制 | `TaskLoopGuard`：相同错误 3 次暂停，最多 1 个诊断分支 | 成功工具结果只清除连续错误计数，不重置诊断分支预算 |
| Skill 定制 | 更新现有 `feature-delivery` Skill | 未新增第四条工作流，仍沿用 DevPilot 审批/Apply/验证/回滚边界 |

## 验收与测试证据

基线：P6 结项提交 `e8369151cba046007cc05621756ab702a1ec99f8`。实现候选固定前，保留
用户已有的两个 Node fixture 未提交修改，不将其当作 P7 实现文件。

已通过：

- `uv run --no-sync ruff check nanobot tests`；
- nanobot 全量：`5856 passed, 42 skipped`；
- P7 专项与受影响回归：`95 passed`，另补充全量回归通过；
- 使用同仓库 `devpilot-agent/src` 加入 `PYTHONPATH` 后，两个跨包 DevPilot 集成测试
  通过；这只是测试环境拼接，不改变运行时架构；
- `compileall`/Ruff 作为实现检查通过。

基于当前网络环境，`uv sync --extra dev --locked` 无法从 PyPI 下载缺失的
`basedpyright`（TLS handshake EOF），因此本次没有宣称类型检查通过；现有测试与 Ruff
证据不替代后续换机后的类型检查。

## 反向用例覆盖

- 三类 Profile 的工具集合互相区分，分析 Profile 不暴露代码修改/执行工具；
- 缺少工具或 Skill 时拒绝启用；
- 直接点名未列入 Profile 的工具在受限 registry 中返回 not found；
- 同一 Profile 只绑定一个 Session，另一个 Session 不继承；
- Profile 版本变化或元数据陈旧时停止；
- 连续相同工具错误第三次暂停；成功证据会清除连续错误计数；
- 上下文中保留 Profile、输出字段和权限边界，不产生独立前端页面。

## 未做与不做

- 不实现通用插件安装/市场、运行时热卸载或动态下载；
- 不新增独立控制台或任务管理页面；
- 不把 Profile 当作审批、Apply、验证、回滚或沙箱授权；
- 不改写 nanobot Agent Loop，不引入向量数据库或新的记忆服务；
- 不提前进入 P7-02 的基线对照评测。

双轴 review 通过并记录后，P7-01 才能标记 complete；若 reviewer 发现问题，只批量
修复本节点范围，再对原候选做增量复核。
