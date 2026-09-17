# 2026-09-17：最终换机开发交接

## 一句话结论

当前冻结计划中的 P0–P8、H1–H11 已按仓库记录完成。本次最后补的是用户体验维护：工具失败或诊断暂停后，对话不再被锁死，提示改成自然中文，斜杠菜单和录屏文档已同步。换电脑后不需要继续补冻结计划功能，直接安装本机依赖、配置自己的模型与状态目录，然后复验录制主线即可。

## 仓库和版本

- GitHub：https://github.com/g3182479125-hub/devflow-agent
- 分支：codex/h11-complete-handoff
- 本次交接前基线：ce61989
- 实现候选：990dfb3
- 双轴复核：devpilot-agent/docs/reviews/2026-09-17-ux-recovery-standards-review.md 和 devpilot-agent/docs/reviews/2026-09-17-ux-recovery-spec-review.md
- 用户体验证据：devpilot-agent/docs/evidence/phase-7/p7-ux-recovery-maintenance-2026-09-17.md

本交接文档会随当前提交一起推送；换机时以远端分支最新提交为准，不要手工拼接旧分支。

## 当前已经完成的内容

- P0–P8 已完成并有历史验收证据。
- H1–H11 Live Harness 扩展已完成并有验收/复核记录。
- 当前冻结计划没有待实现的 P/H 功能节点；后续新需求必须先更新 devpilot-agent/docs/plans/CURRENT_PLAN.md。
- 训练项目已纳入仓库：training-projects/python-project-template-bugfix。
- 工具步骤没有最终答复时，显示中性中文提示，用户可以继续发送消息或要求重新总结。
- 重复错误、路径越界、权限、超时等问题转成用户能看懂的提示，不再直接展示 Python 异常。
- 斜杠菜单可选择“选择任务模式”和“管理临时工具”。
- 任务模式切换、关闭、失败和权限拒绝都有中文说明。
- 成功提示仍明确说明：任务模式不授予审批、执行或回滚权限，仍要走正式流程。

## 换电脑操作

### 1. 拉取当前分支

新电脑：

~~~powershell
git clone -b codex/h11-complete-handoff https://github.com/g3182479125-hub/devflow-agent.git
Set-Location .\devflow-agent
~~~

已有副本：

~~~powershell
git fetch origin
git switch codex/h11-complete-handoff
git pull --ff-only origin codex/h11-complete-handoff
git status --short
git log -1 --oneline
~~~

### 2. 安装本机环境

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
~~~

不要复制另一台电脑的 .venv、数据库或配置文件。

### 3. 配置自己的模型

在新电脑配置自己的 %USERPROFILE%\.nanobot\config.json 和模型 Key。先在 WebUI 发一句短消息确认模型连通，再开始项目流程。

不要复制旧电脑的 API Key、Token、密码、SQLite 数据库、会话、日志、确认令牌或 config.json 私密字段。

### 4. 发布训练项目 Skill

~~~powershell
$repoRoot = (Resolve-Path .).Path
$project = Join-Path $repoRoot 'training-projects\python-project-template-bugfix'
$workspacePath = Join-Path $env:USERPROFILE '.nanobot\recording-workspace'
foreach ($skillName in @('bug-fix', 'repo-analysis', 'feature-delivery')) { & "$repoRoot\nanobot\.venv\Scripts\python.exe" "$repoRoot\devpilot-agent\scripts\publish_skill.py" --skill "$project\skills\$skillName\SKILL.md" --workspace $workspacePath }
~~~

### 5. 启动集成 Harness

~~~powershell
$repoRoot = (Resolve-Path .).Path
$configPath = Join-Path $env:USERPROFILE '.nanobot\config.json'
$workspacePath = Join-Path $env:USERPROFILE '.nanobot\recording-workspace'
$dataPath = Join-Path $env:USERPROFILE '.nanobot\devpilot-data-recording'
$project = Join-Path $repoRoot 'training-projects\python-project-template-bugfix'
$allowedRoots = "$workspacePath;$repoRoot"
$env:DEVPILOT_FC_ALLOWED_PROJECTS = $project
$env:DEVPILOT_FC_INSTALL_PYTEST = '1'
$env:DEVPILOT_FC_NETWORK_POLICY_MODE = 'provider-default'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$repoRoot\devpilot-agent\scripts\start-integrated-harness.ps1" -ConfigPath $configPath -WorkspacePath $workspacePath -DataPath $dataPath -AllowedRoots $allowedRoots
~~~

启动后检查：

- WebUI：http://127.0.0.1:8765
- Gateway：http://127.0.0.1:18790
- DevPilot 控制面：http://127.0.0.1:18800

如果看到 RandomNumberGenerator.Fill，说明运行的不是当前分支脚本；当前脚本使用兼容 Windows PowerShell 的 GetBytes 实现。

### 6. 登记并信任训练项目

控制面重启后，项目 trust 可能需要重新登记；不能复制旧电脑数据库。

~~~powershell
$repoRoot = (Resolve-Path .).Path
$project = Join-Path $repoRoot 'training-projects\python-project-template-bugfix'
$token = [Environment]::GetEnvironmentVariable('DEVPILOT_CONTROL_PLANE_TOKEN', 'User')
$headers = @{ 'X-DevPilot-Internal-Token' = $token }
$body = @{ project_path = $project } | ConvertTo-Json
$registered = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:18800/v1/projects' -Headers $headers -ContentType 'application/json' -Body $body
$projectId = $registered.project_id
$trustBody = @{ confirmation = 'trust' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:18800/v1/projects/$projectId/trust" -Headers $headers -ContentType 'application/json' -Body $trustBody
~~~

如果返回 PATH_DENIED，检查项目路径是否包含在 DEVPILOT_ALLOWED_ROOTS 和 DEVPILOT_FC_ALLOWED_PROJECTS。如果返回 INTERNAL_AUTH_REQUIRED，重新读取本机 Token；不要把 Token 粘贴进聊天或提交到 Git。

### 7. 验证训练项目基线

~~~powershell
Set-Location $project
python -m pytest -q
~~~

预期为 3 failed, 2 passed。这是录制项目故意保留的三个缺陷，不是环境安装失败。

### 8. 开始录制或继续开发

- 斜杠菜单 → 选择任务模式 → 修复 Bug；英文界面对应 Choose task mode → Bug fix。
- 录屏主稿：devpilot-agent/docs/runbooks/natural-language-full-flow-test.md
- 完整能力稿：devpilot-agent/docs/runbooks/unknown-bug-full-capability-video.md
- 训练项目详细说明：training-projects/README.md

## 当前验证结果和限制

本批已验证：

- 后端相关回归：76 passed；
- WebUI 回归：160 passed；
- WebUI lint：通过；
- WebUI production build：通过；
- Standards / Spec 双轴 review：PASS。

全量后端扫描曾得到 888 passed、12 skipped、3 failed，在前三个失败后停止。失败是测试环境前置条件问题：

1. test_agent_loops_do_not_share_exec_session_managers：测试环境未注册可选 exec 工具；
2. test_h7_default_driver_runs_each_task_profile_main_chain[bug-fix]：缺少 effective bug-fix Skill 快照；
3. test_main_filesystem_guard_and_controlled_shell_boundary：最小测试配置未启用 ExecTool。

这三项没有被伪装成通过，也不是本次中文提示和会话恢复修复引入的失败。若下一台电脑要求全量测试全绿，应先补齐 Harness/Skill/Exec 测试环境。

FC 当前使用 provider-default 网络策略；它不等于已经证明完全断网。真实 FC 账号、模型余额、项目 trust 和本机 Token 都需要在每台电脑重新配置和验证。

## Git 交接纪律

- 只提交源码、测试、训练项目、运行手册、证据和 review 文档。
- 不提交 .env、Key、Token、SQLite、日志、会话、数据库、虚拟环境和本地临时改动。
- 当前旧电脑留下的 HEARTBEAT.md、SOUL.md、USER.md、cron/、memory/、prompts/、sessions/、临时测试目录和其他临时训练项目均未上传。
- 新电脑开发前先执行 git status --short，不要直接 git add ..

## 下一步

1. 在另一台电脑完成安装、模型配置、Skill 发布、Harness 启动和项目 trust 登记。
2. 跑训练项目基线，再录制“只读分析 → FC 失败证据 → 提案 → 确认执行 → 验证 → 回滚”。
3. 如果模型没有最终答复，直接发“把刚才的结果总结一下”，不要重放旧副作用操作。
4. 如果要新增功能，先更新冻结计划和验收节点，再按 AGENTS.md 的双轴 review 流程开发。
