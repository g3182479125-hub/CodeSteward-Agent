# 2026-09-27 Local Harness Cross-Machine Handoff

更新时间：2026-09-27

仓库：`https://github.com/g3182479125-hub/devflow-agent.git`

分支：`codex/h11-complete-handoff`

本次目标：继续完成 DH-01 本地 Harness 演示验收；当前整体状态 **NO-GO**，不可宣称整套录制链路已验收。

## 1. 当前范围与结论

当前新增目标只覆盖 FC 关闭时的本地 Harness 演示。按用户明确要求，**Skill 自进化与所有 FC/cloud 功能不在本次展示范围内**。本地临时 Python Tool 不是 OS 沙箱：候选代码以当前 Windows 用户权限运行，并可使用主机默认网络。演练只可使用一次性项目副本与合成数据。

已有真实现场证据：

- 同一运行进程内刷新 Skill 内容，下一轮 Agent 上下文可见更新；Gateway listener PID `86760`。
- 同一 Host 中完成内置 Loop Driver `default-react → plan-then-act → default-react` 检查点切换。
- 真实 WebUI 中完成临时 Tool 创建、固定本机校验、激活、调用、版本更新和 Run 结束清理；Session `websocket:4ce02c71-45dd-47df-b085-463ac918b772`，候选 v1.0.4，调用结果 `20`，本地校验退出码 `0`，未使用 FC。
- ShopSphere 已有一次真实 Agent ChangeSet Apply+Verify，范围仅 `shop/orders.py`；单独的本地回归 Run 执行 223 项并退出码 `0`。Apply/Verify 所绑定的档位是 `python-syntax`，不可描述成它自身执行了完整回归。

正式验收仍未通过：

1. 在独立 ShopSphere 故障副本上，Agent 的 `devpilot_test_run` 返回 `HTTP 409: PROJECT_TEST_INVALID`，没有 Run/ChangeSet，也没有改文件；根因缺少可用的项目注册/信任路径尚待解决。
2. disposable clone 上的 Rollback/hash 演练尚未完成。
3. 临时 Tool 的跨会话隔离只有自动化断言，尚无第二个实时 WebUI 会话证据；Run 清理返回 `removed=3`，但仅列出两条 route，数量差异尚未解释。
4. Nanobot 全量测试 `6077 passed, 42 skipped, 18 failed`，退出码 `1`。失败分类为 10 个旧英文文案断言、1 个 media fixture 缺少 `plugin_manager`、7 个 MCP/WebFetch 网络或重定向断言。尚未在精确基线 SHA 上做对照，不得称它们已证明为预存失败。
5. DevPilot 全量测试为 `577 passed, 5 skipped`；受影响定向回归 `278 passed, 1 skipped`；Ruff 与 `git diff --check` 通过。Standards review PASS；Spec review 确认 Run 绑定符合规格，但整体 DH-01 仍 NO-GO。

换机交接前于仓库内专用 pytest basetemp 再次复跑：`test_local_execution.py` 与 `test_temporary_tools.py` 退出码 `0`（有 1 项 skip）；受影响的 Nanobot 四组测试为 `229 passed, 2 failed`。这两项失败分别是 `test_streamed_flag_not_set_on_llm_error` 与 `test_next_turn_after_llm_error_keeps_turn_boundary`，测试仍断言英文错误文本，但当前运行时按设计返回本地化 fallback。第一次用默认 `%TEMP%` 跑 pytest 时遇到 `WinError 5`，随后切换到仓库专用 basetemp；不要把该 fixture 初始化权限错误当作产品失败。

本次实现细节、命令和证据以以下文件为准：

- [`2026-09-27-local-only-harness-demo-plan.md`](../superpowers/plans/2026-09-27-local-only-harness-demo-plan.md)
- [`2026-09-27-local-only-harness-demo-design.md`](../superpowers/specs/2026-09-27-local-only-harness-demo-design.md)
- [`recording-script-natural-verified.md`](../recording-script-natural-verified.md)
- [`CURRENT_PLAN.md`](../../devpilot-agent/docs/plans/CURRENT_PLAN.md) §11

## 2. 本次同步的代码范围

本次提交包含 DH-01 本机固定校验档位、local-only 临时 Tool receipt 与生命周期、Runtime 热刷新/Driver 检查点相关实现和测试，以及计划、录制稿和本交接文档。基线为 `ff524bb`；应以远端分支拉取后的最新 SHA 为准。

个人运行目录、`.nanobot/tool-results`、数据库、会话、日志、临时测试目录、虚拟环境、API Key 和其他本机模型配置都不属于交接内容，未上传。

## 3. 换电脑拉取与恢复

在新电脑 PowerShell 执行：

```powershell
git clone -b codex/h11-complete-handoff https://github.com/g3182479125-hub/devflow-agent.git
Set-Location .\devflow-agent
git log -1 --oneline
Get-Content .\docs\handoffs\2026-09-27-local-harness-cross-machine-handoff.md
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
Set-Location .\nanobot\webui
npm install
npm run build
Set-Location ..\..
```

如果仓库已克隆：

```powershell
git status --short
git pull --ff-only origin codex/h11-complete-handoff
```

先按新电脑的实际路径和信任配置启动本地 Harness；不要复制旧电脑的数据库、密钥、会话、工具输出或运行状态。模型 API 配置、`DEVPILOT_HARNESS_SELECTION_INTEGRITY_KEY`、允许项目根目录等只在新电脑本地设置，严禁提交到 Git。

## 4. 推荐续做顺序

1. 阅读本交接文档、DH-01 spec、plan、`CURRENT_PLAN.md` §11 及仓库 `AGENTS.md`，检查 `git status`，确认没有把本机数据带进工作区。
2. 先复现并定位独立 ShopSphere 挑战路径的 `PROJECT_TEST_INVALID`。只调整受信项目的登记/信任配置或修复确切入口问题，不要绕过控制面，也不要手工改挑战项目源码。
3. 让 Personal Agent 在挑战副本上正式完成：只读检查 → 本地测试复现 → ChangeSet/Diff → 明确确认 → Apply → Verify/完整回归。记录真实 Run/ChangeSet ID 和文件哈希。
4. 在另一个 disposable clone 演练 Rollback，并验证哈希恢复；不可在唯一挑战基线上做破坏性演练。
5. 用两个真实 WebUI 会话验证临时 Tool 只对创建会话可见；调查清理 `removed` 数与 route 清单不一致，补自动化和现场证据。
6. 对 18 个 Nanobot 全量失败，在精确基线 SHA 上做 A/B 或逐项明确处置；不能仅凭测试路径未改就认定为 pre-existing。
7. 更新证据与录制稿；按仓库 ADR-0004，对固定候选 SHA 做 Standards/Spec 双轴 review，并完成增量复核。只有所有 DH-01 验收项通过后才能把整体状态从 NO-GO 改为可录制。

## 5. 录制边界

准确说明：“本机执行（非 OS 沙箱；候选代码继承当前 Windows 用户权限）。”不演示 Skill 自进化，不调用 FC，不把本地测试说成 OS 隔离；未完成的跨会话隔离、Rollback 或全量测试项不能剪辑成已验收。
