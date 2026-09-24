# 2026-09-24 跨电脑交接：ShopSphere Bug-fix 训练项目

## 交接结论

本次已把当前进度整理到主仓库 `devflow-agent`，并准备推送到分支
`codex/h11-complete-handoff`。主仓库继续承载 nanobot、DevPilot 控制面、Harness
和录制文档；ShopSphere 被整理成可随主仓库迁移的训练项目：

```text
training-projects/shopsphere-bugfix
```

主仓库当前基线提交为 `59a5f9f fix: derive test wait from control plane backend`；
本交接提交会在它之上增加本训练项目、训练说明和本文档。不要把本机配置、会话、
数据库或临时目录当作代码同步。

## 当前完成情况

### DevPilot / nanobot 主系统

- 冻结计划 P0–P8 与 Live Harness H1–H11 的已记录实现保留在当前分支。
- SI-01–SI-08 的相关代码、测试和交接材料保留在仓库中。
- 本机默认使用本地执行；FC 仍是显式选择的受控执行路径，不要把个人电脑的
  `config.json`、模型密钥或控制面 token 复制到另一台电脑。
- 既有录制和启动说明仍以 `training-projects/README.md`、
  `devpilot-agent/docs/runbooks/` 以及 `devpilot-agent/docs/plans/CURRENT_PLAN.md`
  为准。

### ShopSphere 训练项目

当前快照位于 `training-projects/shopsphere-bugfix`，包含以下已经完成的工作：

- 订单并发取消的库存重复恢复已修复：先用 `UPDATE ... WHERE status = 原状态`
  抢占状态，只有 `rowcount == 1` 的请求才会恢复库存。
- 密码重置页面已从 `forgot_password.html` / `reset_password.html` 改名为
  `forgot_form.html` / `reset_form.html`，并同步修改 `shop/auth.py` 的渲染引用。
  这是因为 DevPilot 的运行时执行副本会过滤文件名中含 `password` 的模板，
  导致原页面在受控运行时出现 `TemplateNotFound`；限流逻辑本身并不是根因。
- `tests/test_shop.py` 包含并发取消订单回归测试。
- 当前完整测试结果：

  ```text
  python -m unittest discover -s tests -t .
  Ran 223 tests ... OK
  ```

ShopSphere 原始上游是 `yongjunmun/E-Commerce-Website`。本次没有向原作者的
`origin` 推送；当前可继续开发的版本已经随你的 `devflow-agent` 仓库提交。

## 换电脑后的操作

### 1. 拉取主仓库

```powershell
git clone -b codex/h11-complete-handoff https://github.com/g3182479125-hub/devflow-agent.git
Set-Location .\devflow-agent
git pull --ff-only origin codex/h11-complete-handoff
```

### 2. 安装主系统依赖

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
```

这一步只安装本机运行依赖。模型 provider、API Key、控制面 token 和状态目录要
在新电脑上重新配置。

### 3. 先单独验证训练项目

```powershell
Set-Location .\training-projects\shopsphere-bugfix
python -m pip install -r requirements.txt
python -m unittest discover -s tests -t .
```

看到 `Ran 223 tests ... OK` 才说明迁移的代码和测试文件完整。需要启动商城时：

```powershell
python run.py
```

浏览器打开 `http://127.0.0.1:5000/`。数据库、密钥和上传文件会生成在该项目的
`instance/` 中，不要提交。

### 4. 配置并启动 Harness

必须使用新电脑自己的 nanobot 配置。下面是路径关系，实际用户名和仓库路径按新
电脑修改：

```powershell
$repoRoot = (Resolve-Path .).Path
$project = Join-Path $repoRoot 'training-projects\shopsphere-bugfix'
$configPath = Join-Path $env:USERPROFILE '.nanobot\config.json'
$workspacePath = Join-Path $env:USERPROFILE '.nanobot\recording-workspace'
$dataPath = Join-Path $env:USERPROFILE '.nanobot\devpilot-data-recording'
$allowedRoots = "$workspacePath;$repoRoot"
$env:DEVPILOT_FC_ALLOWED_PROJECTS = $project
$env:DEVPILOT_FC_INSTALL_PYTEST = '1'

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "$repoRoot\devpilot-agent\scripts\start-integrated-harness.ps1" `
  -ConfigPath $configPath `
  -WorkspacePath $workspacePath `
  -DataPath $dataPath `
  -AllowedRoots $allowedRoots
```

启动后打开 `http://127.0.0.1:8765/#/new`，登记并选择完整项目路径
`$project`。如果出现 `PATH_DENIED`，先把项目路径加入允许根目录并确保它就是
当前 clone 下的路径；如果出现内部认证错误，重新读取新电脑自己的
`DEVPILOT_CONTROL_PLANE_TOKEN`，不要复制旧电脑的 token。

### 5. 继续开发的建议顺序

1. 先阅读本文、`training-projects/shopsphere-bugfix/HANDOFF.md` 和
   `devpilot-agent/docs/plans/CURRENT_PLAN.md`。
2. 运行 ShopSphere 的 223 个测试，确认迁移无误。
3. 若要录制“故障到修复”，使用隔离副本制造一个明确、可回滚的故障；保留
   原始 `shopsphere-bugfix` 作为修复后基线。
4. 在 WebUI 里从 `/` 菜单选择 Bug-fix profile，再做只读检查、受控复现、方案
   提案、确认执行和验证。普通对话不需要手写模式命令。
5. 每次修改前确认项目路径和允许根目录一致；每次修改后先跑项目测试，再看
   Diff。不要把 ShopSphere 的 `origin` 改成原作者仓库后直接推送。

## 明确未上传的内容

以下内容本次刻意未加入 Git：本机 `.nanobot/`、模型配置、API Key、控制面 token、
SQLite 数据库、日志、`.venv/`、缓存、上传图片、会话记录，以及主仓库原本存在的
其他未跟踪个人文件。它们必须在新电脑上重新生成或配置。

## 验收与边界

- 已上传的是当前“可继续开发”的代码和交接材料，不代表另一台电脑已经自动拥有
  模型配置或正在运行的服务。
- ShopSphere 快照当前是修复版；如果视频需要展示真实失败，必须使用隔离副本，
  不要直接改坏 GitHub 上的修复基线。
- 本文没有把任何“未实际验证”的结果写成已完成；如果新电脑的环境不同，先按
  上面的测试和启动步骤重新取得证据。
