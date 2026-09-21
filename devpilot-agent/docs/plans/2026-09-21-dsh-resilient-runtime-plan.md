# DSH 借鉴：可恢复对话与统一活动轨迹实施计划

> 版本：0.1
> 日期：2026-09-21
> 状态：**IMPLEMENTED CANDIDATE — R5 REVIEW PENDING**。R1–R4 已形成候选实现；R5 双轴审查和最终验收仍未完成。
> 实施基线：执行者必须记录开始时 `origin/codex/h11-complete-handoff` 的实际 SHA，不能把旧证据挪为本次通过证据。

## 1. 执行摘要

目标是修复用户已经遇到的体验问题：一次诊断、路径、工具或模型失败后，整个会话变成“任务已暂停”，连普通聊天也不能继续。借鉴 DSH 的原则：**guard 只停止当前无进展尝试；失败是可记录、可恢复的 Turn 事实；只有未知副作用仍限制对应旧操作。**

本计划只实现四项：

1. 诊断预算耗尽、普通工具失败和模型失败不再锁住后续普通聊天或新的只读请求。
2. 统一持久化 Turn outcome 事件和 WebUI 状态投影，刷新后仍解释清楚失败和下一步。
3. 在已有活动轨迹中显示临时/动态插件的校验、启用、失败、清理生命周期。
4. 统一普通工具、子代理、DevPilot、模型与恢复错误的简短中文提示。

明确不做：完整工具输出查看入口、独立状态页、Cordis 重写、Node/TypeScript Runtime、第二 Agent Loop、第二数据库、插件市场、浏览器端动态执行、模型修改宿主代码，或任何权限放宽。

## 2. 事实、差异与约束

| 事实 | 证据 | 对本计划的影响 |
|---|---|---|
| nanobot 已有 turn delivery、runtime event、WebUI turn projection 和 transcript replay。 | `nanobot/nanobot/agent/turn_delivery.py`、`nanobot/nanobot/bus/runtime_events.py`、`nanobot/nanobot/session/webui_turns.py`、`nanobot/nanobot/webui/transcript.py` | 在原通道扩展，不建新服务/新库。 |
| H7/H8 checkpoint 已能记录未知副作用并保护重放。 | `nanobot/nanobot/agent/loop.py`、`loop_driver.py` | 只解除会话级锁死，绝不能放开旧副作用重放。 |
| 临时工具已有 artifact、状态机、FC 校验、激活和清理。 | `devpilot-agent/src/devpilot/temporary_tools.py`、`nanobot/nanobot/agent/plugins/manager.py` | 只加活动适配，不重做生命周期。 |
| 冻结计划写着“普通/只读/状态查询可以继续”，用户 WebUI 截图仍显示预算耗尽后所有消息暂停。 | `CURRENT_PLAN.md` 的 2026-09-17 修正；用户截图 | 必须先复现，不能假设现有实现已生效。 |
| DSH 的 guard 是“重复调用提醒 + 单次工具超时”，会把可处理错误返回给模型，而非冻结会话。 | `D:/Users/tianmin.li/Downloads/deepseek-harness-master (2).zip`：`packages/guard/README.zh.md` | 采纳行为原则，不引入 DSH 框架。 |

### 2.1 不可放宽的安全边界

| 情况 | 本 Turn | 下一条消息 | 重放规则 |
|---|---|---|---|
| 读操作、路径拒绝、模型/工具超时、诊断 budget、插件校验失败 | 给出结论 | 普通聊天、只读、新任务允许 | 只能由新请求重新触发，不能暗中重放旧调用 |
| 已知成功/失败/取消的 Apply、FC、外部操作 | 展示终态 | 允许继续 | 查询已记录结果，绝不重复 |
| 结果未知的副作用 | 停在“需查询” | 普通聊天和只读允许 | 对同一 operation 仅允许查询/恢复，不允许重发 |
| 路径、密钥、未确认写入或外部动作的安全拒绝 | 明确拒绝 | 其他安全请求允许 | 永远不绕过 |

### 2.2 计划评审结论与决策日志

本轮是设计计划，不是代码验收。以下角色结论来自当前仓库入口、用户现场截图和 DSH 的只读
参照；开始代码后仍须按 ADR-0004 做独立 Standards/Spec 双轴审查。

| 角色 | 结论 | 已采纳决定 |
|---|---|---|
| 产品 | 用户的核心体验是“失败后还能自然继续”，不能要求背命令或新开会话。 | R1/R4 将 guard 限制为当前尝试。 |
| 架构 | 已有 event、projection、plugin lifecycle；缺的是统一分类 seam，而非新 Runtime。 | R1 建 `TurnOutcomePolicy`，R2/R3 只接现有通道。 |
| 安全 | 不能把 session 可继续误解为未知副作用可重放。 | `query_only` 精确绑定 operation reference；保守读取旧 checkpoint。 |
| UX | 用户不应看到 Python 异常、预算术语或英文兜底。 | message key + 中文短提示 + 活动明细，且不新增独立状态页。 |
| 平台/SRE | 连接中断后 UI 内存不可信，重连必须重放已提交事实。 | R2 先 append outcome 再 publish，并做 replay fixture。 |
| QA | “计划说已修”与截图冲突时，必须先有红灯复现。 | R0 是所有代码变更的强制前置。 |
| 交付 | 新增范围必须可单节点回滚，不能污染录制训练项目。 | R5 隔离 data/session/basetemp，每节点单独候选与证据。 |

| 决策 ID | 决定 | 原因 | 状态 |
|---|---|---|---|
| DSH-R-01 | 不迁移 Cordis/DSH，只借鉴 guard、event projection、活动生命周期原则。 | 当前产品是 Python/nanobot 单 Runtime，重写会破坏冻结范围。 | 已决定 |
| DSH-R-02 | 不做完整输出入口和独立状态页。 | 用户明确排除，且不是锁死问题的必要修复。 | 已决定 |
| DSH-R-03 | 路径拒绝可继续聊天；未知副作用仅 operation 级受限。 | 同时满足 UX 和防重放。 | 已决定 |
| DSH-R-04 | 插件可见性只进入现有活动轨迹。 | 用户需要理解过程，不需要新增后台。 | 已决定 |

## 3. 新的稳定内部契约

### 3.1 `TurnOutcomePolicy` 深模块

R0 可调整文件名，但不能把这项职责拆回各个 caller。建议新建 `nanobot/nanobot/agent/turn_outcomes.py`：

```python
@dataclass(frozen=True)
class TurnOutcome:
    category: Literal[
        "completed", "recoverable_failure", "guard_stopped",
        "blocked_for_safety", "unknown_side_effect", "cancelled"
    ]
    scope: Literal["turn", "operation", "project"]
    user_message_key: str
    retry_hint: Literal["retry_new", "check_status", "change_input", "none"]
    continuation: Literal["allow", "allow_readonly", "query_only"]
    operation_refs: tuple[str, ...]
    internal_code: str | None
```

强制规则：

- `bounded diagnostic-branch budget exhausted` → `guard_stopped / turn / allow / change_input`。
- 路径越界 → `blocked_for_safety / turn / allow / change_input`。
- 未知副作用 → `unknown_side_effect / operation / allow_readonly / check_status`。
- 任何模块不得再自行用字符串 `Task paused` 决定 session 是否拒绝后续消息。
- 用户最终文本只能来自中文 message catalog；内部类名、堆栈、哈希、token、完整参数不进聊天。

### 3.2 事件与投影

不新增数据库，复用已有 transcript/session/runtime event。一个 turn 至多一个终态 `turn_outcome`：

```json
{
  "event":"turn_outcome", "schema_version":1, "turn_id":"stable-id",
  "session_key":"...", "outcome":"guard_stopped", "scope":"turn",
  "continuation":"allow", "retry_hint":"change_input",
  "operation_refs":[], "message_key":"guard.no_progress", "occurred_at":"ISO-8601"
}
```

- 先追加持久事实，再发布 WebSocket；发布失败后刷新从事实重放。
- 新投影字段为 `latest_turn_outcome`；projection 是事件的纯函数。
- 旧 transcript/checkpoint 缺字段时：若存在未知副作用，保守 `query_only`；其他情况允许继续。
- 事件内不得有源码、文件内容、工具原始参数、密钥、授权 token、原始堆栈。

### 3.3 插件活动事件

复用既有 `gap_detected → generated → validating → validated → active → retiring → disposed` 状态机：

```json
{
  "event":"capability_activity", "schema_version":1, "turn_id":"...",
  "capability_kind":"temporary_tool|plugin|subagent|devpilot|tool",
  "name":"safe-id", "phase":"validating|active|retiring|failed",
  "outcome":"completed|recoverable_failure|blocked_for_safety",
  "summary_key":"temporary_tool.validation_failed", "operation_refs":["run:..."]
}
```

只显示安全名称、版本、阶段、短中文说明和下一步；不得显示 artifact 绝对路径、source、FC request、用户项目输入或凭据。

## 4. 节点总览

| ID | 类型 | 目标 | 前置 | 解锁 |
|---|---|---|---|---|
| R0 | diagnosis | 复现截图所示锁死并锁定真实入口 | 无 | R1/R2/R3 |
| R1 | contract/security | 实现 outcome 分类，修复 guard 会话锁死 | R0 | R2/R4 |
| R2 | persistence/protocol | 持久化 outcome，重连/刷新一致投影 | R1 | R4/R5 |
| R3 | capability/protocol | 临时插件生命周期进入活动轨迹 | R0，R2 事件格式 | R4/R5 |
| R4 | UX integration | 统一工具、子代理、模型、DevPilot、恢复提示 | R1/R2/R3 | R5 |
| R5 | release | WebUI 录制前验收、文档、双轴审查 | R1–R4 | 发布/录制 |

未通过节点不得解锁依赖节点。一个节点对应一个可审查候选；每个代码候选按 ADR-0004 进行 Standards 与 Spec 并行审查。

## 5. 节点详细执行说明

### R0：复现与归因

**目的：** 确认用户看到的是当前代码而不是旧进程、旧 data/session 或错误配置。

**必须阅读：**

- `nanobot/nanobot/agent/loop.py`：`h7_handoff_pause` 的读写、清理与恢复。
- `nanobot/nanobot/agent/runner.py`：`driver_paused`、工具错误、iteration budget。
- `nanobot/nanobot/agent/loop_driver.py`：PAUSE 结果。
- `nanobot/nanobot/channels/websocket/runtime.py`、`turn_delivery.py`：最终回复与完成。
- `nanobot/nanobot/session/webui_turns.py`、`webui/transcript.py`：投影/replay。

**步骤：**

1. 保存 `git status --short`、branch、HEAD、实际 Python、启动命令、数据目录和 PID；绝不打印密钥。
2. 用独立 temp data/session 启动 Harness；在浏览器确认其确实连到新 PID。
3. 场景 A：发送范围外项目的只读检查，随后发送“你好”。
4. 场景 B：通过现有 test double 或最小 fixture 耗尽诊断 budget，随后发送“你好”和不相关只读问题。
5. 保存脱敏 transcript、runtime event、session metadata、checkpoint 与截图。
6. 将根因明确定位为：旧服务/旧包、残留 pause metadata、`driver_paused` 被全局处理、TurnDelivery 映射错误，或其他可复现入口。
7. 新增红灯测试：`test_guard_exhaustion_does_not_block_next_plain_chat_turn`（名称可按仓库惯例调整，但断言不得弱化）。

**通过条件：** 自动测试在修改前红灯；证据写明用户截图与当前计划文本的冲突；本节点不修改业务逻辑或用户数据。

**失败处理：** 无法复现不能声称无缺陷。记录环境差异，继续针对“旧 session 含 pause metadata”的兼容 fixture 开发 R1。

### R1：分类与会话继续

**目的：** guard 只停止当前尝试；未知副作用只保护相同 operation。

**预计修改文件：**

- 新 `nanobot/nanobot/agent/turn_outcomes.py` 或等效深模块。
- `nanobot/nanobot/agent/loop.py`、`runner.py`、`loop_driver.py`。
- `nanobot/nanobot/agent/turn_delivery.py`。
- `nanobot/tests/agent/test_loop_driver.py`、`test_runner_core.py`、`test_loop_runner_integration.py` 及 R0 实际入口测试。

**步骤：**

1. 盘点全部 pause/exception 来源；建立 `internal_code → TurnOutcome` 映射表。
2. 写 outcome 纯函数单测：输入受信内部事实，断言 category/scope/continuation/retry hint。
3. 让 `loop.py` 只存 operation 级未知副作用，而非“整个聊天暂停”的裸标记。
4. 让 `runner.py` 将 budget/重复调用结束为当前 turn 的 `guard_stopped`，不阻止下一条新消息。
5. `turn_delivery.py` 使用 message key 输出中文短提示，例如“这一步一直没有新进展，我先停在这里。你可以补充线索、换个问法，或者继续聊别的。”
6. 保留 `/new` 作为显式新会话入口，但不得再作为普通聊天的“解锁按钮”。
7. 旧 checkpoint 兼容：无 replay fingerprint 的未知旧工具仍 fail-closed；无未知副作用的旧 pause 不能永久封锁普通聊天。

**最低测试：**

- budget 耗尽后同一 session 的“你好”获得正常模型调用与回复。
- 同一 session 的不相关只读项目分析可以执行。
- 未知 Apply/FC 不能重放，只能查询；两个未知 operation 中一个结算后，另一个仍被保护。
- 路径拒绝、模型超时、工具超时、取消分类不同；最终用户文本没有内部异常。

**手工验收：** WebUI 中触发 guard → 看到“当前尝试停止” → 发送“你好”成功。

**回滚：** 回退本节点提交即可；不得迁移或删除历史 checkpoint。

### R2：统一 event/projection

**目的：** 刷新、重连、历史回放得到一致的失败说明和可继续状态。

**预计修改文件：**

- `nanobot/nanobot/bus/runtime_events.py`
- `nanobot/nanobot/agent/turn_delivery.py`
- `nanobot/nanobot/session/webui_turns.py`
- `nanobot/nanobot/webui/transcript.py`
- `nanobot/webui/src/hooks/useNanobotStream.ts`、现有 trace/activity model/renderer 和对应测试（以实际引用为准）。

**步骤：**

1. 定义版本化 Python event schema，拒绝 WebSocket 的自由 dict。
2. 终态 outcome 与 `turn_completed` 共用 `turn_id`；每 turn 只能有一个终态 outcome。
3. outcome append 成功后才发布；若崩溃发生在 append 前，replay 显示“上一轮未完整记录”，而非成功。
4. `webui_turns.py` 用纯函数折叠 `latest_turn_outcome`，不把浏览器内存当权威。
5. 前端只以 `category/scope/retry_hint/message_key` 渲染；不得检测 Python 类名或英文错误文本。
6. 加旧 transcript fixture：没有 schema/outcome 字段的历史聊天必须能正常打开。

**测试/验收：** refresh/reconnect 不重复工具调用；失败摘要不丢失；活动轨迹提供“发生了什么/下一步”；所有载荷经脱敏断言。

**回滚：** 前端忽略新事件、后端停止发事件；旧 transcript 和 projection 保持可读。

### R3：插件生命周期活动

**目的：** 不增加管理页，只在当前聊天活动轨迹展示临时插件生命周期和失败原因。

**预计修改文件：**

- `devpilot-agent/src/devpilot/temporary_tools.py`
- `nanobot/nanobot/agent/plugins/manager.py`
- DevPilot event bridge、`nanobot/nanobot/bus/runtime_events.py`
- 现有 WebUI activity/tool trace renderer 与 `devpilot-agent/tests/test_temporary_tools.py`。

**步骤：**

1. 从既有状态机列出每个合法迁移；每次迁移至多发一个幂等 activity event。
2. 新建 `PluginActivityAdapter`：唯一负责 lifecycle/manager result → capability activity；禁止多个模块复制中文文案。
3. 分开映射：校验拒绝、FC 不可用、过期、激活冲突、cleanup pending；都允许普通聊天继续。
4. 会话结束/重启 orphan cleanup 仅写后台活动，清理失败不能阻塞新 turn。
5. 不修改 FC 校验、权限、持久化确认或临时工具的 source 执行边界。

**最低测试：** 成功全链路顺序；校验失败后聊天继续；并发 cleanup 不重复事件；重启可读；事件无 source/路径/secret。

**手工验收：** WebUI 跑一次合规 temporary tool，用户能看懂“正在校验/已启用/已清理”；失败时看到“校验未通过，聊天仍可继续”。

### R4：统一失败和恢复体验

**目的：** 取消英语兜底和全局暂停，不掩盖安全边界。

| 来源 | 分类 | 中文短提示 | 下一步 |
|---|---|---|---|
| 工具超时 | recoverable_failure | “这个操作等太久了，已经停止；对话还能继续。” | 缩小范围/重试 |
| 重复/预算 guard | guard_stopped | “这一步没有新进展，我先停在这里。” | 新线索/换目标 |
| 路径拒绝 | blocked_for_safety | “这个目录不在当前授权范围内，所以我没有读取它。” | 换已授权路径 |
| 模型/网络暂时失败 | recoverable_failure | “这次请求没有完成，不影响后续对话。” | 重试/继续其他话题 |
| 未知副作用 | unknown_side_effect | “上次执行结果还不能确认；我不会重复执行，但可以查状态。” | 查状态/只读 |
| 子代理失败 | recoverable_failure | “辅助任务没有完成，主对话仍可继续。” | 改策略 |

**步骤：**

1. 搜索并归类用户路径中的 `Task paused`、`couldn't produce a final answer`、裸 `RuntimeError`、`Sorry, I encountered an error`。
2. 只替换能由 R1/R2 分类的最终用户路径；不做无关全仓文案重写。
3. 模型完成工具步骤却没有 final answer 时，形成 recoverable outcome，输出已有事实/未知项/下一步。
4. 保持 `failOnToolError` 的子代理配置语义；子代理失败不得冻结父会话。
5. 每次失败只出一条终态聊天提示，明细在活动轨迹中，避免重复报错。

**验收：** 用户不用 `/new`、固定命令或重选模式来从普通失败恢复；只有会重放未知副作用的具体请求被阻止。

### R5：录制前验收与交付

**隔离原则：** 使用独立 data/session/pytest basetemp；不修改录制用训练项目的故障基线，不污染真实聊天和本机密钥。

**自动检查：**

1. nanobot：loop/runner/task profile/WebSocket/transcript/runtime event/plugin manager/subagent 受影响 pytest。
2. devpilot-agent：temporary tools/event bridge 受影响 pytest。
3. 对应 Ruff/Pyright 和 WebUI lint/typecheck/test。
4. 运行命令和实际输出写入证据；没有执行不得写通过。

**真实 WebUI 验收脚本：**

1. 新会话，发送“你好”。
2. bug-fix 下故意给范围外路径 → 中文拒绝 → “你好”与不相关只读问题均成功。
3. guard 场景 → 当前尝试停止 → 自然语言继续任务成功。
4. 工具/模型失败 → 刷新 → 同一失败摘要仍在 → 能继续聊天。
5. temporary tool 成功 create/validate/activate/cleanup：活动阶段完整。
6. temporary tool 校验失败：活动失败但聊天继续。
7. 安全 double 产生未知副作用：普通问答/只读继续；重复旧写入被阻止并指向状态查询。
8. 刷新/重启后断言没有第二次 FC/Apply/临时工具副作用。

**审查：** 固定候选 SHA → 并行 Standards/Spec review → 批量修复 → 原审查员增量复核。证据放 `devpilot-agent/docs/evidence/resilient-runtime/`，报告放 `devpilot-agent/docs/reviews/`。

**发布门槛：** R0–R5 全部通过、两轴明确 PASS、录制启动说明更新后，才能说“可以录制”。

## 6. 风险与回滚

| 风险 | 防护 | 回滚 |
|---|---|---|
| 为解除锁死而重放未知副作用 | operation 级 `query_only` 与普通会话 continuation 分离测试 | 回退 R1，不删除 operation/checkpoint |
| 新事件破坏历史聊天 | version + tolerant reader + 旧 fixture | 前端忽略新 event，旧 replay 不变 |
| 活动轨迹泄露隐私 | adapter allowlist + redaction test | 停止 activity 发布，不触碰 artifact/FC |
| 友好文案掩盖安全拒绝 | `blocked_for_safety` 独立类别，Spec review 检查 | 恢复原安全拒绝，不放宽 policy |
| 参照 DSH 导致范围膨胀 | 本文 non-goals 是硬约束 | 拒绝 Cordis/第二 Runtime 变更 |

## 7. 给后续模型的固定执行顺序

每个节点开始前必须：

1. 读根 `AGENTS.md`、`CURRENT_PLAN.md` §0/§7、ADR-0004、本计划节点；代码变更再读 `engineering/implement`、`engineering/tdd`，故障节点读 `engineering/diagnosing-bugs`。
2. 记录 `git status --short`、branch、HEAD；保留未跟踪配置、`sessions/`、`memory/` 和用户文件。
3. 先让最小测试失败，再做最小代码改动；不要在 R1 新建页面，不要在 R2 新建数据库。
4. 跑节点检查、人工场景并更新证据；失败先定位，不借机重构。
5. 固定候选后执行 ADR-0004 双轴 review；两轴最终明确通过才标记节点完成。
6. 在本表更新状态、基线、候选 SHA、测试命令、证据与 review 路径。

## 8. 状态历史

| 日期 | 节点 | 状态 | 事实 |
|---|---|---|---|
| 2026-09-21 | R0–R5 | ready | 用户批准范围；未修改应用代码，未运行本计划测试，未做本计划代码审查。 |
