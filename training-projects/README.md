# 录制训练项目

这里放的是专门给 DevPilot / nanobot 录制修复流程用的项目。推荐使用：

```text
training-projects/python-project-template-bugfix
```

它是从 [pamelafox/python-project-template](https://github.com/pamelafox/python-project-template)
复制出来的独立 Python 项目，保留 MIT 许可。项目中的 `main.py` 故意保留了三个缺陷，
所以录制开始时测试应该失败；Agent 修复、FC 验证、回滚之后，项目应该回到这个失败基线。

## 项目里有什么

```text
python-project-template-bugfix/
├─ main.py                         # JSON 任务板 CLI，故意有缺陷
├─ tests/main_test.py              # 5 个测试，基线预期 3 failed, 2 passed
├─ CHALLENGE.md                    # 任务背景、验收命令和安全边界
├─ requirements-dev.txt            # pytest 等开发依赖
└─ skills/
   ├─ bug-fix/SKILL.md             # 缺陷修复流程
   ├─ repo-analysis/SKILL.md       # 只读分析流程
   └─ feature-delivery/SKILL.md    # 小功能交付流程
```

三个故意保留的问题是：删除后编号可能重复、按编号删除可能删错任务、完成不存在的编号时错误不清楚。

## 先确认故障基线

在主仓库根目录打开 PowerShell：

```powershell
$repoRoot = (Resolve-Path .).Path
$project = Join-Path $repoRoot 'training-projects\python-project-template-bugfix'
Set-Location $project
python -m pytest -q
```

预期是 `3 failed, 2 passed`。这是故意留下的录制起点，不是安装失败。只在这个项目目录运行测试，
不要在用户目录或主仓库根目录运行裸的 `python -m pytest -q`。

如果本机没有 pytest，可以在本机安装开发依赖：

```powershell
python -m pip install -r requirements-dev.txt
```

不过，录制 FC 流程时不要求把 pytest 固定安装到 FC 镜像；集成 Harness 默认会在每个临时 Python
测试 Sandbox 中安装 pytest。环境变量 `DEVPILOT_FC_INSTALL_PYTEST=0` 才会关闭这个临时安装。

## 从 GitHub 拉取并启动

```powershell
git clone -b codex/h11-complete-handoff https://github.com/g3182479125-hub/devflow-agent.git
Set-Location .\devflow-agent
$repoRoot = (Resolve-Path .).Path
$project = Join-Path $repoRoot 'training-projects\python-project-template-bugfix'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
```

先在这台电脑上配置自己的模型和本地状态目录。不要从另一台电脑复制 `config.json` 的私密内容、
API Key、SQLite 数据库、日志或确认令牌。

如果要让 Harness 读取项目自带的三个任务 Skill，先发布到本机录制 workspace：

```powershell
$workspacePath = Join-Path $env:USERPROFILE '.nanobot\recording-workspace'
foreach ($skillName in @('bug-fix', 'repo-analysis', 'feature-delivery')) {
  & "$repoRoot\nanobot\.venv\Scripts\python.exe" `
    "$repoRoot\devpilot-agent\scripts\publish_skill.py" `
    --skill "$project\skills\$skillName\SKILL.md" `
    --workspace $workspacePath
}
```

然后启动本机集成 Harness：

```powershell
$configPath = Join-Path $env:USERPROFILE '.nanobot\config.json'
$workspacePath = Join-Path $env:USERPROFILE '.nanobot\recording-workspace'
$dataPath = Join-Path $env:USERPROFILE '.nanobot\devpilot-data-recording'
$allowedRoots = "$workspacePath;$repoRoot"
$env:DEVPILOT_CONTROL_PLANE_TOKEN = [Environment]::GetEnvironmentVariable('DEVPILOT_CONTROL_PLANE_TOKEN', 'User')
$env:DEVPILOT_FC_ALLOWED_PROJECTS = $project
$env:DEVPILOT_FC_INSTALL_PYTEST = '1'
$env:DEVPILOT_FC_NETWORK_POLICY_MODE = 'provider-default'

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "$repoRoot\devpilot-agent\scripts\start-integrated-harness.ps1" `
  -ConfigPath $configPath `
  -WorkspacePath $workspacePath `
  -DataPath $dataPath `
  -AllowedRoots $allowedRoots
```

看到 Harness 正常启动后，打开 <http://127.0.0.1:8765/#/new>。在项目选择器中选择完整路径
`$project`。如果项目还没有登记，可以用本机控制面完成登记和信任。下面的命令只访问回环地址，
不会把 token 打印到屏幕：

```powershell
$headers = @{ 'X-DevPilot-Internal-Token' = $env:DEVPILOT_CONTROL_PLANE_TOKEN }
$body = @{ project_path = $project } | ConvertTo-Json
$registered = Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:18800/v1/projects' `
  -ContentType 'application/json' -Body $body
$projectId = $registered.project_id

$trustBody = @{ confirmation = 'trust' } | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:18800/v1/projects/$projectId/trust" `
  -Headers $headers -ContentType 'application/json' -Body $trustBody
```

如果返回 `PATH_DENIED`，检查 `$project` 是否在 `$repoRoot` 下并且已经放进
`DEVPILOT_FC_ALLOWED_PROJECTS`；如果返回 `INTERNAL_AUTH_REQUIRED`，在同一个 PowerShell
窗口重新读取本机的 `DEVPILOT_CONTROL_PLANE_TOKEN` 后再执行。信任请求中的 `trust` 是系统确认词，
也可以使用中文 `信任项目`；不要把确认词混在普通聊天里。

## 录制时发什么

每次只发一条，等 Agent 回完再发下一条。下面是最短的完整 Bug-fix 主线：

1. 先发：

   ```text
   嗨，你简单说说你能帮我做什么。
   ```

2. 再发：

   ```text
   /task-profile bug-fix
   ```

3. 只读看代码：

   ```text
   先看看入口和测试，告诉我这几个失败分别是什么原因。只读，不要改文件。
   ```

4. 请求受控测试：

   ```text
   请用受控沙箱跑一下这个项目的测试，确认问题能复现。不要修改代码。
   ```

5. 请求方案但不执行：

   ```text
   根据刚才的失败记录，给我一个只改必要文件的修复方案和 Diff。先提案，不要执行。
   ```

6. 确认页面确实展示了完整 Diff，并且当前项目就是这个训练副本后，才发：

   ```text
   执行
   ```

7. 如果系统单独要求验证，再发：

   ```text
   验证
   ```

8. 修复后终端应看到 `5 passed`。最后要录回滚时，等回滚确认完成，再在项目目录运行测试，
   应回到 `3 failed, 2 passed`。

更自然的完整台词、镜头顺序、停止/恢复和越界读取安全测试，见：

- `devpilot-agent/docs/runbooks/natural-language-full-flow-test.md`
- `devpilot-agent/docs/runbooks/unknown-bug-full-capability-video.md`
- `devpilot-agent/docs/handoffs/2026-09-17-recording-flow-and-fc-fixes-handoff.md`

## 录制边界

- 只把 `training-projects/python-project-template-bugfix` 当作演示项目。
- 不要把 `.env`、API Key、SQLite、日志、虚拟环境、临时 pytest 目录或私人文件放进提交或视频。
- `provider-default` 是 FC 提供商默认网络策略，不等于已经证明完全断网。
- 没有正式失败证据、没有正式 Diff 或模型没有最终答复时，不要口播“已经修复”；保留真实失败结果即可。
