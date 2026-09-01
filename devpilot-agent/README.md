# DevPilot Agent

个人研发自动化 Agent：以 nanobot 作为主 Runtime，在其外部增加项目分析、Git 总结、测试报告、定时任务和可追踪执行能力。

## 冻结架构

- 主 Runtime：nanobot（Python）
- 插件与组合设计参考：DeepSeek Harness
- 权限、Sandbox、审批和执行协议参考：Codex Harness
- 产品边界：个人研发自动化，不重写三套 Runtime

## 当前状态

项目已完成 P0/P1、P2A 和 P2B：提供受限代码访问、结构化 ChangeSet、人工审批、
Apply、Rollback 和持久化恢复。当前还没有把仓库代码交给本机任意 Shell 执行；
FC Agent Sandbox 的可选执行端口已实现，但云端连通性必须由用户配置凭据后单独验证。
项目不改写 nanobot 的 Runtime、权限和执行边界；当前主流程通过 nanobot Tool 和对话确认完成
Changeset 交付。独立审批 WebUI 已移除，主流程只通过 nanobot 对话完成确认和执行。

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
`devpilot_propose_changeset`、`devpilot_execute_changeset` 和
`devpilot_rollback_changeset`。运行 nanobot
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

P2B 的 proposal Tool 只创建预览，不能批准、apply 或 rollback，也不会接收审批令牌。
提案完成后，nanobot 必须在对话中展示 Diff 并等待用户明确回复“执行”；随后
`devpilot_execute_changeset` 才会完成 Apply 和 FC Sandbox 验证。回滚同样必须在对话中
再次明确确认。原有控制面 API 保留作为内部执行接口，独立审批页面已移除，不再作为运行依赖。
控制面仍默认关闭；只有开发者明确设置
`DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE=1` 且数据库已执行 `alembic upgrade head`
后才可用于本机测试。它不是 OS Sandbox，不得处理真实生产仓库。

FC Sandbox 的可选依赖和真实连通性检查见
[FC Sandbox runbook](docs/runbooks/local-development.md#fc-agent-sandbox-optional)。

nanobot 不需要额外的 DevPilot 审批页面或前端补丁；只需安装 DevPilot 的 nanobot Tool entry points。

## 阶段门禁

开发必须遵守仓库外的《代码交付 Agent 冻结版实施计划》。旧计划保留为 P0/P1 历史基线，后续阶段的接管关系见 [ADR-0001](docs/adr/ADR-0001-code-delivery-pivot-and-sandbox-port.md)。P1 验收证据见 [P1 acceptance](docs/evidence/phase-1/p1-acceptance.md)。本仓库不覆盖冻结计划；如需改变冻结决策，只能新增 ADR。
