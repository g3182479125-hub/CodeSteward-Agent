# P5 Acceptance Evidence

当前状态：`review_pending`。代码和本地门禁已完成；等待本轮 Standards 与 Spec
subagent 对同一工作树给出最终结论后，才把状态改为 `complete`。

## Scope

P5 采用 nanobot 原生 WebUI 作为唯一用户入口。DevPilot 保留 loopback FastAPI
内部控制面、SQLite durable events/SSE、事件桥接和受信 Action Broker；不新增或
恢复独立 React 控制台。用户在 nanobot 对话中看到提案、Diff 摘要、阶段、终态、
错误码、Artifact 引用和回滚结果。

## Implemented safety path

- WebSocket Gateway 只接受受信 WebUI 的精确确认词 `执行`、`验证`、`撤销`。
- WebUI 只从用户显式选中的消息引用中发送完整 tuple：`change_set_id`、`plan_hash`、
  `patch_hash`、`scope_hash`、`project_id`；它不会从模型历史自动选择 tuple。
- Gateway 再向 loopback Control Plane 解析当前 ChangeSet，只有服务端 tuple 与用户
  引用一致时才在本地签名。
- `RequestContext.attributes` 只在当前 Tool 请求内携带 signer/context；hooks、
  runtime events、Session 持久化和聊天文本会过滤 `devpilot_*` 属性。
- Action Broker 验签、校验当前 trusted snapshot 和有效期，再在 capability 发行
  前持久化一次性消费；相同 action tuple 的刷新确认会被唯一约束拒绝。
- Broker 自己生成不可预测的幂等键；同一确认在执行中断后可从 durable record 恢复
  原幂等键，新的确认事件不会复用旧动作。
- Broker 终态和脱敏结果写回 `broker_actions.result_json`；审批 token 和 API key
  不进入模型、Session、WebUI、SSE 或日志。
- 生产 store 和 FastAPI 启动严格要求 Alembic head
  `0010_p5_persist_idempotency_key`；旧库不会通过 `create_all()` 静默升级。

## Verification

- DevPilot full Python suite: `205 passed, 3 skipped`.
- DevPilot focused Broker/migration suite: `17 passed`.
- DevPilot Ruff: passed.
- DevPilot Pyright: `0 errors, 0 warnings, 0 informations`.
- nanobot Gateway/BaseChannel/runtime tests: `27 passed, 1 skipped`.
- Native WebUI client has a regression test for confirmation tuple serialization.
- `git diff --check`: passed.

The skipped tests are existing Windows symlink-privilege checks and are reported as
skips, not successful security coverage. npm dependency installation was unavailable
because the registry/cache repeatedly returned corrupted tarball checksums; ADR-0003
explicitly removes frontend build/browser smoke from the P5 gate.

## Review gate

Two parallel subagents are reviewing the same current P5 worktree on the Standards and
Spec axes. Their reports must be recorded under `docs/reviews/` before P5 is marked
complete. P6 has not started.
