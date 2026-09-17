# H7/H8 不确定回合恢复修正证据

## 结论

这不是新的 H 节点，也不是对冻结范围的扩大，而是对已完成 H7/H8 实现的一次维护性
修正：旧工具调用结果不确定时，系统仍然保护旧副作用，但不再把后续所有用户消息都
当成同一个失败回合而暂停。

## 计划对照

- 冻结计划基线：`CURRENT_PLAN.md` 3.9，最终候选 `fd7de83`。
- 实现修正批次基线：`64685b1`。
- 修正后的代码候选：`331beaa`。
- 审查结论记录：`6abc5c2`。
- 适用契约：`LIVE_HARNESS_EXTENSION.md` H7/H8、CURRENT_PLAN §0/§6/§7、ADR-0004。

## 实际行为

| 场景 | 当前行为 | 安全边界 |
|---|---|---|
| 旧回合结果未知，用户发送普通问题/只读问题 | 作为新的 Host 回合继续处理 | 不自动重放旧工具调用 |
| 用户查询状态或恢复旧运行 | 允许查询/恢复路径推进 | 只消费已持久事实，不凭摘要猜测 |
| 新回合再次提交与旧调用完全相同的工具及参数 | 在 Host 工具边界阻断 | 旧副作用不会因为新回合而重做 |
| 旧检查点没有持久 replay fingerprint | 对工具调用 fail-closed | 不能用临时指纹推断“安全可执行” |
| 持久回合预算已为 0 | 新回合仍可启动本地预算 | 持久 checkpoint 预算不会增加 |
| 只有一个与终态查询匹配的旧工具意图 | 可清理对应旧意图 | 多个或混合未知意图继续保留保护 |
| 用户明确输入 `/new` | 清理 H8 owner 和恢复标记 | 这是开始全新任务的显式入口 |

## 实现落点

- `nanobot/nanobot/agent/loop.py`：区分新回合与旧不确定操作，恢复时重建 Host
  checkpoint，并在安全边界清理恢复标记。
- `nanobot/nanobot/agent/loop_driver.py`：持久化旧工具调用指纹，按业务引用和未结算
  意图数量决定是否可结算旧意图。
- `nanobot/nanobot/agent/loop_handoff.py`：保留 H8 owner generation/CAS 和持久预算
  单调性，允许安全的新 Host 回合接续。
- `nanobot/nanobot/agent/runner.py`：在工具边界阻断精确旧调用；对缺少持久指纹的
  legacy checkpoint fail-closed。
- `nanobot/nanobot/command/builtin.py`：`/new` 显式清理 H8 所有权和恢复标记。

## 自动化证据

在 `nanobot/` 目录执行：

```text
python -m pytest -q tests/agent/test_loop_save_turn.py tests/agent/test_loop_driver.py tests/agent/test_loop_handoff.py -k "not h9_persisted_loop_selection_starts_only_a_new_task_owner"
126 passed, 1 deselected

ruff check nanobot/agent/loop.py nanobot/agent/loop_driver.py nanobot/agent/loop_handoff.py nanobot/agent/runner.py tests/agent/test_loop_driver.py tests/agent/test_loop_handoff.py tests/agent/test_loop_save_turn.py
All checks passed

git diff --check
passed
```

测试覆盖普通消息继续、精确旧调用阻断、legacy fail-closed、零持久预算恢复、终态查询
结算、混合未知意图保留、H8 持久化以及 `/new` 重置。

## 双轴审查

- Standards：`devpilot-agent/docs/reviews/2026-09-17-uncertain-turn-standards-review.md`，
  reviewer `01a0ae3a-17a9-7be2-8039-92c31ab76536`，候选 `331beaa`，结论 PASS。
- Spec：`devpilot-agent/docs/reviews/2026-09-17-uncertain-turn-spec-review.md`，
  reviewer `01a0ae3a-18af-7472-a602-90ee1e15964f`，候选 `331beaa`，结论 PASS。

两份报告均以 CURRENT_PLAN §0/§6/§7 和 LIVE_HARNESS_EXTENSION H7/H8 为检查范围，
没有遗留 H7/H8 规范阻断。

## 明确限制

当多个旧业务引用或多个未知工具意图无法唯一对应时，系统不会替用户猜测哪一个已经
完成；这些意图继续保持 query-only/保护状态。需要彻底开始新任务时，用户应使用
`/new`。这项保守行为是安全边界，不是普通聊天被全局锁死。
