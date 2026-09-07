# nanobot + DevPilot 本地运行手册

nanobot 自带 WebUI 是唯一用户入口。DevPilot 不提供独立审批页面或控制台。
FastAPI 只作为 nanobot Tool Adapter 和本地 Action Broker 使用的 loopback 内部控制面。

## 换机安装（集成运行时）

从仓库根目录开始。两个项目各自保留锁文件，但集成运行时必须把 DevPilot
安装到 nanobot 使用的同一个虚拟环境；只安装 nanobot 不算完成部署：

```powershell
cd D:\path\to\nanobot-devpilot-agent
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
```

若当前网络不可用，先在独立的 `devpilot-agent` 环境执行
`uv sync --project .\devpilot-agent --locked`，再使用本机缓存按锁文件补齐集成环境；
不要用未锁定的 `pip install -U` 替代。验证两个包和全部 DevPilot Tool entry points：

```powershell
.\nanobot\.venv\Scripts\python.exe -c "import importlib.metadata as m; print(m.version('nanobot-ai')); print(m.version('devpilot-agent')); print(sorted(ep.name for ep in m.entry_points(group='nanobot.tools') if ep.name.startswith('devpilot_')))"
```

预期包含 14 个 `devpilot_*` 工具。控制面可以使用自己的 `devpilot-agent/.venv`，
但 nanobot 进程必须使用上面已经安装 DevPilot 的环境。

配置恢复时复制 `.env.example` 为本机未跟踪的 `.env`，并在 nanobot 本地配置中填写
模型、Base URL 和 API key；FC 的 `E2B_API_KEY`、`E2B_API_URL`、`E2B_DOMAIN` 只放在
本机环境变量。迁移前备份 `local-data/devpilot.db`，恢复到新的隔离 `local-data` 后，
设置正确的 `DEVPILOT_DATABASE_URL` 并先执行迁移，再启动服务；不覆盖旧目录。缺 key、
端口占用、旧库版本分别按启动日志、端口检查和 `alembic upgrade head` 输出定位。

依赖、密钥和运行数据检查完成后，再按下文启动控制面和 nanobot。停止时先停止
nanobot，再停止 DevPilot；仅删除本次新建的隔离数据目录，不删除已有运行数据。

## 启动 DevPilot 内部控制面

在 `devpilot-agent` 目录执行：

```powershell
$env:DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE = "1"
$env:DEVPILOT_ENABLE_FC_SANDBOX = "0"
$env:DEVPILOT_ALLOWED_ROOTS = "D:\path\to\allowed-projects"
$env:DEVPILOT_DATA_DIR = "D:\path\to\devpilot-agent\local-data"
$env:DEVPILOT_DATABASE_URL = "sqlite:///local-data/devpilot.db"
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

Gateway 已完成 nanobot 原生 WebUI 的受信确认注入：提案工具先登记当前 chat 的服务端
ChangeSet tuple，用户在同一聊天中明确输入 `执行`、`验证` 或 `撤销` 后，Gateway 才注入
短生命周期的隐藏确认上下文，模型随后调用 `devpilot_confirmed_action` 进入 Action Broker。
旧的 `devpilot_request_approval` 只保留给兼容/测试边界，不能作为主链路下一步；token 不进入
模型、Session、聊天文本、SSE 或日志。未登记、跨 chat、过期、篡改或没有当前请求上下文的
确认仍返回 `APPROVAL_BROKER_UNAVAILABLE`，保持 fail closed。

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
- `APPROVAL_BROKER_UNAVAILABLE`：检查提案是否已由同一 chat 产生且 tuple 完整匹配；保持
  fail closed，不恢复独立页面，不让模型携带 token；
- `EXECUTION_BACKEND_UNAVAILABLE`：检查 P6 后端证据，不把配置项当成 Sandbox 证明；
- SSE 不可用：nanobot 读取持久 Run 快照，不能宣称实时事件已经到达。
