# 2026-09-16 录屏验证与换机开发交接

## 1. 交接目标

本次交付是给另一台电脑继续开发和录制使用的当前分支。交接内容包括：

- H1–H11 现有实现继续按 CURRENT_PLAN 和对应 evidence 使用；
- 修复集成 Harness 在数据库已经迁移到 H8/H9 版本时，P8 Skill 存储错误拒绝启动的问题；
- 修正 Windows PowerShell 生成 Harness 随机密钥的命令；
- 把自然语言录屏流程改成短消息、具体项目目录和真实成功/失败分支；
- 保留本次 WebUI 现场没有跑通的部分，不把模型未完成回合写成成功。

## 2. GitHub 状态

- 仓库：<https://github.com/g3182479125-hub/devflow-agent>
- 分支：codex/h11-complete-handoff
- 最终候选提交：1cde483
- 工作目录：D:\Users\tianmin.li\.codex\worktrees\6401\remote-integrate
- 本交接文档和录屏稿随本次提交上传；提交完成后以 GitHub 分支上的最新 SHA 为准。

另一台电脑直接执行：

~~~~powershell
git clone -b codex/h11-complete-handoff https://github.com/g3182479125-hub/devflow-agent.git
Set-Location .\devflow-agent
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
~~~~

如果电脑上已经有这个仓库，先执行 git status --short 保留本地修改，再使用对应分支的 git pull --ff-only。不要把旧电脑的 SQLite、日志、虚拟环境、模型密钥、确认 token 或 Skill 候选复制过去。

## 3. 目前已完成的代码和文档

### P8 数据库兼容修复

文件：devpilot-agent/src/devpilot/skill_improvement.py

P8 SkillImprovementStore 原来只接受 0011/0012 两个数据库版本。集成 Harness 启动时会继续迁移到 0013、0014、0015、0016；因此 task-profile bug-fix 激活后，下一轮会被错误判断为 Skill snapshot unavailable。

现在明确接受同一条线性迁移链上的以下版本：

- 0011_p8_skill_improvement_records
- 0012_h3_plugin_runtime_state
- 0013_h8_loop_handoff_state
- 0014_h8_requested_driver_state
- 0015_h8_switch_generation
- 0016_h9_harness_selection_records

未知或分叉版本仍然 fail closed，不会因为“版本号更高”就盲目放行。

文件：devpilot-agent/tests/test_p8_01_skill_improvement.py

新增回归测试，确认生产 Store 在当前 0016 H9 数据库头上仍能捕获任务 Skill snapshot。

### Windows 启动修复

文件：devpilot-agent/scripts/start-integrated-harness.ps1

启动脚本已经使用 RandomNumberGenerator.Create().GetBytes()，兼容当前 Windows PowerShell/.NET 环境。配套运行手册中的旧 RandomNumberGenerator.Fill 命令也已经改掉，位置是：

devpilot-agent/docs/runbooks/local-nanobot-devpilot.md

### 录屏稿

文件：devpilot-agent/docs/runbooks/natural-language-full-flow-test.md

这份稿子现在明确写出了：

- 具体项目目录、入口文件、测试文件和三个故障；
- 录屏前的基线命令：python -m pytest -q tests/main_test.py；
- 本机没有 E 盘时的完整 Harness 启动命令；
- 每个镜头的“我说”“我发”“看画面”；
- Agent 只读分析与终端测试的边界；
- bug-fix 提案必须绑定服务端失败 Run；
- 没有最终答复或进入 H8 暂停状态时如何如实收尾；
- 不把未验证的 Apply、Verify、Rollback、图片识别写成已通过。

## 4. 训练项目位置和准备方式

训练副本当前位于：

D:\Users\tianmin.li\.codex\worktrees\6401\remote-integrate\training-projects\python-project-template-bugfix

它来自 [pamelafox/python-project-template](https://github.com/pamelafox/python-project-template)，是专门用于 Agent 修复演示的独立副本。主要文件：

- main.py
- tests/main_test.py
- CHALLENGE.md
- README.md

故意保留的三个问题：

1. add_task() 用列表长度生成 ID，删除后可能复用旧 ID；
2. remove_task() 把稳定 ID 当列表位置，删除错位后的任务会失败或删错；
3. complete_task() 找不到任务时直接访问 None.done，没有清楚的 KeyError。

这个目录以及它下面的 skills/目前是工作区里的未跟踪训练材料，不会随主仓库自动出现。换电脑时需要另外复制，或重新从上游准备一份后按录屏稿发布三个 Skill。不能把已修复过的副本当作基线。

## 5. 实际验证结果

### 已通过

- 项目目录内执行 python -m pytest -q tests/main_test.py：3 failed, 2 passed，故障可复现；
- start-integrated-harness.ps1 -ValidateOnly：通过；
- 集成 Harness 实际启动：通过，数据库迁移到 0016；
- WebUI、Gateway、DevPilot Control Plane：均能连接；
- WebUI 选择训练项目：通过；
- /task-profile bug-fix：通过；
- Agent 只读读取 main.py、tests/main_test.py 并定位三个问题：通过；
- DevPilot 全量 pytest：使用 nanobot 虚拟环境并设置跨包 PYTHONPATH 后退出码 0，Windows symlink 权限用例保留为 3 个 skip；
- DevPilot Ruff：通过；
- Markdown link check：通过；
- nanobot P8/Skill/Dream/Profile/Cron 相关定向回归：清除当前 Harness 环境变量后 83 passed；
- git diff --check：通过。

### 本次 WebUI 没有通过、不能假装通过

- 当前 DeepSeek V4.1 Flash 在读取 Run/项目工具后出现“工具调用完成，但没有生成最终答复”；
- 继续追问会触发“上一次操作的结果尚未确认”，H8 正确暂停并禁止重放；
- 普通 bug-fix WebUI Profile 没有 shell/exec 工具，Agent 不会替用户在本机终端执行 pytest；
- devpilot_bug_fix_propose 需要服务端保存的失败 Run 和分析 Run；聊天里手动说“3 failed”不是合法失败证据；
- 因此本轮没有把 bug-fix 的正式 Diff、Apply、Verify、Rollback 记为 WebUI 通过；
- 图片识别只有在模型实际说出图中文字或界面细节时才算通过，本轮不计入通过。

这不是 P8 数据库修复失败，而是当前模型回合和失败证据前置条件没有形成完整提案闭环。视频中应展示真实状态，不能为了连贯性补写一个不存在的 Diff。

## 6. 另一台电脑的启动顺序

1. 拉取本分支并安装集成依赖。
2. 在本机填写自己的模型配置；不要复制旧电脑的 config.json、API Key 或环境变量值。
3. 准备未修复的训练项目副本。
4. 按 natural-language-full-flow-test.md 发布项目 Skill，并确认三个 skills/*/SKILL.md 文件存在。
5. 按录屏稿中的 -ValidateOnly 命令检查路径，再启动 Harness。
6. 打开 http://127.0.0.1:8765，选择训练项目，先发 /task-profile bug-fix。
7. 每次只发一条短消息；等当前回合结束后再发下一条。
8. 如果出现“上一次操作的结果尚未确认”，新开浏览器标签页和新会话，不要在暂停会话中重发。
9. 测试结果以项目终端和服务端持久状态为准，不以聊天中的“应该通过”作为证据。

## 7. 下一位开发者先做什么

建议顺序：

1. 先检查本交接分支和 natural-language-full-flow-test.md；
2. 用新的隔离 DataPath 重跑启动和只读 WebUI 段；
3. 如果要继续修复 WebUI 提案未完成问题，先检查 DeepSeek V4.1 Flash 的工具调用后最终答复链路和 H8 pause/resume 语义，不要放宽确认或允许重放；
4. 如果要让录屏完整跑到 bug-fix 提案，先补一条受控的“终端结果登记为服务端 failure Run”的用户入口，必须由服务端读取/哈希项目文件，不能信任模型手写的失败事实；
5. 这个新入口需要单独写需求、测试、证据和双轴 Review，不要通过聊天文字绕过现有 failure_evidence_ids 门禁；
6. 继续遵守 AGENTS.md、CURRENT_PLAN.md 第 0/6/7 节和 ADR-0004，不重开已完成的 P0–P8/H1–H11。

## 8. 不要提交的内容

以下内容必须保持未跟踪或留在本机：

- API Key、模型密钥、H9 完整性密钥、Control Plane token；
- .env、config.json 私密内容；
- SQLite 数据库、运行日志、聊天记录和临时截图；
- nanobot/.venv、node_modules、pytest 缓存；
- training-projects/训练副本及其中的运行产物；
- nanobot/.pytest-h8-current/、nanobot/MagicMock/等本次运行生成目录。
