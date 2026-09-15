# 2026-09-15：H11 完成与跨机器开发交接

## 当前结论

冻结范围已经完成：P0–P8、H1–H11 均有对应实现、验收或历史验收记录。H11-A–E
已完成，最终代码候选为 `fd7de83`；文档闭环提交为 `1cd2d21`。本交接文档用于
另一台电脑继续开发，不表示扩大既有范围或重新开启已完成节点。

状态的权威来源：

- [CURRENT_PLAN](../plans/CURRENT_PLAN.md)：P0–P8 与 H1–H11 总状态、验收规则和开发流程。
- [LIVE_HARNESS_EXTENSION](../plans/LIVE_HARNESS_EXTENSION.md)：H1–H11 的冻结契约和状态表。
- [H11 证据](../evidence/live-harness/h11-autonomy-policy.md)：H11-A–E 现场证据、限制和验证命令。

## 最近提交

从 H11 冻结基线 `3b37fd0` 到当前候选的关键提交：

| 提交 | 内容 |
|---|---|
| `62540b5` | H11 候选工具自主策略与初始证据 |
| `d13dff8` | 首轮 Standards/Spec 发现的批量修复，补齐生命周期审计、H9 隔离和撤销路由 |
| `f75b856` | 重启后安全降级、撤销停止候选路由、清理审计加固 |
| `fd7de83` | remove、清理重试、租约超时和重启恢复路径保留 H11 审计字段 |
| `1cd2d21` | H11 证据、计划状态和最终双轴复核报告闭环 |

## H11 已交付内容

- 可信来源只使用 WebUI/Gateway 的 `workspace_scope.access_mode`：`restricted` 或 `full`。
- `CandidateAutonomyPolicy` 统一决定 `deny/confirm/auto/notice`；不新增权限按钮。
- `restricted`：define + fixture trial 自动执行；首次真实数据调用合并一次确认。
- `full`：合规、已试用的纯计算候选可自动调用，并显示非阻塞 notice。
- 敏感字段、越界路径、超预算/超时、未试用、内容变化和陈旧路由均 fail-closed。
- 候选只在当前 session/task 临时存在；停止、撤销、会话结束、清理和重启恢复均有审计和租约处理。
- H11 不绕过 Apply/Verify/Rollback、P8 Skill 确认、OAuth/payment 或 native shell 门禁。

## Review 与验证

首轮 Standards/Spec 发现已集中修复；同一对 reviewer 对 `f75b856...fd7de83` 做最终增量复核，
两轴均为 PASS：

- [Standards 最终复核](../reviews/h11-standards-recheck-fd7de83.md)
- [Spec 最终复核](../reviews/h11-spec-recheck-fd7de83.md)
- [首轮 Standards 报告](../reviews/h11-standards-review-62540b5.md)
- [首轮 Spec 报告](../reviews/h11-spec-review-62540b5.md)

已记录的验证结果：

```powershell
cd nanobot
$env:PYTHONPATH='.;..\devpilot-agent\src'
.venv\Scripts\python.exe -m pytest ..\devpilot-agent\tests -q
# exit 0
.venv\Scripts\python.exe -m pytest ..\devpilot-agent\tests\test_h5_candidate_runner.py ..\devpilot-agent\tests\test_h6_model_plugins.py ..\devpilot-agent\tests\test_h11_candidate_autonomy.py -q
# 66 passed
.venv\Scripts\python.exe -m pytest tests/agent/test_plugin_contracts.py tests/agent/test_plugin_h3.py tests/agent/test_plugin_h4.py tests/agent/test_plugin_manager.py tests/agent/test_workspace_scope.py tests/channels/test_websocket_devpilot_confirmation.py -q
# 68 passed
```

另有 WebUI/WS 相关集合 186 passed；Ruff 与 `git diff --check` 通过。全量 Pyright 仍有既存的
跨项目严格类型错误，不能把它误记为 H11 回归失败或完成条件已消失。

## 真实能力边界

- FC 证据采用项目明确接受的 `provider-default`；该模式不等于网络隔离，不能对外宣称断网保证。
- 最终增量 review 没有重复启动真实 WebUI/FC；H11-E 使用本日已记录的真实 WebUI + 既有 FC 现场证据。
- 现有未跟踪目录 `nanobot/.pytest-h8-current/` 与 `nanobot/MagicMock/` 属于用户环境，未提交、不得删除。

## 另一台电脑接手步骤

1. 拉取分支并确认提交链：`git fetch origin`，检查 `git log --oneline --decorate -8`。
2. 阅读本文件、根目录 `AGENTS.md`、`CURRENT_PLAN.md`、`ADR-0004` 和 `LIVE_HARNESS_EXTENSION.md`。
3. 不重做 P0–P8 或 H1–H11；新需求先新增明确节点和验收条件。
4. 若修改代码，按 AGENTS/ADR-0004：受影响测试 → 固定候选 → Standards/Spec 并行 review → 批量修复 → 原 reviewer 增量复核。
5. 集成测试使用独立环境；密钥、数据库、日志、虚拟环境和生成目录保持未跟踪。

当前分支应为 `codex/h11-complete-handoff`，对应远端同名分支；继续开发可从该分支创建新的 `codex/` 分支。
