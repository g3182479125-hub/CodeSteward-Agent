# nanobot + DevPilot 本地运行手册

nanobot 自带 WebUI 是唯一用户入口。DevPilot 不提供独立审批页面或控制台。
FastAPI 只作为 nanobot Tool Adapter 和本地 Action Broker 使用的 loopback 内部控制面。

## 启动 DevPilot 内部控制面

在 `devpilot-agent` 目录执行：

```powershell
$env:DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE = "1"
$env:DEVPILOT_ENABLE_FC_SANDBOX = "0"
$env:DEVPILOT_ALLOWED_ROOTS = "D:\path\to\allowed-projects"
$env:DEVPILOT_DATA_DIR = "D:\path\to\devpilot-agent\local-data"
uv run alembic upgrade head
uv run devpilot serve --host 127.0.0.1 --port 18800
```

API 只绑定 `127.0.0.1`。不要启动 `web/`、Vite 或 18801 端口；该独立控制台已被
ADR-0003 删除。

## 启动 nanobot

按 nanobot 自身 Runbook 启动 Gateway/WebUI，并确保 nanobot 能加载 DevPilot 的
`nanobot.tools` entry points。设置 `DEVPILOT_CONTROL_PLANE_URL` 时只允许本机 HTTP
地址，默认值为 `http://127.0.0.1:18800`。

当前可验证的 nanobot WebUI 流程仅覆盖以下只读/提案能力：

1. 要求 Agent 分析指定项目；
2. 要求生成结构化变更计划和 Diff 摘要；
3. 阅读文件范围、风险、验证项和停止条件；
4. 查看当前 Run 状态、阶段事件、失败原因、Artifact 引用和下一步。

当前已经有 nanobot 事件桥接和 Action Broker 核心，但 Gateway 的 WebUI 确认事件尚未完成
受信上下文注入，真实 ControlPlane 回调也尚未接通。因此 Apply、Verify、Rollback 在默认
运行方式下仍应返回 `APPROVAL_BROKER_UNAVAILABLE`，保持 fail closed；不能因为 Tool 已注册
就把它当成动作已经可执行。P5-04 完成后，才允许在同一聊天中完成“用户确认 → Broker → 动作 →
验证/回滚”的闭环，并补充真实 nanobot WebUI E2E 证据。

DevPilot 的持久 RunEvent/SSE 是内部事件源，不能被当作公开浏览器页面。nanobot
适配层必须将阶段、状态和结果转成脱敏的结构化聊天消息；SSE 断开时使用持久快照，
不得把 capability token 转发给模型或 WebUI。

## 安全边界

- 不接受任意 Shell、PowerShell、命令拼接或模型指定的后端；
- 未获得有效审批、项目未信任或 Sandbox 未通过 P6 Gate 时，R1/R2 必须拒绝；
- API Key、审批 token、`.env`、数据库和运行日志不进入 Git；
- FC Sandbox 仍需单独完成 P6 能力、网络、恢复和真实执行证据，配置成功不等于可用；
- nanobot WebUI 中看见“已请求审批”不等于已经执行，终态必须以 DevPilot 事件和结果为准。

## 排障

- `API offline`：检查 18800 和 `DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE`；
- `PROJECT_TRUST_REQUIRED`：先在 nanobot 对话中完成项目路径、来源和风险确认；
- `APPROVAL_BROKER_UNAVAILABLE`：保持 fail closed，不恢复独立页面，不让模型携带 token；
- `EXECUTION_BACKEND_UNAVAILABLE`：检查 P6 后端证据，不把配置项当成 Sandbox 证明；
- SSE 不可用：nanobot 读取持久 Run 快照，不能宣称实时事件已经到达。
