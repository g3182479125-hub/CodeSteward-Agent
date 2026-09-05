# P5 Acceptance Evidence

当前状态：`complete`。代码、本地门禁以及同一候选提交上的 Standards 与
Spec 并行 subagent review 均已通过。

## Scope

P5 采用 nanobot 原生 WebUI 作为唯一用户入口。DevPilot 保留 loopback FastAPI
内部控制面、SQLite durable events/SSE、事件桥接和受信 Action Broker；不新增或
恢复独立 React 控制台。用户在 nanobot 对话中看到提案、Diff 摘要、阶段、终态、
错误码、Artifact 引用和回滚结果。

## Implemented safety path

- WebSocket Gateway 只接受受信 WebUI 的精确确认词 `执行`、`验证`、`撤销`。
- 确认上下文使用 Gateway 生成的服务端线程 UUID；ChangeSet 在进程内绑定首次确认线程，
  客户端不能通过修改 chat/turn 标识跨线程复用。
- WebUI 只从用户显式选中的消息引用中发送完整 tuple：`change_set_id`、`plan_hash`、
  `patch_hash`、`scope_hash`、`project_id`；它不会从模型历史自动选择 tuple。
- Gateway 再向 loopback Control Plane 解析当前 ChangeSet，只有服务端 tuple 与用户
  引用一致时才在本地签名。
- `RequestContext.attributes` 只在当前 Tool 请求内携带 signer/context；hooks、
  runtime events、Session 持久化和聊天文本会过滤 `devpilot_*` 属性。
- 提案工具把完整 tuple 只交给当前 turn 的内部投递元数据；Gateway 只登记本 chat
  已产生的服务端提案，确认时拒绝未登记、跨 chat 或被替换的 tuple。
- 提案后的模型指引统一进入 `devpilot_confirmed_action`；不再把用户引向只创建
  `approval_pending` 的旧 `devpilot_request_approval` 工具。
- Action Broker 验签、校验当前 trusted snapshot 和有效期，再在 capability 发行
  前持久化一次性消费；相同 action tuple 的刷新确认会被唯一约束拒绝。
- Broker 自己生成不可预测的幂等键；同一确认在执行中断后可从 durable record 恢复
  原幂等键，新的确认事件不会复用旧动作。
- Broker 终态和脱敏结果写回 `broker_actions.result_json`；审批 token 和 API key
  不进入模型、Session、WebUI、SSE 或日志。
- 生产 store 和 FastAPI 启动严格要求 Alembic head
  `0010_p5_persist_idempotency_key`；旧库不会通过 `create_all()` 静默升级。

## Verification

- DevPilot full Python suite: `208 passed, 3 skipped`.
- DevPilot focused P5 suite: `39 passed` (`23` API contract + `16` Action Broker).
- DevPilot Ruff: passed.
- DevPilot Pyright: `0 errors, 0 warnings, 0 informations`.
- nanobot Gateway/WebSocket/runtime and AgentLoop context integration tests: `26 passed`.
- Native WebUI client has a regression test for confirmation tuple serialization.
- `nanobot/tests/agent/test_loop_tool_context.py` provides fake tool-loop coverage proving
  trusted attributes reach one tool turn while remaining absent from runtime events and
  Session persistence; provider-backed live chat was not available in this environment.
- `git diff --check`: passed.

The skipped tests are existing Windows symlink-privilege checks and are reported as
skips, not successful security coverage. npm dependency installation was unavailable
because the registry/cache repeatedly returned corrupted tarball checksums; ADR-0003
explicitly removes frontend build/browser smoke from the P5 gate.

The repository-wide nanobot run also reported one existing timing-sensitive failure in
`tests/tools/test_exec_session_tools.py`; the P5-specific nanobot integration set remains
green and no P5 file is involved in that failure.

## Review gate

两份并行 subagent review 已分别记录在 `docs/reviews/` 并通过。P5 已完成；
P6 尚未开始。
