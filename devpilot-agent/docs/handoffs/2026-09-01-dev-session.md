# 2026-09-01 开发交接记录

## 交接结论

今天已完成 DevPilot 控制面与 nanobot WebUI 的审批入口联调准备。GitHub 仓库为：

`https://github.com/g3182479125-hub/nanobot-devpilot-agent`

本次提交不会包含 API Key、`.env`、`local-data`、SQLite 数据库、nanobot 会话历史、
`node_modules`、虚拟环境或运行日志。

## 已完成

### DevPilot 仓库内代码

- DevPilot WebUI 支持 `?change_set_id=<id>` 深链接。
- 页面启动时调用 `GET /v1/change-sets/{id}`，自动加载提案、项目路径、文件列表和 Diff。
- 控制面增加 loopback-only CORS，仅允许 `127.0.0.1/localhost` 的 18801 或 5173 WebUI。
- 保持审批令牌只存在于控制面调用链，深链接和 nanobot 消息不携带令牌。
- 增加 loopback CORS 自动化测试。

### nanobot WebUI 集成代码

- 新增 `DevPilotProposalCard`，从 `devpilot_propose_changeset` Tool 完成事件中提取
  `change_set_id`。
- 在 nanobot 活动区渲染“打开审批页面”按钮。
- 对 Changeset ID 和目标地址做前端校验，拒绝外部域名，防止把本地标识导向外部站点。
- 集成文件、测试和 `AgentActivityCluster` 两行修改补丁位于
  `integrations/nanobot-webui/`。

## 今日验证证据

- FC Agent Sandbox 生命周期检查已成功：backend=`fc-agent-sandbox`，exit code=`0`，
  输出为 `devpilot-fc-sandbox-ok`；该检查创建临时远程 Sandbox、执行固定无害命令并销毁，
  不上传本地项目。
- DevPilot WebUI `pnpm build` 通过。
- nanobot WebUI `pnpm run build` 通过；由于本机未安装 Bun，使用已安装的 pnpm 执行同等脚本。
- nanobot WebUI Changeset 按钮测试通过：2 tests passed。
- DevPilot durable control plane Python 测试通过；新增 CORS 测试覆盖 loopback 审批页面来源。

## 当前卡点 / 需要下一台电脑继续做

1. nanobot 源码目录是独立解压目录，没有 Git 历史；下一台电脑需先准备 nanobot checkout，
   再应用 `integrations/nanobot-webui/` 中的源文件和补丁。
2. 本机正在运行的 18800 控制面和 8765 nanobot Gateway 是旧进程；代码改动后必须重启两者，
   否则浏览器仍可能加载旧 CORS 配置或旧 `nanobot/web/dist`。
3. 尚未完成一次真实浏览器端到端点击验收：需要同时启动 18800、18801、8765，生成新的
   Changeset，点击 nanobot 按钮，确认 DevPilot 页面加载同一个 ID，再按审批顺序执行 Apply、
   `r2.verify` 和 Rollback。
4. FC Sandbox 是可选远程执行后端，不是本机 OS 隔离；真实项目执行仍必须经过人工审批、
   显式能力和既有路径/补丁约束。

## 下一台电脑启动清单

在 DevPilot 仓库：

```powershell
cd <repo>\nanobot-devpilot-agent
uv sync --dev
uv run alembic upgrade head
pnpm --dir web install
pnpm --dir web dev
```

另一个终端启动控制面：

```powershell
$env:DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE = "1"
$env:DEVPILOT_ENABLE_FC_SANDBOX = "1"
uv run devpilot serve
```

再准备 nanobot WebUI，应用集成目录中的文件/补丁并执行：

```powershell
pnpm run test -- src/tests/devpilot-proposal-card.test.tsx
pnpm run build
```

最后启动 nanobot Gateway，打开 `http://127.0.0.1:8765`，生成提案并点击审批入口。
