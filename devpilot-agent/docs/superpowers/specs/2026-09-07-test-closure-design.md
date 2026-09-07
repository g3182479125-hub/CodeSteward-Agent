# 测试收口与 FC provider-default 口径设计

## 目标

在不新增页面、Runtime、数据库或自动执行能力的前提下，收口当前测试方案的剩余阻塞：接受杭州 FC 的 `provider-default` 作为本项目的正式验证模式，并使 Windows 回归测试不因终端/子进程启动开销产生误报。

## 方案

1. **FC 口径**：继续在结果中保留 `network_policy=provider-default` 和 `network_isolation_proven=false`，用于透明告知实际能力；但不再把网络隔离证明作为 P6/P7/P8 的退出条件或阻塞项。本项目明确不提供严格断网保证，仍保留项目 allowlist、固定验证 profile、显式确认、快照上传和清理保护。
2. **Windows 回归**：不改变执行实现和用户行为。计时测试继续断言完整输出、退出码和无残留 session；仅对 Windows 使用合理的进程启动开销容差，Linux/macOS 保持原有更严格阈值。这样测试验证的是“早于会话残留/超时错误地结束”，而不是把 PowerShell 启动时间误判为功能失败。
3. **证据同步**：更新冻结计划、P6 验收记录和本轮全量测试证据，区分“已通过”“已接受边界”和“未执行”。不把 provider-default 写成网络隔离，不把未执行的人工场景伪造为通过。

## 验收

- FC `sandbox-check` 返回 `exit_code=0`，真实网页链路完成 Apply + FC Verify。
- nanobot 回归除明确标注的环境不稳定测试外全部通过；修复后的计时测试通过。
- DevPilot、WebUI source test/build/lint 和 nanobot Ruff 通过。
- 工作区 `git diff --check` 通过，敏感配置不进入仓库。
