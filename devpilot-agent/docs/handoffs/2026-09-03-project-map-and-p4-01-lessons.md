# DevPilot 项目交接与 P4-01 问题复盘

日期：2026-09-03  
当前证据 checkout：`59329c65066f02067d7546c95c85342ca188aa7f`  
P4-01 实现评审目标：`2958ae35bb534197095bdef22e5b94a7b6c0de65`  
P4-01 状态：已通过独立只读 Review，P4-02 已解锁。  
当前分支：`main`，工作树应保持 clean。

## 给后续 AI 的第一步

这是本项目的入口交接文档。接手时按下面顺序读取：

1. 本文，了解问题复盘、架构边界、模块地图和当前状态；
2. 根目录 `README.md`，了解本地启动、安全声明和 nanobot 集成方式；
3. 冻结计划 `E:/codex-local/plans/DEVPILOT_FROZEN_PLAN.md`，确认当前节点目标和禁止事项；
4. 当前节点证据 `docs/evidence/phase-4/manifest.json` 与
   `docs/evidence/phase-4/p4-01-acceptance.md`；
5. 只有在需要查历史争议时，才读取 `docs/reviews/` 中带有旧日期或旧版本的报告。

历史 Review 报告可能保留修复前的 `blocked` 或 `request-changes` 结论，不能覆盖本文、
当前 manifest 和最终独立 Review 的状态。当前最终 Review 见
`docs/reviews/p4-01-independent-review-final-20260902.md`。

## 项目定位

这是一个以 nanobot 为主 Runtime 的个人研发自动化 Agent。DevPilot 是 nanobot 外部的
Python 业务层，负责受限项目分析、结构化代码变更、审批、验证和 Artifact 记录。

- nanobot：对话、Runtime、Tool 加载和用户确认入口；
- DevPilot：确定性的路径、补丁、状态、审批、验证和证据逻辑；
- DeepSeek Harness：插件组合和运行方式的参考；
- Codex Harness：审批边界、结构化变更、执行端口和安全设计的参考；
- FC Sandbox：可选执行适配器，不等于本机 OS Sandbox；P6 未通过前不能声称真实隔离或
  自动修复已启用。

禁止把 Codex Harness、DeepSeek Harness 或 nanobot 源码 Runtime 混合重写。本项目的主线
是“nanobot 对话 + DevPilot 确定性控制面”。

## P4-01 最初卡点与解决方案

### 1. nanobot 直接暴露 capability token

最初 nanobot 注册了 `devpilot_execute_changeset`、`devpilot_verify_changeset` 和
`devpilot_rollback_changeset`，这些 Tool 接收外部审批 token。这样模型通道可能接触或
转发真正的执行凭证，违反“模型只能提出方案，控制面才执行副作用”的边界。

解决方式：

- 从 `pyproject.toml` 的 nanobot entry point 移除三个直接执行入口；
- 保留源码中的旧类仅用于历史单元测试兼容，并明确标记为 legacy/test-only；
- 新增 `devpilot_request_approval`，只传递用户确认的审批意图，不接收、不签发、不返回 token；
- Apply、Verify、Rollback 仍由本地控制面和外部用户动作完成。

定位文件：`pyproject.toml`、`skills/catalog/feature-delivery/SKILL.md`、
`src/devpilot/nanobot_tools.py`、`src/devpilot/control_plane.py`。

### 2. 项目 trust gate 可以被临时管理器绕过

第一次修复加入了 `trust_state`、仓库指纹和 allowed-root 绑定，但 Review 发现：
`ControlPlaneManager(store=None)` 时，`_require_trusted_project()` 直接 return。
这会让没有持久化项目记录的实例继续尝试审批或执行。

解决方式：

- `store=None` 只允许生成 Preview；
- `request_approval`、`approve`、`apply`、`verify`、`rollback` 统一返回
  `PERSISTENCE_REQUIRED`；
- 持久化项目必须先显式 trust；
- 每次 R1/R2 操作重新计算 `repo_fingerprint` 和 `allowed_root_id`，任一不匹配都拒绝；
- 回归测试断言五个入口都 fail-closed，且不会写入项目文件。

定位文件：`src/devpilot/control_plane.py:598`、`src/devpilot/storage.py:256`、
`src/devpilot/storage.py:282`、`src/devpilot/paths.py:63`、
`tests/test_p2b_control_plane.py`。

### 3. 证据 manifest 与实际实现提交不一致

Review 期间出现过 manifest 绑定旧实现提交、当前 HEAD 又包含后续文档提交的情况。
这不一定改变运行行为，但会使审查者无法确定测试和 evidence 对应哪一版代码。

解决方式：

- 实现评审目标固定为代码提交 `2958ae3`；
- 后续只修改 evidence 的文档提交不混入实现评审目标；
- manifest 明确记录 `implementation_commit`、`review_target` 和
  `review_target_note`；
- acceptance、manifest、最终独立 Review 和 Artifact SHA-256 互相核对。

定位文件：`docs/evidence/phase-4/manifest.json`、
`docs/evidence/phase-4/p4-01-acceptance.md`、
`docs/reviews/p4-01-independent-review-final-20260902.md`。

### 4. 外部数据库环境变量污染测试

`migrations/env.py` 原来会无条件使用 `DEVPILOT_DATABASE_URL` 覆盖 Alembic 配置。
当开发机已有该变量时，测试临时库会被指向错误数据库，出现失败；手动清除变量后才通过。

解决方式：

- 新增 `src/devpilot/migration_config.py`；
- 测试或调用方显式传入的数据库 URL 优先；
- 只有 Alembic 默认占位 URL 才读取 `DEVPILOT_DATABASE_URL`；
- 用错误的外部数据库变量运行全量测试，实际结果仍为 `124 passed, 3 skipped`。

定位文件：`src/devpilot/migration_config.py`、`migrations/env.py`、
`tests/test_p2a_migration.py`。

## 当前主链

```text
nanobot 对话
  -> DevPilot R0 Tool（inspect/search/read）
  -> proposal / immutable Preview / Diff
  -> nanobot 展示风险并等待用户确认
  -> request_approval（只记录确认意图，不带 token）
  -> 控制面用户动作签发一次性 capability token
  -> Apply（结构化 FilePatch + 前置 hash）
  -> Verify（服务端 allowlist；P6 前仅 Fake）
  -> server-owned Artifact + Run/Event
  -> 用户明确选择 Rollback 或接受结果
```

任何代码修改都必须经过 ChangeSet、审批、前置 hash 和控制面状态机；不能调用任意 Shell、
模型提供 command、自动安装依赖或绕过 nanobot Tool 直接写项目。

## 模块地图：需要改哪里就从这里定位

| 模块 | 文件 | 责任 | 何时读取 |
|---|---|---|---|
| CLI/服务启动 | `src/devpilot/cli.py` | `devpilot serve`、诊断和本地入口 | 启动服务或改端口时 |
| 配置 | `src/devpilot/config.py` | 环境变量与运行配置 | 改配置、检查默认值时 |
| 路径安全 | `src/devpilot/paths.py` | 真实路径、allowed roots、root id、仓库指纹 | 任何文件访问或项目绑定前 |
| 只读代码访问 | `src/devpilot/code_access.py` | search/read、秘密/二进制/预算边界 | 实现 R0 工具时 |
| 仓库分析 | `src/devpilot/inspector.py`、`services.py` | 项目登记、分析报告、事实证据 | repo-analysis 或收集证据时 |
| 领域状态 | `src/devpilot/domain/entities.py`、`enums.py`、`state_machine.py` | Task/Plan/ChangeSet 状态和迁移守卫 | 改生命周期或错误状态时 |
| 补丁引擎 | `src/devpilot/patches.py` | FilePatch、Diff、原子 Apply、Rollback、hash | 改代码写入或回滚时 |
| 控制面 | `src/devpilot/control_plane.py` | Preview、审批、幂等、Apply、Verify、Rollback、FastAPI 路由 | 改副作用或审批边界时 |
| 持久化 | `src/devpilot/storage.py` | SQLite Project/Run/Event/Artifact/Approval 记录 | 改恢复、信任或数据库时 |
| 迁移配置 | `src/devpilot/migration_config.py`、`migrations/env.py` | Alembic 数据库 URL 解析 | 改迁移或测试数据库时 |
| 执行端口 | `src/devpilot/execution.py` | ExecutionRequest/Result、Fake 后端、能力边界 | 改验证后端时 |
| 验证策略 | `src/devpilot/verification.py` | 允许的 check/profile 和请求构造 | 新增验证类型时 |
| FC 适配器 | `src/devpilot/fc_sandbox.py` | 可选远程 FC/E2B ExecutionBackend | 只有 P6 方案明确后 |
| Artifact | `src/devpilot/artifacts.py`、`report.py` | 服务端报告、脱敏、hash 和持久化元数据 | 改交付报告时 |
| 工作流 | `src/devpilot/workflows.py` | 当前 FeatureDelivery 编排和结果报告 | 新增 feature/bug/test workflow 时 |
| nanobot 适配 | `src/devpilot/nanobot_tools.py` | Tool schema、HTTP 调用、错误映射 | 改 nanobot 可见能力时 |
| Skill 管理 | `src/devpilot/skill_manager.py`、`skills/catalog/` | catalog/validate/reviewed/enabled 生命周期 | 新增或发布 Skill 时 |
| 数据库迁移 | `migrations/versions/` | 0001–0007 schema 演进 | 改持久化模型时 |
| 单元/集成测试 | `tests/` | P0–P4 安全、状态、工具、Golden 和迁移测试 | 每次实现修改后 |

## 当前关键契约

- R0：inspect/search/read，只读；项目路径必须位于服务端配置的 allowed roots。
- R1：Apply/Rollback，必须有持久化项目、显式 trust、匹配指纹/root、有效一次性审批和
  前置 hash。
- R2：Verify，必须有独立验证审批；P6 前自动工作流只接受精确类型的
  `FakeExecutionBackend`。
- Tool：模型只能提出方案、读取状态和请求确认；不能传 capability token。
- Artifact：写到服务端 data 目录，只保存安全摘要和元数据，不保存完整 stdout/stderr、
  API key 或完整环境。
- 数据库：生产服务启动必须通过 Alembic head `0007_project_trust` 检查。
- 前端：当前主流程使用 nanobot 自带前端/对话确认，不依赖独立审批页面或 AG-UI 页面。

## 当前验证结果

在 E 盘临时目录运行过以下检查：

- 全量 pytest：`124 passed, 3 skipped`；
- 即使设置错误 `DEVPILOT_DATABASE_URL`，仍为 `124 passed, 3 skipped`；
- Ruff、Pyright、uv lock、Secret scan、Markdown/link check、Alembic upgrade、
  `git diff --check`：通过；
- nanobot 实际 DevPilot entrypoint：9 个，和 `feature-delivery` Skill allowlist 精确一致；
- P4-01 最终独立 Review：`approve`，无 blocker/major。

三个 skip 是 Windows 符号链接测试需要 `SeCreateSymbolicLinkPrivilege`，不是业务失败。

## P4-02 起点

冻结计划定义：`P4-02 = bug-fix Skill/Workflow 与 G-02`。G-02 的目标是 React 表单缺陷
修复，必须证明：

1. 先通过 inspect/search/read 收集可追溯的失败现象；
2. 只创建最小修复计划和批准文件范围内的 ChangeSet；
3. 保留失败证据以及修复前后的 hash；
4. 通过用户确认后才能 Apply；
5. 失败时不能宣称修复成功，必须保留状态和证据；
6. 在 P6 前不得把真实自动修复或真实 Sandbox 执行包装成已完成。

P4-02 已建立最小 React 表单 fixture，位置为 `tests/fixtures/react-form-project`；设计和后续
实施顺序见 `docs/superpowers/specs/2026-09-03-p4-02-bug-fix-design.md`。当前只完成 fixture
与 Skill 草案，`BugFixWorkflow`、G-02 Golden 和独立 Review 仍待完成。不要把 P4-02 扩大成
React 控制台、AG-UI 接入或真实 Sandbox。

## 对后续 AI 的工作规则

- 先读本文和冻结计划，再改代码；
- 先检查 `git status` 和当前 commit，不覆盖用户未提交修改；
- 不向 GitHub、远程 Sandbox 或其他外部服务上传 key、`.env`、SQLite、日志或会话数据；
- 本项目路径在 `E:\codex-local\nanobot-devpilot-agent`，临时测试目录优先使用
  `E:\codex-local\pytest-temp`，不要把依赖或数据安装到 C 盘；
- 对任何写入、执行、审批、回滚相关修改，都补反向安全测试；
- 看到旧 Review 的 `blocked` 结论时，先核对它对应的 commit，不能直接当作当前状态；
- 任何“已通过”“已沙箱化”“已自动修复”的表述，都必须有当前代码和机器可读 evidence 支撑。
