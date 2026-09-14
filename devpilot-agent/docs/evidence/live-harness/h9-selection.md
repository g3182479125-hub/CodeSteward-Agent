# H9：基于任务反馈选择 Harness 版本

日期：2026-09-14

## 已交付

- 候选插件与 Loop 使用同一持久化选择账本：证据、候选、24 次固定评测、确认和激活记录均绑定项目、工作区、版本哈希与预算。
- 固定评测由宿主拥有：每个目标必须先注册六组输入和预期输出；候选只能被执行，不能定义输入、通过规则或替换评测契约。
- 每个结果持久化 `fixture_contract_hash`，该哈希覆盖目标 ID、实际输入和预期输出。Loop 的真实 projection 与其 oracle 契约调用同一个宿主函数，避免两套测试数据漂移。
- 无改善、回归、预算超限、契约/权限违规、基础设施失败都不会晋升；确认只能消费一次，并绑定候选、评测结果、用户、会话和作用域。
- 已选中的 Loop 只在新任务取得 owner 时生效；选择期间已经运行的 H8 owner 继续完成原任务，不被强制换 Loop。

## 验收映射

| 验收 | 证据 |
| --- | --- |
| H9-A | `test_h9_default_host_oracle_rejects_buggy_calculator_and_accepts_repair`：错误折扣计算与修复版本进行 24 次固定比较，只有修复版本可进入确认。 |
| H9-B / H9-E | `test_h9_harness_selection.py` 覆盖无改善、回归、预算、固定套件篡改、确认重放/并发、版本持久引用。 |
| H9-C | `test_h9_default_host_oracle_evaluates_a_remote_loop_repair`：Loop 候选以六组不同 projection 走同一 24 次固定门禁。 |
| H9-D | `test_h9_persisted_loop_selection_starts_only_a_new_task_owner`：旧会话先建立 default owner，激活选择后仍保持 default；新会话才得到 `plan-then-act`。 |

## 本轮回归

```powershell
$env:PYTHONPATH='E:\codex-local\nanobot-devpilot-agent\nanobot;E:\codex-local\nanobot-devpilot-agent\devpilot-agent\src'
& 'E:\codex-local\nanobot-devpilot-agent\nanobot.venv\Scripts\python.exe' -m pytest `
  devpilot-agent/tests/test_p2a_migration.py `
  devpilot-agent/tests/test_h5_candidate_runner.py `
  devpilot-agent/tests/test_h6_model_plugins.py `
  devpilot-agent/tests/test_h8_remote_strategy_catalog.py `
  devpilot-agent/tests/test_h9_harness_selection.py `
  devpilot-agent/tests/test_h9_harness_oracle.py `
  nanobot/tests/agent/test_loop_handoff.py `
  nanobot/tests/command/test_plugin_commands.py -q
```

结果：通过。本轮还对 H9 影响文件运行 Ruff，结果通过。

## 边界

H9 验证的是宿主固定夹具、候选执行边界和持久化选择协议；`required` 且请求禁网的 FC 执行可提供可信安全回执，允许进入 H9 晋升。杭州的 `provider-default` 模式不会伪造该回执，因而 H9 会拒绝晋升。真实浏览器演示、跨机器恢复演练与最终交付清单属于 H10，不能以本文件替代。
