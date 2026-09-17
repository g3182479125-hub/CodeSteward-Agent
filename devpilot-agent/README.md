# DevPilot Agent

个人研发 Agent：以 nanobot 为主 Runtime，复用其聊天、定时任务、记忆和 Skills；DevPilot 补充受控代码交付与验证。后续能力以当前计划为准，不把计划设计当作已实现功能。

## 冻结架构

每次开发及修复前必读 [ADR-0004：批量修复与增量复审](docs/adr/ADR-0004-batched-review-and-incremental-recheck.md)。
它是冻结计划的流程修订：集中修改和预检、并行双轴审查、按影响范围增量复审，
两轴明确通过后才能完成；原冻结计划与现有安全门禁继续有效。

后续模型必须读取 [CURRENT_PLAN 3.3](docs/plans/CURRENT_PLAN.md)：第 0 节接手步骤、
第 5/6 节全部剩余节点的实施与验收、第 7 节开发和 Review 规则。详细设计不是
已实现状态；已完成 P0–P5 不重做。

P8 的必读实施规格：[Skill 持续改进](docs/plans/SKILL_IMPROVEMENT.md)；
本次范围修订见 [ADR-0007](docs/adr/ADR-0007-controlled-skill-improvement.md)。

- 主 Runtime：nanobot（Python）
- 插件与组合设计参考：DeepSeek Harness
- 权限、Sandbox、审批和执行协议参考：Codex Harness
- 产品边界：个人研发自动化，不重写三套 Runtime

## 当前状态

截至 2026-09-17，P0–P8 与 H1–H11 已按当前冻结计划完成，并保留各节点的 evidence、
测试和 Standards/Spec 双轴 review。当前不应再依据旧 README 中“只完成 H1–H4”或“H6–H10
尚未开始”的历史描述判断项目状态；详细事实以 [当前精简执行计划](docs/plans/CURRENT_PLAN.md)
和[最终换机交接](docs/handoffs/2026-09-17-final-cross-machine-handoff.md)为准。

最近一批维护修复了工具失败后的用户体验：普通聊天、只读问题、状态/恢复查询可以继续；
旧未知副作用调用仍禁止重放，审批、执行、回滚门禁不变。当前项目仍以 nanobot 为唯一主
Runtime，DevPilot 提供受控代码交付与验证，FC 需要每台电脑自行配置并复验。

新开发者接手时，先完整阅读当前精简执行计划、ADR-0004、相关 Skill、
[Harness 借鉴落点](docs/plans/HARNESS_ADOPTION.md)和[最终换机交接](docs/handoffs/2026-09-17-final-cross-machine-handoff.md)。

## 本地目录约定

nanobot 上游源码独立放在 `../nanobot-inspect/nanobot-main`，DeepSeek Harness 和 Codex Harness 仅作为只读参考资料。DevPilot 的运行数据放在 `local-data/`，该目录不会提交到 Git。

## P0 自检

在 Windows PowerShell 中运行：

```powershell
python .\scripts\doctor.py --config .\config\inspect.example.json
```

自检不会打印 API Key，也不会启动服务、安装依赖或修改上游源码；首次运行可能创建 DevPilot 的 `local-data/` 写入测试目录。

## P1 仓库分析闭环

nanobot 的 Tool entry point 提供 `devpilot_inspect_project`、
`devpilot_publish_artifact`、`devpilot_search_code`、`devpilot_read_code`、
`devpilot_propose_changeset`、`devpilot_feature_delivery_propose`、
`devpilot_request_approval`、`devpilot_get_changeset` 和 `devpilot_get_run_state`。运行 nanobot
前设置受控项目根和 DevPilot 数据目录：

```powershell
$env:DEVPILOT_ALLOWED_ROOTS = "D:\path\to\allowed-projects"
$env:DEVPILOT_DATA_DIR = "D:\path\to\devpilot-agent\local-data"
python -m nanobot gateway --config "D:\path\to\nanobot\local-state\config.json"
```

然后在 nanobot 对话中要求“使用 repo-analysis Skill 分析项目”。项目路径必须位于
`DEVPILOT_ALLOWED_ROOTS` 的真实路径内；报告只写入 DevPilot 管理的 Artifact 目录。
首次 inspect 返回的 `project_id` 用于后续 search/read；这两个 Tool 只接受已登记项目，
并限制文件数、文件大小、结果数和读取行数。

Skill 发布只接受本地 catalog 中经过校验、人工审核并启用的版本：

```powershell
uv run python scripts\publish_skill.py `
  --skill skills\catalog\repo-analysis\SKILL.md `
  --workspace "D:\path\to\nanobot\local-state\workspace"
```

## 安全声明

Windows 原生环境下的 workspace 限制和危险命令检查不等于 OS Sandbox。FC Agent Sandbox
适配器只接受显式文件包和显式命令，运行结束后销毁远程 Sandbox，不直接写回本地项目；
它不是本机隔离的替代品，也不能作为执行授权本身。已有 `sandboxed-demo` 配置仅是 P0
诊断占位，能力不满足时，R2 及以上执行必须关闭。

P2B/P4 的 proposal Tool 只创建预览，不能批准、Apply 或 Rollback，也不会接收审批令牌。
提案完成后，nanobot 在对话中展示 Diff 并等待用户明确回复“执行”，然后调用
`devpilot_request_approval` 登记审批意图；控制面用户动作才会签发一次性 token 并执行
Apply/Verify/Rollback。独立审批页面已移除，不再作为运行依赖。
控制面仍默认关闭；只有开发者明确设置
`DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE=1`、长度至少 32 字节的
`DEVPILOT_CONTROL_PLANE_TOKEN` 且数据库已执行 `alembic upgrade head` 后才可用于本机测试。
该 token 只作为 nanobot 适配层与控制面之间的本地进程认证，不进入模型上下文；集成启动脚本
会在当前 PowerShell 进程中自动生成临时值。它不是 OS Sandbox，不得处理真实生产仓库。

当前生效的前端范围修订见
[ADR-0003：统一使用 nanobot 原生前端](docs/adr/ADR-0003-nanobot-only-frontend.md)。
独立 React/Vite 控制台已删除；FastAPI、持久事件和内部 SSE 仅服务 nanobot 适配层。

FC Sandbox 的可选依赖和真实连通性检查见
[FC Sandbox runbook](docs/runbooks/local-development.md#fc-agent-sandbox-optional)。

nanobot 不需要额外的 DevPilot 审批页面或前端补丁；只需安装 DevPilot 的 nanobot Tool entry points。
首次使用项目时，控制面必须通过 `POST /v1/projects/{project_id}/trust` 并提交
`{"confirmation":"trust"}` 显式登记项目；项目指纹或 allowed-root 变化后会自动拒绝 R1/R2。

## 阶段门禁

开发以仓库内 [CURRENT_PLAN](docs/plans/CURRENT_PLAN.md) 为当前入口，效力见
[ADR-0006](docs/adr/ADR-0006-nanobot-first-personal-agent.md)。仓库外冻结计划和旧交接保留为
历史来源；P1 证据见 [P1 acceptance](docs/evidence/phase-1/p1-acceptance.md)。
