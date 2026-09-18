# 2026-09-18：录制主流程验证与跨电脑开发交接

## 1. 交接结论

本次已在真实 WebUI、nanobot Gateway、DevPilot 控制面和 FC 沙箱上跑通录制用的 Bug-fix 主流程，并按要求把训练项目回退到故障基线。

当前可以把本分支拉到另一台电脑继续开发。训练项目保持故意失败，方便重新录制：

~~~text
3 failed, 2 passed
~~~

本文件记录录制现场和换机开发状态，不修改冻结计划。P0–P8、H1–H11 的完成状态仍以仓库中的 CURRENT_PLAN.md、证据和 review 记录为准；本次执行的是训练项目的独立 Bug-fix 演示流程。

## 2. GitHub、分支和当前状态

- GitHub：https://github.com/g3182479125-hub/devflow-agent
- 分支：codex/h11-complete-handoff
- 本次整理前 HEAD：801d721
- 训练项目：training-projects/python-project-template-bugfix
- 训练项目当前工作区：无已跟踪修改
- 本机配置、会话、数据库、日志、虚拟环境和临时目录不属于交接内容，不要上传。

换电脑拉取：

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

## 3. 训练项目的最终基线

项目目录：

~~~text
<repoRoot>\training-projects\python-project-template-bugfix
~~~

关键文件：

- main.py：JSON 任务板 CLI，故意保留三个缺陷；
- tests/main_test.py：5 个验收用例，不得修改；
- CHALLENGE.md：任务背景和边界；
- skills/bug-fix/SKILL.md、skills/repo-analysis/SKILL.md、skills/feature-delivery/SKILL.md：训练项目 Skill。

回滚后的实际 SHA-256：

~~~text
main.py:            dd957367759f4221db9b8bfcdea3e8000ae5a434601506c121d1fc85ceb81fa1
tests/main_test.py: 4d4d07083c61c284e480cde76e28f012058071743f0885123c191654b79decf6
~~~

本地确认命令：

~~~powershell
$repoRoot = (Resolve-Path .).Path
$project = Join-Path $repoRoot 'training-projects\python-project-template-bugfix'
Set-Location $project
python -m pytest -q tests/main_test.py
~~~

预期必须是 3 failed, 2 passed。这不是安装失败，而是录制故意保留的起点。

## 4. 本次真实跑通的流程

### 4.1 只读和失败证据

本机重新登记并 trust 后，项目 ID 为：

~~~text
e50f7f45-1978-47e2-b177-725881c45e17
~~~

这个 ID 属于本机 DevPilot 数据库，换电脑后必须重新登记，不能复制数据库。

只读检查和独立 FC 沙箱测试确认：

- test_add_uses_a_new_id_after_removing_a_task：删除后新增任务复用了旧 ID；
- test_remove_uses_stable_task_id：把稳定 ID 当列表下标使用，触发 IndexError；
- test_complete_unknown_id_has_a_clear_error：未知 ID 得到 None 后直接访问 done，触发 AttributeError；
- 结果：3 failed, 2 passed；
- 训练项目文件 hash 在测试前后没有漂移。

回滚后的最后一次复核 Run：

~~~text
19bfb3c0-bdda-4934-a933-a8b04c95aaaa
~~~

结果为 3 failed, 2 passed。

### 4.2 实际 ChangeSet

第二个干净会话中生成并执行的 ChangeSet：

- Plan ID：plan-7dc81a04-e7a4-4462-8fce-f24e35b515fc；
- ChangeSet ID：dfbb594e-6de6-4365-9337-59fe38111282；
- workflow：bug-fix；
- 允许文件：只有 main.py；
- 禁止修改：tests/main_test.py。

最小修改：

1. 新增 _find_task()，按稳定任务 ID 查找，找不到时抛出 KeyError；
2. add_task() 改用已有最大 ID 加一，避免删除后复用编号；
3. complete_task() 和 remove_task() 按稳定 ID 定位，不再把 ID 当列表下标。

### 4.3 执行、验证和回滚

录制链路中必须把确认词单独发送，不能拼接 ChangeSet ID 或解释文字：

~~~text
执行
~~~

本次 apply_verify 实际结果：

- Apply：applied；
- Verify：verified；
- FC：fc-agent-sandbox；
- 验证 profile：python-regression；
- 控制面验证：exit_code=0；
- 独立复核：python -m pytest -q，5 passed；
- main.py：从 dd957367... 变为 c50648ae...。

之后单独发送：

~~~text
撤销
~~~

回滚实际结果：

- ChangeSet：reverted；
- main.py 恢复为 dd957367...；
- tests/main_test.py 始终未修改；
- 回滚后独立测试恢复为 3 failed, 2 passed；
- 当前没有待处理的交付动作。

### 4.4 曾遇到的确认问题

第一次发送“执行这个 ChangeSet：<ID>”时，Gateway 没有把它识别为受信确认，旧会话随后出现 APPROVAL_BROKER_UNAVAILABLE / 未完成状态；该次没有写入文件。

新建干净会话后，使用精确的一行“执行”，Gateway 正确提供带外签名，Apply/Verify 成功。换电脑录制时必须遵守：

- 看到完整 Diff 后再发送“执行”；
- 只发送一行精确的“执行”；
- 回滚只发送一行精确的“撤销”；
- 不要让模型携带 token，也不要手动改文件冒充 Apply；
- 如果出现 APPROVAL_BROKER_UNAVAILABLE，先查询状态，不要连续重放副作用请求。

## 5. 换电脑配置顺序

### 5.1 安装

在仓库根目录执行：

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
~~~

模型配置使用新电脑自己的 USERPROFILE\.nanobot\config.json。不要复制旧电脑的 API Key、数据库、日志、确认 token 或整个 .nanobot 目录。

### 5.2 发布训练项目 Skill

~~~powershell
$repoRoot = (Resolve-Path .).Path
$project = Join-Path $repoRoot 'training-projects\python-project-template-bugfix'
$workspacePath = Join-Path $env:USERPROFILE '.nanobot\recording-workspace'
foreach ($skillName in @('bug-fix', 'repo-analysis', 'feature-delivery')) {
  & "$repoRoot\nanobot\.venv\Scripts\python.exe" "$repoRoot\devpilot-agent\scripts\publish_skill.py" --skill "$project\skills\$skillName\SKILL.md" --workspace $workspacePath
}
~~~

### 5.3 启动集成 Harness

~~~powershell
$repoRoot = (Resolve-Path .).Path
$project = Join-Path $repoRoot 'training-projects\python-project-template-bugfix'
$configPath = Join-Path $env:USERPROFILE '.nanobot\config.json'
$workspacePath = Join-Path $env:USERPROFILE '.nanobot\recording-workspace'
$dataPath = Join-Path $env:USERPROFILE '.nanobot\devpilot-data-recording'
$allowedRoots = "$workspacePath;$repoRoot"
$env:DEVPILOT_CONTROL_PLANE_TOKEN = [Environment]::GetEnvironmentVariable('DEVPILOT_CONTROL_PLANE_TOKEN', 'User')
$env:DEVPILOT_FC_ALLOWED_PROJECTS = $project
$env:DEVPILOT_FC_INSTALL_PYTEST = '1'
$env:DEVPILOT_FC_NETWORK_POLICY_MODE = 'provider-default'
Set-Location $repoRoot
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\start-integrated-harness.ps1 -ConfigPath $configPath -WorkspacePath $workspacePath -DataPath $dataPath -AllowedRoots $allowedRoots
~~~

脚本当前使用兼容 Windows PowerShell 的 RandomNumberGenerator.Create().GetBytes() 生成临时 token，不要手动改回 RandomNumberGenerator.Fill()。三个本机地址：

- WebUI：http://127.0.0.1:8765
- Gateway：http://127.0.0.1:18790
- DevPilot：http://127.0.0.1:18800

启动后，在控制面重新登记并 trust 当前电脑上的 project。不要复制旧数据库来复用项目 ID。

## 6. 下一台电脑的开发顺序

1. 拉取本分支并安装集成依赖；
2. 配置本机模型，先发送“请只回复：模型连通正常。”；
3. 运行 python -m pytest -q tests/main_test.py，确认训练项目仍为 3 failed, 2 passed；
4. 启动 Harness，登记并 trust 训练项目；
5. 如需录制，按 devpilot-agent/docs/runbooks/natural-language-full-flow-test.md 逐条操作；
6. 如需继续产品开发，先阅读 AGENTS.md、CURRENT_PLAN.md、ADR-0004 和相关 Skill，再从未完成的实际节点开始，不要重新打开已完成的 P0–P8/H1–H11；
7. 新增代码改动必须完成受影响测试、差异审查以及 Standards/Spec 双轴 review 后再提交。

录制主线最短顺序：

~~~text
普通聊天
→ / 菜单选择“修复 Bug”
→ 只读分析
→ 受控沙箱复现
→ 只读提案与 Diff
→ 单独发送“执行”
→ 看到 5 passed
→ 单独发送“撤销”
→ 回到 3 failed, 2 passed
~~~

详细稿件：

- devpilot-agent/docs/runbooks/natural-language-full-flow-test.md
- devpilot-agent/docs/runbooks/unknown-bug-full-capability-video.md
- training-projects/README.md

## 7. 已知边界

- FC 当前使用 provider-default；不要把它表述为已证明的严格断网；
- 本次确认的是 Bug-fix 主流程，不等于重新执行所有 H1–H11 演示镜头；H1–H11 的代码和历史验收证据仍以 CURRENT_PLAN.md 及 docs/evidence/live-harness/ 为准；
- 模型没有最终答复时，不要重复发送旧的 Apply/Verify/Rollback 请求；先查询状态或新建会话；
- WebUI 的“工具执行完成”不等于最终交付成功，必须以控制面终态、文件 hash 和测试结果为准；
- 不上传 API Key、config.json 私密字段、SQLite、日志、虚拟环境、pytest 临时目录或私人会话。

## 8. 本交接的验收状态

| 项目 | 状态 |
|---|---|
| 训练项目上传到当前分支 | 已完成 |
| 当前项目故障基线 | 3 failed, 2 passed |
| 只读分析 | 已完成 |
| FC 失败证据 | 已完成 |
| ChangeSet/Diff | 已完成 |
| Apply + Verify | 已完成，5 passed |
| Rollback | 已完成 |
| 回滚后基线复核 | 已完成，3 failed, 2 passed |
| 训练项目残留代码修改 | 无 |
| 换电脑本机模型配置 | 需要新电脑自行配置 |
| 换电脑项目登记/trust | 需要新电脑重新执行 |
