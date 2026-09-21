# Resilient runtime Standards 增量复核

- 固定点：`ed2b28d`
- 候选：`37c8a9c`
- 范围：`git diff ed2b28d..37c8a9c`
- 审查员：`01a0ae3a-17a9-7be2-8039-92c31ab76536`
- 依据：`CURRENT_PLAN.md` 第 0/6/7 节、ADR-0004、DSH resilient-runtime R1–R5 计划
- 结论：**PASS（仅 R1–R4 增量，不代表 R5 通过）**

## 已核对

- `classify_exception` 只使用异常类型/模块和可信 `sender_id`，不再读取异常消息，
  因而普通 `RuntimeError("DevPilot ...")` 不会被误报为 DevPilot 失败。
- 真实 `DevPilotToolError`、`TaskProfileError` 和 subagent sender 的来源分类保持有效。
- 活跃 task profile 下的普通存储异常反例返回 `tool.failed`。
- R1–R4 的恢复、持久化、未知副作用重放保护和 WebUI 活动投影没有被本次增量回归
  修改或削弱。
- 证据文档记录了实现候选 `0f2abde`、当前文档提交 `37c8a9c`、实际测试命令、计数、
  退出码和已知 DevPilot 基线失败。

## 自动验证

- 定向回归：`111 passed, 3 deselected`，退出码 `0`。
- 集成回归：`379 passed, 3 deselected`，退出码 `0`。
- Ruff：通过；`git diff --check`：通过。
- WebUI 目标测试：`75 passed`；lint 和 build：通过。

## 保留边界

真实 WebUI R5 录制场景没有在本次 Standards 复核中执行，因此不能把 R5 或整体录制
门槛写成通过。
