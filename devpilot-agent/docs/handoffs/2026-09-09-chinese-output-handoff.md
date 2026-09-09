# DevFlow Agent 换机交接文档：简体中文输出修复（2026-09-09）

## 1. 交接目标

本次完成的是 nanobot 原生 WebUI 和 Agent 工作摘要的简体中文化修复，解决网页中出现
`Worked`、`Edited`、`Running` 等英文活动状态，以及 Agent 用英文解释工作过程的问题。

本次代码会与当前本地尚未推到 GitHub `main` 的 P8-03 提交一起上传。不要在新电脑上重新实现
这些改动，先按本文档拉取最新 `main`。

## 2. 当前代码状态

- GitHub：`https://github.com/g3182479125-hub/devflow-agent`
- 目标分支：`main`
- 本地运行目录：仓库根目录下的 `nanobot/` 和 `devpilot-agent/`
- nanobot 网页：`http://127.0.0.1:8765`
- DevPilot 控制面：`http://127.0.0.1:18800`（仅开发测试需要）
- FC Agent Sandbox：代码端口保留，凭据只允许放在新电脑的本地环境变量中

本次未上传或写入仓库的内容：API Key、`.env`、SQLite 数据库、本地日志、虚拟环境、
`node_modules` 和 pytest 临时目录。

## 3. 本次中文化改动

### Agent 输出约束

在 `nanobot/nanobot/templates/agent/output_language.md` 增加统一输出规则，并在
`nanobot/nanobot/agent/context.py` 的动态系统提示最后注入：

- 面向用户的工作进度、工具说明、状态、错误、提案、测试结果和最终回复使用简体中文；
- 代码、路径、命令、函数名、API 名称、字段名、错误码和原始日志可以保留原样；
- 不输出隐藏的详细思维链，只输出简短、可验证的中文工作摘要。

### nanobot WebUI

- `nanobot/webui/src/i18n/config.ts` 的默认语言改为 `zh-CN`；
- 文件编辑活动使用多语言资源，不再在组件中硬编码英文状态；
- trace、generic tool、MCP、搜索、CLI 和 Agent activity cluster 的活动摘要统一通过
  `activity-copy.ts` 按当前界面语言显示；
- 简体中文界面显示“处理了”“工具执行完成”“工具执行失败”“已编辑”等中文文案；
- 英文界面仍保留原有英文文案，其他语言仍有回退路径；
- `diff` 模式直接展开，`collapsed_diff` 模式只显示折叠入口，展开后才挂载 Diff 内容；
- 历史消息正文不会被批量改写。旧消息里已经保存的英文诊断文字仍可能显示英文；新话题和
  新活动摘要按当前规则工作。

## 4. 已完成的验证

在提交前实际完成：

- WebUI 全量测试：`54` 个测试文件、`960` 个测试全部通过；
- WebUI 构建：`npm run build` 通过；
- WebUI 代码检查：`npm run lint` 通过；
- Agent 系统提示回归：`uv run pytest tests/agent/test_context_builder.py -q`，`52 passed`；
- 网页实测：重启 nanobot 后打开 `http://127.0.0.1:8765`，活动状态显示为中文；侧边栏、
  输入框、连接状态和历史活动标签显示为简体中文；
- 网页可见活动标签实测：`处理了`、`工具执行完成`、`工具执行失败` 均可见，活动标签中
  未出现 `Worked` 或 `Edited`；
- `git diff --check`：通过。构建过程仅有既有的大资源包提示，不是本次失败。

## 5. 新电脑拉取代码

首次拉取，在 PowerShell 中执行：

```powershell
cd E:\workspace
git clone https://github.com/g3182479125-hub/devflow-agent.git
cd .\devflow-agent
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
```

如果已经有本地副本：

```powershell
cd E:\workspace\devflow-agent
git status
git pull --ff-only origin main
```

如果 `git status` 显示本地修改，先备份或提交本地修改，再决定是否拉取。不要使用
`git reset --hard` 覆盖本地工作。

拉取后确认版本：

```powershell
git log -5 --oneline --decorate
```

然后阅读以下文件：

```text
AGENTS.md
devpilot-agent/docs/plans/CURRENT_PLAN.md
devpilot-agent/docs/adr/ADR-0004-batched-review-and-incremental-recheck.md
devpilot-agent/docs/handoffs/2026-09-09-chinese-output-handoff.md
```

## 6. 安装与启动

按安装脚本完成集成依赖后，nanobot 的 WebUI 依赖和构建命令如下：

```powershell
cd E:\workspace\devflow-agent\nanobot
uv sync --dev
cd .\webui
npm ci
npm run build
```

控制面和 FC 配置请严格按照以下运行手册执行，尤其不要把真实密钥写入仓库：

```text
devpilot-agent/docs/runbooks/local-nanobot-devpilot.md
devpilot-agent/docs/runbooks/local-development.md
```

nanobot WebUI 的启动命令示例（配置文件和 workspace 路径以新电脑实际位置为准）：

```powershell
cd E:\workspace\devflow-agent
uv run nanobot webui --no-open --yes --config "E:\workspace\devflow-agent\local-state\config.json" --workspace "E:\workspace\devflow-agent\local-state\workspace" --port 8765 --gateway-port 18790
```

不要复制另一台电脑的 `local-state`、数据库或 `.env`。如果使用仓库已有启动脚本，优先使用
脚本提供的路径配置。

## 7. FC Sandbox 配置边界

FC 凭据不在 GitHub 中。新电脑需要自行配置：

```powershell
$env:DEVPILOT_ENABLE_FC_SANDBOX = "1"
$env:DEVPILOT_FC_NETWORK_POLICY_MODE = "provider-default"
$env:DEVPILOT_FC_TEMPLATE = "code-interpreter-v1"
$env:DEVPILOT_FC_ALLOWED_PROJECTS = "E:\workspace\devflow-agent\devpilot-agent\tests\fixtures\node-project"
```

同时在本机环境变量中配置 `E2B_API_KEY`、`E2B_API_URL` 和 `E2B_DOMAIN`。不要把真实密钥写入
本交接文档、PowerShell 历史、仓库文件或聊天记录。

## 8. 换机后的最小验证

1. 打开 `http://127.0.0.1:8765`，确认侧边栏和输入框为简体中文。
2. 新建一个普通话题，输入：

   ```text
   请只用简体中文简要说明你接下来要做什么，不要修改文件。
   ```

3. 如需验证 DevPilot 活动摘要，在已登记的隔离 Node 项目中使用只读请求，确认活动行显示
   “正在检查”“已读取”等中文状态。
4. 如需验证完整交付链路，按既有录制交接文档执行新的 Changeset；确认必须针对当前聊天中
   刚生成的 Changeset，不能复用历史 Changeset。

## 9. 已知边界与后续工作

- 真正的隐藏思维链不会被网页展示，也不能通过 WebUI 文案开关控制；本次修复针对的是网页
  中可见的 Agent 工作摘要、工具活动状态和系统提示约束。
- 已保存的历史英文消息不自动翻译；需要录制中文效果时新建话题。
- 技术标识符、路径、命令、错误码和原始日志保留英文是刻意设计，不属于漏翻译。
- 本次没有增加独立前端页面，没有修改 nanobot Runtime 架构，也没有改变 DevPilot 的审批、
  Apply、Verify、Rollback 和沙箱安全边界。
- 后续继续开发前仍必须先读 `CURRENT_PLAN.md`，每个开发节点完成后按冻结计划使用独立
  Standards/Spec Review；Review 只读，不允许直接修改代码。
