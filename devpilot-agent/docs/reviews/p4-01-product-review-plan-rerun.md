# DevPilot P4-01 独立产品/质量 Review Plan（rerun）

## 评审身份与基线

- 评审节点：`P4-01`，`feature-delivery` Skill/Workflow 与 `G-01`。
- 目标实现提交：`61179185473de93ac34af2b949de1eaa982f5caa`。
- 证据更新提交：`df855f4`；本次优先检查 clean checkout/worktree `D:\Users\tianmin.li\Documents\Codex\2026-08-10\new-chat\work\p4-review-20260902` 的 `HEAD=df855f4`，必要时用 `git show 6117918` 对照实现提交。
- 冻结计划：`outputs/DevPilot-AI-Handoff-Portable-2026-08-31-v2/plans/DEVPILOT_FROZEN_PLAN.md`，版本 2.1.0；SHA-256 `4f06504ad3273781f7c4cff2d919aec432ea0385014c8c94c42455709efbdec9`。
- 评审权限：严格只读检查；除本评审计划和最终评审报告外，不修改 `src`、`tests`、配置、迁移、Skill、前端或其他产品实现。用户-owned Node fixture edits 只记录，不视为实现提交内容。

## 用户/业务结果与非目标

P4-01 应使用户能够把一个边界明确的 FastAPI 功能需求转成可审查的只读事实、冻结 Change Plan、明确审批、不可变 ChangeSet、Diff、受控 Fake 验证和服务端拥有 Artifact；用户能看见真实成功、失败或未执行状态，并能显式选择回滚。该节点是“受控代码交付”的流程验证，不是开放式 Coding Agent。

本节点不包含：`bug-fix`/`test-repair` 工作流、自动修复循环、P6 Sandbox、真实项目代码自动执行、任意 Shell、依赖安装、网络下载、自动 commit/push/deploy、React 控制台完整交付、生产公网认证或新的 Runtime。

## 节点级验收问题

### A. G-01 机器可复现主链

是否存在一条完整且可重复的：

`bounded FastAPI restricted endpoint`
→ `inspect/search/read`
→ `provenance/hash`
→ `immutable plan/scope/check/stop conditions`
→ `external r1.apply approval + exact confirmation`
→ `Apply`
→ `external r2.verify approval`
→ `Fake verification`
→ `server-owned success Artifact`
→ `explicit external rollback`

必须分别证明：

1. R0 事实收集有允许根、路径边界、文件/行范围、来源标识和 SHA-256，且没有以模型声明替代服务器事实。
2. Plan/ChangeSet 对 scope、patch、check IDs、stop conditions 和哈希具有不可变绑定；批准后不能换正文、文件、检查或能力。
3. Apply 与 Verify 均由外部控制面审批触发，精确确认绑定目标对象/哈希/动作，nanobot Skill/Tool 不能直接消费 R1/R2 token。
4. Fake backend 只模拟验证，绝不运行项目代码；结果能区分 pass、nonzero failure、backend unavailable/其他失败。
5. 成功 Artifact 由服务器拥有目录写入、与 Run/Verification 绑定、含可核验哈希；模型不可伪造成功或写入项目目录。
6. 回滚必须是显式外部动作，要求适配的 rollback approval，并基于前/后 manifest；不能把“失败”自动当作回滚完成。

### B. G-01 同一 Golden 的失败与不可伪造性

同一 Golden 或同一机器可复现 Golden 套件是否覆盖：

- nonzero verification failure Artifact：非零退出码产生服务端拥有的失败 Artifact/证据，状态是 `failed` 或等价真实失败，不得生成成功 Artifact 或成功状态。
- Artifact DB failure：Artifact 持久化失败时 Run/Task/Verification 不能伪造成完成；必须暴露稳定错误/失败状态和可诊断证据，不能先报告成功再静默丢失 Artifact。
- Run terminal state 不可伪造完成：模型、Skill、Tool 响应体或客户端字段不能把 `failed/not_run/unavailable` 改成 `completed/verified/success`；终态必须由服务端事实汇总并追加事件。

### C. Skill、Tool 与 nanobot adapter

- `feature-delivery` Skill 的允许 Tool、顺序、停止条件和 P6 前限制是否与冻结计划一致。
- Skill contract 声明的 Tool 名称、输入输出 schema、风险等级、错误码是否与实际注册 Tool 和 adapter 一致；是否 `additionalProperties=false`、无 command/shell/backend/任意环境字段。
- proposal-only 是否真实成立：nanobot 侧只能 inspect/search/read/plan/propose/request-approval/get-state/diff；不能直达 apply/run_check/rollback。
- 错误是否稳定、脱敏、无未信任响应体泄露（尤其环境、绝对敏感路径、内部堆栈、密钥和完整外部响应体）。

### D. P6 前 fail-closed 与边界安全

- 未通过 P6 时真实/GuardedLocal backend 是否拒绝自动修复/真实执行声明；无合格后端时是否 `EXECUTION_BACKEND_UNAVAILABLE` 或等价稳定拒绝。
- 是否没有回退到 nanobot Shell、任意 subprocess、`shell=True`、命令拼接、网络下载或依赖安装。
- 项目路径、允许根、秘密/`.env`、符号链接、生成目录、Artifact 根、tenant/project 绑定和用户-owned Node fixture 是否有边界证据。

### E. 工程与交付证据完整性

- manifest 是否绑定同一目标实现提交/证据提交，记录配置摘要（无秘密）、迁移版本、依赖锁哈希、命令、退出码、时间、产物相对路径与 SHA-256。
- 测试、静态检查、迁移升级、secret/markdown/link/lock/许可证证据是否存在且可复跑；不以“代码存在”或“测试全绿”替代端到端产品证据。
- 是否记录回滚点、最危险失败路径演练、范围变更、clean worktree 状态和外部依赖问题。

## 所需可观察证据

1. 目标提交与 clean worktree 的 `git status`、提交关系、diff/stat、相关文件清单。
2. P4-01 实现文件、Skill 源文件、adapter/Tool 注册、schema/contract、状态机/服务、Artifact/DB 代码和相关测试。
3. G-01 正常及失败场景的测试命令、原始机器结果、Artifact 内容/哈希、Run/Task/ChangeSet/Verification 状态与事件序列。
4. Apply、Verify、Rollback 的外部审批/精确确认请求与拒绝/消费证据；证明 token 不进入模型消息、Skill 参数、JSON body、URL、SSE、日志或 Artifact。
5. Fake backend 契约、P6 前真实 backend fail-closed 测试；禁止 nanobot Shell/subprocess fallback 的静态或运行证据。
6. manifest 与测试/锁文件/迁移/secret/markdown/link/许可证结果，且每项能追溯至目标实现提交。
7. 用户-owned Node fixture edits 的基线、路径和影响说明；不得把其差异计入 P4-01 实现质量。

## 安全、隐私、租户与兼容性检查

- 只允许真实解析后的项目路径落在服务端允许根；不允许跨项目/跨任务/跨 Run 使用 Plan、Approval、ChangeSet、Artifact 或 token。
- 所有写入绑定 immutable patch hash、scope hash、前置/后置 manifest 和一次性外部 approval；并发/重放/范围漂移必须拒绝。
- 错误响应仅含稳定 code、脱敏 message、retryable 和可选 details_ref；不反射不可信响应体或敏感上下文。
- Skill 文本、模型响应、用户请求、项目内配置均不得修改策略、注册后端、扩大权限或定义命令。
- 保持 nanobot Runtime 与 DevPilot 业务层分离，P1 回归不破坏，数据库只走迁移，工作树/用户修改不被覆盖。

## 范围蔓延与外部依赖

记录任何超出 P4-01 的功能（bug-fix/test-repair、自动修复、Sandbox、UI 全量、远程服务、任意执行、依赖升级等）。P6 是真实 Sandbox 的外部依赖门；P4-01 可以用 Fake 验证流程，但不得因此宣称真实隔离或自动修复。缺少前置 P3 Gate、缺少机器可复现证据、外部 backend/Provider 未验证或 clean checkout 不可重演，均可能导致 `blocked` 或 `request-changes`。

## 严重性规则

- `blocker`：绕过审批/范围/路径边界、任意执行、秘密/未信任响应泄露、伪造成功/完成、真实 Sandbox/自动修复虚假声明，或会导致用户不可逆损失。
- `must-fix-before-next-node`：G-01 主链缺段、Skill/Tool contract 不一致、失败/Artifact DB failure 无真实终态、P6 前未 fail closed、manifest/关键测试/迁移证据缺失。
- `follow-up`：不阻塞 P4-02 的低风险文档/可观测性/可用性缺口，有明确责任人、证据路径和完成期限。
- `observation`：不影响当前验收的设计建议或未决产品问题。

## 决定状态

- `approve`：P4-01 全部必需验收与公共 Gate 均有可复现证据，可放行 P4-02。
- `approve-with-follow-ups`：必需门禁通过，仅有明确不阻塞的 follow-up，登记责任、证据和期限后可放行。
- `request-changes`：实现或证据违反必需要求；主 Agent 修复并以新目标提交重新独立 Review 后才可放行。
- `blocked`：缺少关键前置/外部依赖/机器可复现证据或待用户决策；必须补齐精确证据或决策，不能靠演示、手工改库或关闭限制解锁。

## 最终报告模板

报告必须以同一目标实现提交为范围，包含：

- `decision`
- `requirement coverage`
- `observed behavior`
- `evidence inspected`
- `severity`
- `reproduction steps`
- `recommendation`
- `regression risk`
- `unanswered questions`
- 如为 `request-changes`/`blocked`：逐项列出可解锁的精确证据、命令、提交或产品决策

