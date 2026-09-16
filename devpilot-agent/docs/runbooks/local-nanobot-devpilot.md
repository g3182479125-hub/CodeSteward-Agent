# nanobot + DevPilot 本地运行手册

nanobot 自带 WebUI 是唯一用户入口。DevPilot 不提供独立审批页面或控制台。
FastAPI 只作为 nanobot Tool Adapter 和本地 Action Broker 使用的 loopback 内部控制面。

## 换机安装（集成运行时）

从仓库根目录开始。两个项目各自保留锁文件，但集成运行时必须把 DevPilot
安装到 nanobot 使用的同一个虚拟环境；只安装 nanobot 不算完成部署：

```powershell
cd E:\path\to\devflow-agent
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
```

若当前网络不可用，先在独立的 `devpilot-agent` 环境执行
`uv sync --project .\devpilot-agent --locked`，再使用本机缓存按锁文件补齐集成环境；
不要用未锁定的 `pip install -U` 替代。验证两个包和全部 DevPilot Tool entry points：

如果该电脑已有同一仓库，必须原地更新，不要再 clone 一个同名目录。先保留本地工作，
再执行与当前分支对应的 fast-forward pull：

```powershell
cd E:\codex-local\nanobot-devpilot-agent
git status --short
git fetch origin
if (git show-ref --verify --quiet refs/heads/codex/h11-complete-handoff) {
    git switch codex/h11-complete-handoff
} else {
    git switch --track origin/codex/h11-complete-handoff
}
git pull --ff-only origin codex/h11-complete-handoff
```

```powershell
.\nanobot\.venv\Scripts\python.exe -c "import importlib.metadata as m; print(m.version('nanobot-ai')); print(m.version('devpilot-agent')); print(sorted(ep.name for ep in m.entry_points(group='nanobot.tools') if ep.name.startswith('devpilot_')))"
```

预期包含 16 个 `devpilot_*` 工具。控制面可以使用自己的 `devpilot-agent/.venv`，
但 nanobot 进程必须使用上面已经安装 DevPilot 的环境。

配置恢复时复制 `.env.example` 为本机未跟踪的 `.env`，并在 nanobot 本地配置中填写
模型、Base URL 和 API key；FC 的 `E2B_API_KEY`、`E2B_API_URL`、`E2B_DOMAIN` 只放在
本机环境变量。迁移前备份 `local-data/devpilot.db`，恢复到新的隔离 `local-data` 后，
设置正确的 `DEVPILOT_DATABASE_URL` 并先执行迁移，再启动服务；不覆盖旧目录。缺 key、
端口占用、旧库版本分别按启动日志、端口检查和 `alembic upgrade head` 输出定位。

依赖、密钥和运行数据检查完成后，再按下文启动控制面和 nanobot。停止时先停止
nanobot，再停止 DevPilot；仅删除本次新建的隔离数据目录，不删除已有运行数据。

若要启用 P8-03 的 Skill 改进批处理，还必须把仓库中已审核的 `bug-fix` Skill 发布到
nanobot 实际使用的 workspace。发布只会把 catalog 中的受信 Skill 原子复制到 workspace，
不会上传密钥或私有改进记录；如果目标文件已经存在，必须保留当前文件并跳过发布，避免
覆盖人工确认后的 Skill 版本。workspace 路径按本机配置替换：

```powershell
cd E:\path\to\devflow-agent\devpilot-agent
$workspace = "E:\path\to\nanobot-workspace"
$target = Join-Path $workspace "skills\bug-fix\SKILL.md"
if (Test-Path $target) {
  Write-Host "保留已有 bug-fix Skill，跳过发布：$target"
} else {
  uv run python .\scripts\publish_skill.py `
    --skill .\skills\catalog\bug-fix\SKILL.md `
    --workspace $workspace
}
```

如果暂时不启用 P8-03，可以跳过这一步。启用后若 workspace 中没有
`skills/bug-fix/SKILL.md`，命令会被视为未满足前置条件，不能开始批处理。

## 推荐启动：H6/H8/H9 完整 Harness

H6 运行中模型候选、H8 Loop 接续和 H9 持久选择不是 nanobot 的默认能力；必须同时
设置三个开关、配置 H9 完整性密钥并把数据库迁移到最新版本。不要只执行旧的 `nanobot
webui` 命令，否则普通聊天可用但动态 Harness 会被 fail-closed 禁用。

首次仅需为当前 Windows 用户生成一次 H9 完整性密钥和 H6 试用授权密钥；不要复制、提交或展示它们：

```powershell
$selectionBytes = New-Object byte[] 32
$grantBytes = New-Object byte[] 32
$randomNumberGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
try {
  $randomNumberGenerator.GetBytes($selectionBytes)
  $randomNumberGenerator.GetBytes($grantBytes)
}
finally {
  $randomNumberGenerator.Dispose()
}
[Environment]::SetEnvironmentVariable(
  "DEVPILOT_HARNESS_SELECTION_INTEGRITY_KEY", [Convert]::ToBase64String($selectionBytes), "User"
)
[Environment]::SetEnvironmentVariable(
  "DEVPILOT_CANDIDATE_GRANT_SECRET", [Convert]::ToBase64String($grantBytes), "User"
)
Remove-Variable selectionBytes, grantBytes
```

如果配置文件、工作区与隔离数据都在同一个 E: 状态根目录，可在仓库根目录运行以下一条命令。
它会在不改写聊天、配置或现有数据库的前提下：
执行迁移、检查/启动 `127.0.0.1:18800` 控制面，并以前台方式启动 nanobot。关闭该窗口才会
停止 nanobot；动态候选的装卸、更新和 Loop 切换发生在运行中，不需要关闭此窗口。

```powershell
cd E:\codex-local\nanobot-devpilot-agent
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\start-integrated-harness.ps1
```

如果当前机器没有 E:，或像演示环境一样把模型配置、受控工作区和运行数据分开保存，
必须显式传入这三个路径和项目允许根。脚本接受任意盘符，但拒绝盘符根目录、相对路径和
位于 `AllowedRoots` 之外的工作区；多个允许根按 Windows 的 `;` 分隔。先用
`-ValidateOnly` 检查，成功后删掉该开关启动：

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
$configPath = 'D:\path\to\nanobot\config.json'
$workspacePath = 'D:\path\to\demo-workspace'
$dataPath = 'D:\path\to\isolated-devpilot-data'

powershell -ExecutionPolicy Bypass -File "$repoRoot\devpilot-agent\scripts\start-integrated-harness.ps1" `
  -ConfigPath $configPath -WorkspacePath $workspacePath -DataPath $dataPath `
  -AllowedRoots $workspacePath -ValidateOnly
```

`configPath` 只在本机读取，不能录入视频或提交版本库；`dataPath` 是可隔离、可重建的
DevPilot 数据目录，不要指向已有聊天或生产数据库。

启动后打开 `http://127.0.0.1:8765`，新建话题并先输入
`/task-profile feature-delivery` 或 `/task-profile bug-fix`。Profile 回复中必须含有
`devpilot_model_plugin`；这就是 H6/H8/H9 已实际加载的最短自检。安装或删除传统 Python
entry-point 工具仍需重启 nanobot 来重新发现包元数据；通过 `/plugins` 管理的候选工具和
Loop 则不需要。

## 启动 DevPilot 内部控制面（手动排障）

在 `devpilot-agent` 目录执行：

```powershell
$env:DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE = "1"
$env:DEVPILOT_CONTROL_PLANE_TOKEN = "<随机生成的至少 32 字节本地密钥>"
$env:DEVPILOT_ENABLE_FC_SANDBOX = "1"
$env:DEVPILOT_ENABLE_MODEL_CANDIDATES = "1"
$env:DEVPILOT_ENABLE_LOOP_HANDOFF = "1"
$env:DEVPILOT_ENABLE_HARNESS_SELECTION = "1"
$env:DEVPILOT_ALLOWED_ROOTS = "E:\path\to\allowed-projects"
$env:DEVPILOT_DATA_DIR = "E:\path\to\devpilot-data"
$env:DEVPILOT_DATABASE_URL = "sqlite:///E:/path/to/devpilot-data/devpilot.db"
uv run alembic upgrade head
uv run devpilot serve --host 127.0.0.1 --port 18800
```

API 只绑定 `127.0.0.1`。不要启动 `web/`、Vite 或 18801 端口；该独立控制台已被
ADR-0003 删除。
`DEVPILOT_CONTROL_PLANE_TOKEN` 必须同时存在于 DevPilot 和 nanobot 的启动进程环境中；
`scripts/start-integrated-harness.ps1` 在未设置时会自动生成一个仅存于当前 PowerShell
进程的临时值。审批、信任和撤销路由没有该内部认证时会拒绝请求。

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
