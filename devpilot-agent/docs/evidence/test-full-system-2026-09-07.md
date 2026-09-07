# DevPilot 测试方案 v1 执行记录（2026-09-07～2026-09-08）

## 范围与环境

- 测试依据：`E:/download/DevPilot_测试方案_v1.md`。
- 被测系统：nanobot WebUI + DevPilot 本地控制平面 + DeepSeek + Alibaba FC Agent Sandbox。
- 运行位置：E 盘独立数据目录；真实项目之外使用合成项目 `E:/codex-local/p6-webui-contract-demo`。
- FC：杭州区域，模板 `code-interpreter-v1`，网络策略 `provider-default`。
- 安全边界：本次没有把 API Key、SQLite 数据库、日志或虚拟环境写入仓库。

## 已完成的真实网页主场景

1. 在 nanobot 网页中要求对合成项目只读检查，确认 `pricing.py` 的百分比折扣分母错误。
2. 网页展示只读证据、ChangeSet 和单行 Diff；未发生文件写入。
3. 在同一聊天中明确回复“执行”。该确认通过 `devpilot_confirmed_action(apply_verify)`，没有绕过受信动作通道。
4. Apply 成功，`pricing.py` 从 `Decimal("10")` 修复为 `Decimal("100")`。
5. 控制平面使用真实 FC 沙箱验证，返回 `verified`、`exit code 0`、约 3.1 秒。
6. 网页收到完成结果，显示 ChangeSet、Apply、Verify、沙箱后端和最终 SHA-256。
7. 在本地重新执行合成项目单元测试：2 个测试通过。

本次 ChangeSet：`b0257b43-4aea-4f5d-a483-22150ac7d13c`，最终状态：`verified`。

## 关键异常与结论

- 第一次网页确认时，nanobot 重启后旧的内存确认上下文已失效，安全地返回 `APPROVAL_BROKER_UNAVAILABLE`，没有 Apply 副作用。重新建立提案后，确认上下文保持在同一进程内，主链路通过。
- 第二次执行前，控制平面进程没有继承 FC 环境，因此返回 `EXECUTION_BACKEND_UNAVAILABLE`；重启控制平面并继承 FC 配置后通过。该问题是启动编排问题，不是 FC API 连接问题。
- FC `sandbox-check` 和网页真实验证均成功，证明杭州 FC 的创建、上传、执行和清理链路可用；但 `provider-default` 不证明网络隔离。测试结果不得标记为“已验证断网”。

## 自动化回归

- DevPilot 主测试集：在 nanobot 测试环境中设置 `PYTHONPATH` 指向 nanobot 源码和 DevPilot `src` 后，完整测试集退出码 `0`。
- nanobot 全回归（本轮正确环境）：命令使用仓库外临时目录，且设置跨仓库 `PYTHONPATH`；退出码 `0`，`5878 passed, 42 skipped`，用时 `302.63s`。命令如下：

  ```powershell
  $env:PYTHONPATH='E:\codex-local\nanobot-devpilot-agent\devpilot-agent\src'
  E:\codex-local\nanobot-state\nanobot-test.venv\Scripts\python.exe -m pytest -q --basetemp E:\codex-local\test-tmp-nanobot-closure-final2
  ```

- 另一次错误的全量命令曾因未设置跨仓库 `PYTHONPATH` 报 6 条导入失败；修正环境后 7 条相关测试全部通过，未作为当前回归结果统计。
- 回归中发现并修复 2 条 Windows CLI 诊断输出契约问题：Rich 终端宽度会截断 `config.json` 和 `Settings → Models`；改为稳定的纯文本诊断后，相关 2 条测试 `2 passed`，Ruff 通过。
- WebUI：源代码测试 74 条通过，构建和 lint 通过；已重新构建并部署 bundled dist 后执行真实网页测试。
- 静态类型检查：DevPilot/nanobot 当前仍有历史 optional-dependency 和严格类型告警，不能作为本次功能通过依据。

## 按测试计划的当前状态

| 区域 | 状态 | 证据/边界 |
|---|---|---|
| P6-01 提案、Diff、确认、Apply/Verify 绑定 | 通过 | 真实网页主场景；合成项目 2 个测试通过 |
| P6-02 FC 创建/上传/执行/清理 | 通过（provider-default） | 杭州 FC 真实执行；网络隔离不属于本项目目标 |
| P6-03 nanobot WebUI 主链路 | 通过 | 只读提案、聊天确认、Apply、FC Verify、结果回传 |
| P6-03 重启后的旧确认继续执行 | 安全拒绝 | 无副作用；当前确认上下文为进程内短生命周期设计 |
| P6-04 证据、审计、健康状态 | 通过（当前范围） | ChangeSet/run 状态、后端目录、真实网页和健康检查均有记录 |
| P7-01/P7-02/P7-03 | 通过（既有验收 + 自动化回归） | nanobot 全回归和阶段验收文档通过；P7 仍不代表 LLM 质量保证 |
| P8-01/P8-02 | 通过（既有验收 + 自动化回归） | 阶段验收文档和 nanobot 全回归通过；P8-03 仍保持可选、未启用、未实现 |

## 明确不在本次范围的增强项

网络隔离保证、真实 React 执行和无人值守自动修复不属于当前交付目标。跨进程供应商运行状态查询已由本轮补齐：运行元数据写入脱敏本地快照，服务重启后按 `sandbox_id` 显式调用 `connect`，已结束运行复用持久化结果，不可查询时返回 `unknown`，不创建替代任务、不重放命令。项目仍保留固定 profile、allowlist、显式确认、快照范围和清理失败关闭等边界。

## 重现入口

```powershell
cd E:\codex-local\nanobot-devpilot-agent\devpilot-agent
uv sync --extra fc-sandbox
uv run devpilot sandbox-check
```

启动 nanobot 前必须让同一进程继承 FC 配置和控制平面地址；杭州环境使用 `DEVPILOT_FC_NETWORK_POLICY_MODE=provider-default`，不要把它写成已完成网络隔离验证。
