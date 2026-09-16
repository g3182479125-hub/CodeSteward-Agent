# 2026-09-17：录屏主线与 FC 验证修复交接

## 当前分支与交接范围

- 仓库：<https://github.com/g3182479125-hub/devflow-agent>
- 分支：`codex/h11-complete-handoff`
- 本次提交包含：FC 项目测试兼容、WebUI 搜索不阻塞事件循环、自然语言 Bug-fix 证据门槛修复、录屏稿和相应回归测试。
- 不包含：API Key、`.env`、SQLite、日志、临时 pytest 目录、训练项目目录，以及 `nanobot/webui/package-lock.json` 的既有本地变更。

## 本次完成内容

### 1. 真实 FC 项目测试

- FC Code Interpreter 的项目测试改为使用 `python -m pytest -q`；若镜像没有 pytest，自动运行仅支持零参数 `test_*` 函数的无依赖测试运行器。
- FC 命令从上传项目根目录执行，避免上传代码无法被项目测试导入。
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

```powershell
$py = '.\devpilot-agent\.venv\Scripts\python.exe'
& $py -m pytest devpilot-agent/tests/test_p4_bug_fix.py devpilot-agent/tests/test_p4_feature_delivery.py devpilot-agent/tests/test_agent_test_runs.py -q
# 58 passed

& $py -m ruff check devpilot-agent/src/devpilot/workflows.py devpilot-agent/tests/test_p4_bug_fix.py devpilot-agent/tests/test_p4_feature_delivery.py
# All checks passed
```

真实 FC 隔离演示验证已完成：

1. 未修复副本的测试失败（`exit_code=1`）；
2. 专用 `bug-fix` 提案生成成功，绑定分析和失败证据；
3. Apply 后 FC `python-run` 验证通过；
4. FC 项目测试通过（pytest 缺失时自动走无依赖 runner，`exit_code=0`）；
5. Rollback 成功；再次 FC 项目测试恢复失败基线（`exit_code=1`）。

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

本机验证用的 `recording-unknown-bug-demo` 位于状态工作区之外，不会随 Git 提交。换电脑录制前，需要按录屏稿准备一个独立、未修复的 Python 项目：README、`pricing.py` 和至少一个失败测试即可。不要把问题答案告诉 Agent；只在录制前由操作者将该项目加入 AllowedRoots、FC 白名单并登记为可信项目。

## 下一步建议

1. 在新机器按录屏稿从镜头 1 开始做一次 WebUI 现场复验。
2. 若模型再次出现额度或工具调用异常，先用“请只回复：模型连通正常。”确认上游，再检查 Gateway/Control Plane 日志；不要绕过失败证据、确认或回滚门禁。
3. 若继续开发录屏体验，优先补充“从 WebUI 选择项目后完成服务端可信登记”的可视化入口；当前信任登记需要由操作者在控制面预先完成。
