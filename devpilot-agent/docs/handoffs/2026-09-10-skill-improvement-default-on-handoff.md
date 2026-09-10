# 2026-09-10 DevFlow Agent 交接文档

## 1. 当前结论

- GitHub 仓库：<https://github.com/g3182479125-hub/devflow-agent>
- P0–P8 已按冻结计划完成；P8-03 使用 nanobot 原生 Cron，候选 Skill 不会自动激活，也不会自动修改业务代码。
- 本次补齐了 nanobot 原生 `/skill-improvement` 的中文输出，包括命令说明、状态、参数错误、异常和定时任务结果。
- 本次没有把 API Key、`.env`、SQLite 数据库、日志、虚拟环境或本机运行目录提交到 GitHub。

## 2. 本次提交内容

文件：

- `nanobot/nanobot/command/builtin.py`
- `nanobot/tests/command/test_skill_command.py`

覆盖内容：

- `/skill-improvement` 帮助和禁用提示；
- `status`、`enable`、`run`、`disable`、反馈、候选评测和恢复等结果；
- 内部状态和常见异常的中文映射；
- 保留环境变量、命令参数、候选 ID、时间戳等技术标识符原样显示。

验证结果：

```text
23 passed                         nanobot/tests/command/test_skill_command.py
142 passed, 1 skipped             nanobot/tests/cli/test_commands.py
Ruff: All checks passed
git diff --check: passed
```

历史全量回归结果仍以已有交接记录为准；本次只重跑了受影响的 nanobot 测试集合，没有把历史结果冒充成本次全量回归。

## 3. 工作区注意事项

以下文件是你之前留下的演示 fixture 改动，本次提交没有纳入，也没有删除：

```text
devpilot-agent/tests/fixtures/node-project/src/index.js
```

其中包含 `formatThanks` 示例函数。换机后如需保留，请单独提交；如需继续录制，先确认它是否属于当前演示场景。

## 4. 另一台电脑拉取代码

全新电脑：

```powershell
$workspaceRoot = "E:\workspace"
Set-Location $workspaceRoot

git clone https://github.com/g3182479125-hub/devflow-agent.git
Set-Location "$workspaceRoot\devflow-agent"

powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1

Set-Location .\nanobot\webui
npm install
npm run build
Set-Location ..\..
```

已有本地副本：

```powershell
Set-Location "E:\workspace\devflow-agent"
git pull --ff-only origin main
```

如果工作区有自己的未提交改动，先自行提交或暂存；不要使用 `reset --hard` 覆盖本地代码。

## 5. 新电脑本地配置

不要复制旧电脑的 `local-data`、SQLite、`.env`、`local-state`、日志或虚拟环境。模型配置需要在 nanobot 本地配置中重新填写；FC 凭据只放在本机用户环境变量中。

```powershell
[Environment]::SetEnvironmentVariable("E2B_API_KEY", "<填写你自己的 FC API Key>", "User")
[Environment]::SetEnvironmentVariable("E2B_API_URL", "https://api.cn-beijing.e2b.fc.aliyuncs.com", "User")
[Environment]::SetEnvironmentVariable("E2B_DOMAIN", "cn-beijing.e2b.fc.aliyuncs.com", "User")
```

不要把真实 Key 替换进本文件后再提交，也不要把真实 Key 粘贴到聊天记录或 PowerShell 历史中。

## 6. 启动 DevPilot 控制面

打开第一个 PowerShell：

```powershell
$repoRoot = "E:\workspace\devflow-agent"
Set-Location "$repoRoot\devpilot-agent"

$env:DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE = "1"
$env:DEVPILOT_ENABLE_FC_SANDBOX = "1"
$env:DEVPILOT_FC_NETWORK_POLICY_MODE = "provider-default"
$env:DEVPILOT_FC_TEMPLATE = "code-interpreter-v1"
$env:DEVPILOT_ALLOWED_ROOTS = "$repoRoot\devpilot-agent\tests\fixtures"
$env:DEVPILOT_FC_ALLOWED_PROJECTS = "$repoRoot\devpilot-agent\tests\fixtures\node-project"
$env:DEVPILOT_DATA_DIR = "$repoRoot\devpilot-agent\local-data"
$env:DEVPILOT_DATABASE_URL = "sqlite:///local-data/devpilot.db"

$env:E2B_API_KEY = [Environment]::GetEnvironmentVariable("E2B_API_KEY", "User")
$env:E2B_API_URL = [Environment]::GetEnvironmentVariable("E2B_API_URL", "User")
$env:E2B_DOMAIN = [Environment]::GetEnvironmentVariable("E2B_DOMAIN", "User")

uv run --no-sync alembic upgrade head
uv run --no-sync devpilot serve --host 127.0.0.1 --port 18800
```

## 7. 启动 nanobot，并默认开启 Skill 改进能力

这是“本机启动默认开启”的配置。代码本身仍保留 P8 的安全兜底：没有环境变量时默认关闭；这样不会因为误启动而产生定时模型调用。

先执行一次永久配置：

```powershell
[Environment]::SetEnvironmentVariable("DEVPILOT_ENABLE_SKILL_IMPROVEMENT", "1", "User")
```

打开第二个 PowerShell：

```powershell
$repoRoot = "E:\workspace\devflow-agent"
Set-Location $repoRoot

$env:DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE = "1"
$env:DEVPILOT_ENABLE_FC_SANDBOX = "1"
$env:DEVPILOT_FC_NETWORK_POLICY_MODE = "provider-default"
$env:DEVPILOT_FC_TEMPLATE = "code-interpreter-v1"
$env:DEVPILOT_ENABLE_SKILL_IMPROVEMENT = "1"
$env:DEVPILOT_ALLOWED_ROOTS = "$repoRoot\devpilot-agent\tests\fixtures"
$env:DEVPILOT_FC_ALLOWED_PROJECTS = "$repoRoot\devpilot-agent\tests\fixtures\node-project"

$env:E2B_API_KEY = [Environment]::GetEnvironmentVariable("E2B_API_KEY", "User")
$env:E2B_API_URL = [Environment]::GetEnvironmentVariable("E2B_API_URL", "User")
$env:E2B_DOMAIN = [Environment]::GetEnvironmentVariable("E2B_DOMAIN", "User")

.\nanobot\.venv\Scripts\nanobot.exe webui --no-open --yes `
  --config ".\nanobot-config.json" `
  --workspace ".\workspace" `
  --port 8765 `
  --gateway-port 18790
```

打开 <http://127.0.0.1:8765>。已经运行的 nanobot 不会读取新环境变量，必须完整停止后重新启动。

## 8. 开启并验证定时任务

环境变量开启后，先在 nanobot 聊天中执行：

```text
/skill-improvement schedule status
```

录制或快速验证可使用每 30 秒一次：

```text
/skill-improvement schedule enable every 30 Asia/Shanghai calls=2 tokens=12000 timeout=60
```

日常使用如果希望每 30 分钟一次，应使用 1800 秒：

```text
/skill-improvement schedule enable every 1800 Asia/Shanghai calls=2 tokens=12000 timeout=60
```

然后再次执行：

```text
/skill-improvement schedule status
/skill-improvement schedule run
```

说明：`DEVPILOT_ENABLE_SKILL_IMPROVEMENT=1` 只是开启 P8 能力；`schedule enable` 才会创建 Cron 任务。没有新证据时不会调用模型；生成候选后仍需固定评测和人工确认，不会自动启用候选 Skill。

## 9. 当前已知边界

- FC 使用杭州 `provider-default`；结果必须标记实际网络策略，不能宣称已经实现网络隔离。
- FC 验证只允许已登记的项目路径和固定检查 profile，不回退到本机 Shell。
- nanobot 原生 WebUI 是唯一交互入口，没有新增 DevPilot 独立页面。
- 当前 P8 的“默认开启”是启动环境层面的默认开启；若要把源码安全默认值也改为开启，需要单独修改冻结计划并重新进行 Standards / Spec 双轴 Review，本次没有混入该变更。
- 开发或行为变更前继续完整读取 `devpilot-agent/docs/plans/CURRENT_PLAN.md`、ADR-0004 及相关说明；完成节点后由独立 Standards/Spec subagent 分别 Review，Review 只读，不直接改代码。

## 10. 明天建议第一步

1. 按第 4 节拉取并安装。
2. 按第 5 节重新配置模型和 FC 凭据。
3. 按第 6、7 节分别启动控制面和 nanobot。
4. 在网页执行 `/skill-improvement schedule status`，确认中文输出和定时任务状态。
5. 开发新功能前先读取冻结计划，确认是否属于现有 P8 范围。
