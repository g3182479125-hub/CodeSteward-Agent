# 2026-09-17：录屏主线与 FC 验证修复交接

## 当前分支与交接范围

- 仓库：<https://github.com/g3182479125-hub/devflow-agent>
- 分支：`codex/h11-complete-handoff`
- 本次提交包含：FC 项目测试兼容、默认 pytest 临时启动安装、WebUI 搜索不阻塞事件循环、自然语言 Bug-fix 证据门槛修复、录屏稿和相应回归测试；FC fallback 修复为 `e5d168e`、`d722a67`、`bf70a22`，pytest 启动安装由本次交付加入。
- 不包含：API Key、`.env`、SQLite、日志、临时 pytest 目录，以及 `nanobot/webui/package-lock.json` 的既有本地变更。
- 已包含：`training-projects/python-project-template-bugfix` 这份独立、故意故障的录制训练项目；不包含其他本机临时训练目录。

## 本次完成内容

### 1. 真实 FC 项目测试

- FC Code Interpreter 的 Python 项目测试改为在测试命令前先运行服务端固定的
  `python -m pip install --disable-pip-version-check pytest`，每次 Sandbox 临时安装，
  再执行 `python -m pytest -q`；`DEVPILOT_FC_INSTALL_PYTEST=0` 时仍执行 pytest，
  但如果 FC 模板没有预装 pytest 就会如实失败，不会伪造或切换到其他运行器。
- FC 命令从上传项目根目录执行，避免上传代码无法被项目测试导入。
- 安装失败、网络不允许或回执不完整只记录为未知执行结果，不生成 `failure_run_id`。
- `provider-default` 不被表述成绝对断网；回执仍记录宿主机无文件写入、受限命令和沙箱信息。

### 2. WebUI 卡住修复

- `nanobot` 的文件查找移入 `asyncio.to_thread`，避免大目录扫描阻塞事件循环，导致停止或新话题无法发送。
- 相应回归：`nanobot/tests/tools/test_search_tools.py`。

### 3. 自然语言 Bug-fix 提案可用性

- `FeatureDeliveryWorkflow` 与 `BugFixWorkflow` 只要求服务端生成的 `inspect + read` 证据；`search` 结果仍会被保留，但不再是小项目提案的硬前置。
- Bug-fix 的故障描述和预期行为允许自然语言改写；真正的安全绑定仍来自服务端失败 Run、项目 ID、目标文件、前置哈希和 `allowed_files`，没有取消这些约束。

### 4. 录屏材料

- 新稿：[unknown-bug-full-capability-video.md](../runbooks/unknown-bug-full-capability-video.md)。
- 主线：未知缺陷 → 只读检查 → FC 失败证据 → 最小 Diff → 确认执行 → FC 验证 → 回滚。
- 追加片段覆盖：运行中工具热插拔、Loop 切换、Skill 改进、停止/恢复/权限边界。
- 项目信任必须由操作者预先在服务端登记；聊天中输入“我信任”不会替代信任登记，也不向模型泄露缺陷答案。

## 已验证结果

### 2026-09-17 全流程现场验收（历史证据）

在本机专用录制配置下，已完成一遍正式闭环：

1. 录制项目 `training-projects/python-project-template-bugfix` 保持初始故障，FC
   真实运行 `python -m pytest -q`，得到 `3 failed, 2 passed`。
2. 通过正式 Bug-fix 提案生成 ChangeSet，确认后走 `r1.apply`，只修改 `main.py`。
3. 通过正式 `r2.verify` 执行 `python-regression`，FC 回执为 `exit_code=0`、
   `5 passed`；该 profile 会在同一临时 Sandbox 内默认安装 pytest。
4. 通过正式 `r1.rollback` 恢复项目初始哈希；最后再次运行 FC，得到预期的失败基线，
   因此当前目录已经可以直接从“发现故障”开始录制。

本次现场闭环的关键 ChangeSet 为 `eb6f1dbb-7d50-4ff3-b6db-84928c2086e8`；验证成功后
已回滚，不要把它当作当前项目已修复状态。当前 `main.py` 哈希应为：
`dd957367759f4221db9b8bfcdea3e8000ae5a434601506c121d1fc85ceb81fa1`。

这组结果是本机专用录制配置下的历史验收证据，证明代码链路曾经完成过 Apply → FC Verify →
Rollback；它不等于换电脑后模型、FC 账号、控制面数据库和项目 trust 已经自动就绪。换电脑后
仍需按 `training-projects/README.md` 重新登记并信任项目，再实际复验当前模型的完整录制流程。
录屏稿中记录的 DeepSeek V4.1 Flash 当前回合未产出正式 Diff，和这里保存的历史闭环证据属于
不同环境/回合，不能互相替代。

```powershell
$py = '.\devpilot-agent\.venv\Scripts\python.exe'
& $py -m pytest devpilot-agent/tests/test_p4_bug_fix.py devpilot-agent/tests/test_p4_feature_delivery.py devpilot-agent/tests/test_agent_test_runs.py -q
# 58 passed

& $py -m ruff check devpilot-agent/src/devpilot/workflows.py devpilot-agent/tests/test_p4_bug_fix.py devpilot-agent/tests/test_p4_feature_delivery.py
# All checks passed
```

本机 FC 配置与链路状态：

1. 已安装本机虚拟环境的可选 FC 依赖：`uv sync --extra fc-sandbox`；
2. 独立 `uv run devpilot sandbox-check` 已通过，确认 FC 后端可创建沙箱并返回回执；
3. 真实 WebUI 项目测试已打到 `fc-agent-sandbox`。最新 Run 为
   `69971ca9-7f4c-42d0-9eb5-1db9a2e53f1f`，实际命令是
   `python /workspace/devpilot/.devpilot/plain_test_runner.py`，退出码为 `1`；
4. 之前的退出不是项目断言失败，而是镜像缺少 `pytest`；本次已改为每个 Python 项目
   测试在 FC Sandbox 内临时安装 pytest。安装失败仍归类为 `unknown`，不生成
   `failure_run_id`，也不写入 `test_check.failed`；
5. 当前演示项目含 `import pytest` 和 pytest fixture，重新启动最新代码后可以取得真实
   断言失败证据，继续录制“真实项目缺陷 → 专用提案 → Apply → 验证 → Rollback”。

网络策略：本机用户环境已配置 `DEVPILOT_FC_NETWORK_POLICY_MODE=provider-default`，因为
当前 FC 端点拒绝严格网络隔离模式。该模式只表示采用提供商默认网络策略，不能宣称网络隔离已证明；
需要安全演示时使用支持 strict 模式的 FC 端点。内部控制面认证令牌在本机用户环境中生成，未提交到仓库。

DeepSeek 余额恢复后，WebUI 新话题已实测返回“模型连通正常”。录制时仍应每步等待当前回合结束，再发送下一条。

## 换电脑后开始

首次拉取：

```powershell
git clone -b codex/h11-complete-handoff https://github.com/g3182479125-hub/devflow-agent.git
Set-Location .\devflow-agent
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
```

已有仓库：

```powershell
git status --short
git fetch origin
git switch codex/h11-complete-handoff
git pull --ff-only origin codex/h11-complete-handoff
```

之后在新机器配置自己的模型 Key 和本地状态目录，不能复制旧机器的 Key、数据库、日志、确认令牌或 `config.json` 私密内容。

## 演示项目说明

录制训练项目已经上传到 `training-projects/python-project-template-bugfix`，其中有 `main.py`、
`tests/main_test.py`、`CHALLENGE.md` 和三个项目级 Skill。它保持未修复基线，录制开始前应看到
`3 failed, 2 passed`；不要把它当作正常产品源码，也不要提前手动修复来伪造 Agent 的结果。
换电脑后直接按 [`training-projects/README.md`](../../../training-projects/README.md) 克隆、启动、
登记项目和录制。只在录制前由操作者将该项目加入 AllowedRoots、FC 白名单并登记为可信项目。

## 下一步建议

### 已完成：跨消息 Bug-fix 证据复用

该服务端缺口已由 `075846d` 和 `0a4ef50` 修复，并按 ADR-0004 完成
Standards / Spec 双轴复核，记录见：

- [Standards review](../reviews/2026-09-17-bug-fix-evidence-reuse-standards-review.md)
- [Spec review](../reviews/2026-09-17-bug-fix-evidence-reuse-spec-review.md)

**复现：**在同一 WebUI `bug-fix` 话题中，先完成“读取代码 + FC 失败测试”，再要求生成
专用 Bug-fix 提案。模型有时会新建一个仅含 `evidence.inspect.completed` 的 repo-analysis Run，
但不读取文件，就把该不完整 Run 传给 `devpilot_bug_fix_propose`。服务端正确返回
`WORKFLOW_EVIDENCE_INVALID`；模型随后可能退回通用 `devpilot_propose_changeset`，只绑定
`python-syntax`，这不满足录屏所需的回归验证闭环。

**已确认事实：**

- `/task-profile bug-fix` 已启用不是根因；它只影响可见工具和提示，不会自动复用证据。
- 当前项目的完整分析证据和失败证据均已存在；失败 Run、项目绑定、目标文件及哈希有效。
- 已修复“search 必须存在”和“故障说明必须逐字匹配”两个过严门槛；本问题是模型重试时选择了新建但不完整的 analysis Run。

**实际实现：**在服务端 Bug-fix 提案入口中，调用方提供的 analysis evidence 不完整时，
仅在**同一 project_id** 的历史 repo-analysis Run 中选择最近一份同时具备
`evidence.inspect.completed` 与至少一个 `evidence.read.completed` 的服务端证据。找不到完整证据
仍拒绝；跨项目、伪造证据、失败 Run、文件哈希、`allowed_files` 和确认门禁均保持原有约束。

**已完成的自动化验收：**

1. 单元测试：不完整 analysis ID + 同项目完整历史 analysis ID 时，专用 Bug-fix 提案成功且合同只记录完整 ID。
2. 单元测试：没有同项目完整历史证据、跨项目证据或伪造证据时，仍为 `WORKFLOW_EVIDENCE_INVALID`。
3. 额外覆盖：完整证据即使排在 33 个较新的不完整 Run 之后，仍能被找到。

**2026-09-17 现场状态：**新 WebUI 已实测可选择训练项目、切换 `bug-fix` profile、完成
inspect/read/search 的只读分析，并已真实调用 FC。控制面重启后需要重新登记项目 trust；本机已用
内部认证重新登记训练项目。当前代码已绕过“模板没有 pytest”的阻塞：只要当前 FC
`provider-default` 允许出网，每次 Python 项目测试都会临时安装 pytest。安装失败时仍必须保留
`unknown`，不得伪造失败证据。

### 其他后续项

1. 为目标训练项目恢复一个可用的 FC 执行后端后，按录屏稿从镜头 3 重新取得真实失败 Run；再验证专用 `bug-fix` 提案、Apply、验证和 Rollback。
2. 若模型再次出现额度或工具调用异常，先用“请只回复：模型连通正常。”确认上游，再检查 Gateway/Control Plane 日志；不要绕过失败证据、确认或回滚门禁。
3. 若继续开发录屏体验，优先补充“从 WebUI 选择项目后完成服务端可信登记”的可视化入口；当前信任登记需要由操作者在控制面预先完成。
