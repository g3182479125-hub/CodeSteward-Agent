# ADR-0003：统一使用 nanobot 原生前端

## 状态

已采纳（2026-09-04）。本 ADR 是冻结计划 v2.2.0 的受控产品范围修订；v2.2.0
原文保留为历史基线，不原地改写。后续开发必须同时读取 v2.2.0 与本文，本文对
前端、P5/P7 演示和用户交互的规定优先。

## 决策

DevPilot 从头到尾只使用 nanobot 自带的 WebUI 作为用户界面。DevPilot 不再维护、
启动或交付独立 React 控制台、审批页面、Dashboard、Projects、Diff、Run Timeline、
Artifacts 或 Security 页面。

DevPilot 的 FastAPI 控制面仍然保留，但它是 nanobot Tool Adapter 和受信任本地动作
Broker 使用的 loopback 内部服务，不是面向真实用户的第二个产品入口。DevPilot 的持久
事件/SSE 仍然保留为内部事件源，由 nanobot 适配层转换为 nanobot WebUI 可见的结构化
阶段消息；如果 nanobot 原生通道已经提供流式传输，则复用该通道，不修改 nanobot 核心
Agent Loop。

## 用户可见闭环

```text
nanobot WebUI
  → nanobot Tool：分析 / 计划 / Diff 摘要
  → nanobot WebUI：风险、文件、验证项、停止条件
  → 用户在同一聊天中明确确认
  → 受信任本地动作 Broker：在进程内申请并立即消费 capability
  → DevPilot 控制面：Apply / Verify / Rollback
  → DevPilot 事件 → nanobot WebUI：阶段、结果、失败、证据和下一步
```

Capability token 永远不能进入模型消息、Tool 返回值、Session、nanobot WebUI 文本、
URL、SSE payload、日志或 Artifact。用户的聊天确认不是把 token 交给模型；它只触发
受信任的本地 Broker。若当前 nanobot 适配方式不能同时满足“聊天确认”和“token 不出
受信边界”，动作必须拒绝并返回 `APPROVAL_BROKER_UNAVAILABLE`，不能退回独立页面或
要求模型携带 token。

## Action Broker 安全契约（后续实现的硬性要求）

1. **确认来源与签名**：`user_confirmation` 字符串本身不具备授权能力。nanobot
   Gateway 在进程启动时用 CSPRNG 生成一次性的 32-byte `gateway_key`，仅保存在
   Gateway/Broker 可信进程内存中，进程重启即轮换且不落盘。原生 WebUI 的用户事件
   由 Gateway 在 Tool 调用上下文中 out-of-band 附加，不进入模型可控 JSON 参数。
   它必须包含 `session_id`、`thread_id`、单调递增 `user_message_seq`、
   `confirmation_id`、`issued_at`、`expires_at` 和 `user_event_nonce`；Broker 只接受
   Gateway 对下列换行规范化 canonical string 的 HMAC-SHA256：
   `v1\nsession_id\nthread_id\nuser_message_seq\nconfirmation_id\nchange_set_id\nplan_hash\npatch_hash\nscope_hash\nproject_id\ncapability\nissued_at\nexpires_at\nuser_event_nonce`。
   模型生成的 Tool 参数、Prompt 文本或复制的“执行”字样不能伪造该上下文。nanobot
   没有可信上下文或 HMAC 校验失败时，Broker 必须返回 `APPROVAL_BROKER_UNAVAILABLE`
   并拒绝 R1/R2。
2. **请求/响应协议**：当前 Broker 必须是 nanobot 进程内的受信模块，不开放 HTTP、
   WebSocket、命名管道或其他 IPC；因此不会产生第二个可伪造入口。固定的内部类型为
   `ConfirmationContext`（上项字段 + `signature`）、`BrokerActionRequest`
   （`context`、`action`、`change_set_id`、`idempotency_key`）和
   `BrokerActionResult`（`status`、`change_set_id`、`action`、`run_id`、`error_code`、
   `result_ref`），均使用严格 schema 和 `additionalProperties=false`。未来若必须拆分
   进程，必须另立 ADR 冻结 Windows named pipe/Unix socket 身份、ACL、密钥轮换和协议，
   不能自行降级为 HTTP。
3. **精确绑定**：Broker 根据 `confirmation_id + action + change_set_id + plan_hash +
   patch_hash + scope_hash + project_id + session_id + thread_id + user_message_seq`
   建立 pending record；`action` 只能是 `apply|verify|rollback`，三者分别确认。执行前
   再从 DevPilot 读取当前 ChangeSet、校验全部哈希、Project trust、能力和有效期。Broker
   自己生成不可预测的 `idempotency_key`，并把它绑定到完全相同的 canonical action tuple；
   模型不得传入或覆盖该 key。
4. **防重放/并发**：`confirmation_id`、`user_event_nonce` 和 capability grant 均一次性；
   数据库用条件更新消费，重复、过期、撤销、跨线程/跨项目、哈希变化或并发第二次调用
   全部拒绝，且不启动 Apply/Verify/Rollback。确认上下文最大有效期为 5 分钟，超过期限
   必须重新产生用户事件。
5. **秘密边界**：明文 token 只存在 Broker 可信进程内存的单次调用窗口，不落盘、不进
   nanobot Tool 返回、模型上下文、Session、聊天文本、SSE、URL、日志或 Artifact；
   数据库只保存 token 摘要、HMAC 校验结果和脱敏 action tuple。Broker 异常、重启或
   token 丢失后必须重新确认。
6. **终态与最小暴露**：每个动作开始前记录授权事件，结束后记录脱敏终态；nanobot
   WebUI 只看到计划、Diff 摘要、阶段、错误码、Artifact 引用和下一步，看不到本地绝对
   路径、凭据、原始执行输出或 capability token。事件桥接丢失时只能读取持久快照，不得
   把“未收到事件”解释成成功。

本文冻结的契约已由 nanobot-only P5 实现并通过对应测试；R1/R2 仍不得通过模型
参数直接执行，必须经过 Gateway 服务端 ChangeSet 校验、用户显式引用和 Broker。

## 对冻结计划的具体修订

1. 第 1 节“控制台”角色删除；React/TypeScript 不再是产品运行时或学习目标。FastAPI
   仅作为内部控制面适配器。
2. 第 2.2 节依赖顺序固定为：workspace Skill → nanobot Tool entry point →
   DevPilot Domain/Application → loopback control plane；删除 `API/UI` 中的独立 UI
   含义。
3. 第 2.3 节不再要求 React 前端目录；`web/` 目录及其前端依赖、构建、浏览器 smoke
   不属于交付物。nanobot 上游 WebUI 不复制、不 Fork，只通过现有 Tool/流式消息契约
   展示结果。
4. P2B 的“审批 UI 原型”改为“nanobot 对话确认契约和 Broker 设计”。P2B-04 的
   UI E2E 改为 nanobot WebUI 的对话确认、拒绝和结果事件 E2E；没有可信用户事件
   上下文时只能验收拒绝路径。安全不变量不变：模型不能批准、签发或接收 token。
5. P5-01/P5-02 保留 HTTP 契约、持久事件和 SSE 内部能力；P5-03 改为
   “nanobot WebUI 事件桥接与本地动作 Broker”；P5-04 改为 nanobot Tool/流式事件
   集成、重连降级、确认安全测试和真实聊天 E2E 证据。两者都不新增页面；未完成前
   P5 不得重新标记为 certified。
6. P5 阶段目标由“FastAPI + React 控制台”改为“FastAPI 内部控制面 + nanobot
   原生聊天闭环”。验收必须从 nanobot WebUI 完成需求、确认、结果、失败和回滚查看。
7. P6 Sandbox 只实现 ExecutionBackend 接口和安全证据，不新增任何前端页面；能力
   状态通过 nanobot 对话中的结构化消息报告。
8. P7 演示、README 和简历不得再声称“React 控制台”；演示只录 nanobot WebUI，
   简历可写 FastAPI 内部控制面、事件桥接和 nanobot Tool 集成。
9. 冻结计划第 9.1 节的前端 build/browser E2E 改为 nanobot adapter contract、
   Broker 安全测试、事件脱敏/重放和 nanobot 聊天 E2E；第 11.1 节的控制台步骤改为
   nanobot 对话步骤；第 11.3 节的 FastAPI+React DoD 改为 nanobot-only 受控交付场景。

### 旧节点的替代验收矩阵

| 原节点/条目 | 当前替代交付 | 放行条件 |
|---|---|---|
| P2B-03 审批 UI 原型 | nanobot Tool schema、对话确认消息契约、内部 loopback API | schema 严格、API loopback、模型不接触 token |
| P2B-04 聊天→审批→应用→Diff E2E | nanobot WebUI 的提案→用户确认→Broker→Apply→Diff 事件 E2E | 用户事件可证明、哈希/审批/幂等/回滚证据完整；Broker 未完成前只通过拒绝路径 |
| P5-01 HTTP 契约 | `/api/v1` 与 `/v1` 内部兼容接口 | API/SSE 合同、稳定错误、脱敏和 loopback 测试通过 |
| P5-02 Event/SSE | 内部 RunEvent/SSE → nanobot 结构化阶段消息 | 顺序、Last-Event-ID 重放、断开快照降级和无 token 测试通过 |
| P5-03 Projects/Diff/Run 等页面 | nanobot WebUI 事件桥接与 Action Broker | 不新增页面；消息覆盖计划、风险、进度、终态、Artifact、回滚 |
| P5-04 Skills/Security/浏览器 E2E | nanobot Tool 集成、Broker 安全测试、Fake 聊天 E2E | 错误/不可用状态如实展示；无前端 build/browser gate |
| P6 前端能力展示 | nanobot 消息中的 backend capability/unavailable 事件 | 真实 Sandbox 仍须通过 P6 独立能力证据 |
| P7 演示/简历 | 只录 nanobot WebUI；描述内部 FastAPI、事件桥接和 Tool 集成 | 不出现“React 控制台”；指标有可复现 evidence |

冻结计划中的 React 项目 fixture（例如 G-02）仍可作为“被交付的用户项目”测试对象，
但不是 DevPilot 的前端；删除的是 DevPilot 独立控制台，不是用户项目的 React 代码支持。

## 保留与删除

保留：Domain/Application、SQLite、ChangeSet、Approval、Apply、Verify、Rollback、
Artifact、ExecutionBackend、P6 FC 适配器、FastAPI loopback API、持久 RunEvent 和
内部 SSE。删除：`devpilot-agent/web` 独立控制台源码、前端依赖、Vite 构建和浏览器
控制台 smoke。

## 验收与回滚

本 ADR 生效后，必须删除独立控制台并更新当前 P5/P6 交接文档；后端回归测试不得因
删除 UI 而降低审批、路径、token、哈希、执行和回滚门禁。新增 nanobot 集成前，先用
Fake backend 验证结构化阶段事件；真实 Sandbox 仍受 P6 Gate 约束。

若 nanobot 原生流式通道或动作 Broker 无法提供安全闭环，回滚点为本 ADR 对应提交，
系统保持“只读分析 + 变更提案 + 明确不可执行”状态，不恢复独立控制台作为临时绕过。
