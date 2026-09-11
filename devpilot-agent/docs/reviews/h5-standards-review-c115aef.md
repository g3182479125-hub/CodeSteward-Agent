# H5 Standards Review：c115aef

## 复核信息

- 复核类型：Standards / 工程规范增量复核
- 复核员 agent：`01a08f4a-cd23-7581-a383-09a6d5e78557`
- 基线：`0303533`
- 候选：`c115aef`
- 范围：仅 H5 候选 runner、FC 候选执行标记和关联测试；未审 H6。

## 结论

**NEEDS_FIX**

## 条款结果

| 条款 | 结果 | 说明 |
|---|---|---|
| H5-A | PASS | 固定 FC runner、JSON 输入输出、30 秒和 64 KiB 约束已覆盖。 |
| H5-B | PASS | 超时、异常、输出超限、FC 不可用和清理未确认均显式失败，不回退宿主。 |
| H5-C | NEEDS_FIX | 输入值做了敏感检测，但候选源码仍未检测本机路径引用；AST 检查不能被当作沙箱。 |
| H5-D | NEEDS_FIX | 请求级 Grant 有作用域校验，但生产代码未接入真实 ProjectStore/Host issuer。 |
| H5-E | PASS（现场证据） | 已有 provider-default FC 成功和 cleanup 记录；不得宣称网络隔离。 |
| H5-F | NEEDS_FIX | 结果重放已覆盖；跨进程预算仍是读改写，缺少全局一次试运行并发租约和未知状态恢复。 |

## 阻断项

1. `store` 可选，无 store 时使用内存预算/操作记录；需要生产路径强制持久化，且崩溃后
   查询已有 run/sandbox 状态，不把 in-progress 永久当作失败或自动重试。
2. 预算持久化当前不是跨进程原子增量，需要事务/CAS/持久租约，避免丢失更新和突破共享预算。
3. 损坏的预算记录不能静默重置为零，应 fail-closed 并报告持久化错误。
4. 源码本机路径检测和严格类型检查仍需补充/确定可复现基线。

## 已核对命令

- H5、FC、P3 定向 pytest：通过。
- Ruff：通过。
- `git diff --check`：通过。
- 本轮未重新发起真实 FC；可复用的现场记录见 `docs/evidence/live-harness/h5-candidate-execution.md`。
