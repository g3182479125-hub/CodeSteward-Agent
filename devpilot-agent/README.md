# DevPilot Agent

个人研发自动化 Agent：以 nanobot 作为主 Runtime，在其外部增加项目分析、Git 总结、测试报告、定时任务和可追踪执行能力。

## 冻结架构

- 主 Runtime：nanobot（Python）
- 插件与组合设计参考：DeepSeek Harness
- 权限、Sandbox、审批和执行协议参考：Codex Harness
- 产品边界：个人研发自动化，不重写三套 Runtime

## 当前状态

项目已完成 P0/P1：第一个只读仓库分析垂直闭环已可运行。后续从 P2A 开始进入受控代码交付能力建设；P1 不执行仓库代码、不安装依赖、不提供任意 Shell，也不修改 nanobot 上游源码。

## 本地目录约定

nanobot 上游源码独立放在 `../nanobot-inspect/nanobot-main`，DeepSeek Harness 和 Codex Harness 仅作为只读参考资料。DevPilot 的运行数据放在 `local-data/`，该目录不会提交到 Git。

## P0 自检

在 Windows PowerShell 中运行：

```powershell
python .\scripts\doctor.py --config .\config\inspect.example.json
```

自检不会打印 API Key，也不会启动服务、安装依赖或修改上游源码；首次运行可能创建 DevPilot 的 `local-data/` 写入测试目录。

## P1 仓库分析闭环

P1 通过 nanobot 的 Tool entry point 提供 `devpilot_inspect_project` 和
`devpilot_publish_artifact`。运行 nanobot 前设置受控项目根和 DevPilot 数据目录：

```powershell
$env:DEVPILOT_ALLOWED_ROOTS = "D:\path\to\allowed-projects"
$env:DEVPILOT_DATA_DIR = "D:\path\to\devpilot-agent\local-data"
python -m nanobot gateway --config "D:\path\to\nanobot\local-state\config.json"
```

然后在 nanobot 对话中要求“使用 repo-analysis Skill 分析项目”。项目路径必须位于
`DEVPILOT_ALLOWED_ROOTS` 的真实路径内；报告只写入 DevPilot 管理的 Artifact 目录。

Skill 发布只接受本地 catalog 中经过校验、人工审核并启用的版本：

```powershell
uv run python scripts\publish_skill.py `
  --skill skills\catalog\repo-analysis\SKILL.md `
  --workspace "D:\path\to\nanobot\local-state\workspace"
```

## 安全声明

Windows 原生环境下的 workspace 限制和危险命令检查不等于 OS Sandbox。当前项目未接入或宣称任何具体 Sandbox；已有 `sandboxed-demo` 配置仅是 P0 诊断占位，不能作为执行授权。后续 ExecutionBackend 与未来 Sandbox 的能力门禁以新版冻结计划为准；能力不满足时，R2 及以上执行必须关闭。

## 阶段门禁

开发必须遵守 [代码交付 Agent 冻结版实施计划](../plans/DEVPILOT_FROZEN_PLAN.md)。旧计划保留为 P0/P1 历史基线，后续阶段的接管关系见 [ADR-0001](docs/adr/ADR-0001-code-delivery-pivot-and-sandbox-port.md)。P1 验收证据见 [P1 acceptance](docs/evidence/phase-1/p1-acceptance.md)。本仓库不覆盖冻结计划；如需改变冻结决策，只能新增 ADR。
