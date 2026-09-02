# DevPilot P4-01 独立产品/质量 Review Report（rerun）

## decision

`request-changes`

P4-01 当前不能放行 P4-02。clean review worktree 本身无未提交修改，且实现文件在 `61179185473de93ac34af2b949de1eaa982f5caa` 与证据提交 `df855f4d1003e20ad5998775ce61f4a5b4394d65` 之间没有变化；但是 G-01 的产品闭环仍未被实现和证据共同证明，且存在可绕过 P6 fail-closed 的运行时路径。需要主 Agent 修复后，以新的实现提交重新启动独立 Review。

本报告严格只读生成。检查范围为 clean worktree：
`D:\Users\tianmin.li\Documents\Codex\2026-08-10\new-chat\work\p4-review-20260902\devpilot-agent`。

冻结计划已从头到尾读取并核验：版本 `2.1.0`，SHA-256
`4f06504ad3273781f7c4cff2d919aec432ea0385014c8c94c42455709efbdec9`。

## requirement coverage

| 要求 | 结论 | 评审观察 |
|---|---|---|
| bounded FastAPI restricted endpoint fixture | 部分满足 | 测试 fixture 包含 FastAPI `/health` 与 `/internal/status` 变更，但只做源码 fixture 断言；P4 前不应执行项目代码，因此不能把它当作真实 endpoint E2E。 |
| inspect/search/read provenance/hash | 不满足 | 生产实现只验证 evidence run 属于目标项目且 `kind == repo-analysis`；没有验证该 Run 中真实发生了 search/read，也没有把 read 的路径、行段和 SHA-256 纳入服务端绑定。 |
| immutable plan/scope/check/stop conditions | 部分满足 | patch/scope 在 P2B ChangeSet 中持久化，但 P4 的 `check_ids`/`stop_conditions` 只更新返回给调用者的内存 `preview`；数据库中的 ChangePlan 仍保存空数组，Approval 也不绑定这些字段。 |
| external `r1.apply` approval + exact confirmation | 部分满足 | 测试使用控制面签发 token，Workflow 校验确认字符串；但确认不是服务端持久化/绑定条件，直接控制面 Apply API 不需要 exact confirmation。 |
| Apply → external `r2.verify` approval → Fake verification | 部分满足 | 普通 Fake 路径可工作，验证 token 由外部控制面签发；但 Fake 判定可被恶意 Fake 子类绕过，且运行时没有 server-attested P6 backend record。 |
| server-owned success Artifact | 部分满足 | Artifact 路径与项目隔离检查存在，内容有脱敏；但 root 仍由构造 `ControlPlaneManager` 的调用方提供，启动自检未证明所有 data/Artifact root 与允许项目不重叠，运行 Artifact 也未纳入提交证据 manifest。 |
| nonzero verification failure Artifact | 部分满足 | 非零 Fake 使 Run 为 `failed` 且写出 Artifact；但仍写 `feature-delivery.md`，没有明确的失败 Artifact/索引语义，测试未核验失败 Artifact 内容和哈希。 |
| Artifact DB failure | 部分满足 | Run 不会产生 `run.completed`，并尝试写失败文件；但成功文件已写入、ChangeSet 已进入 `verified`，失败文件没有 Artifact DB 记录，成功 Artifact 也没有持久索引，导致事实源不一致。 |
| Run terminal state cannot be forged completed | 部分满足 | `finish_run` 拒绝二次终态迁移，测试覆盖了这一点；但 P4 没有把 workflow 的完整计划/Artifact事实与 Task 终态汇总持久化，不能只凭这一条断言证明整个交付状态机不可伪造。 |
| nanobot adapter proposal-only | 部分满足 | `devpilot_feature_delivery_propose` 本身不调用 approve/apply/verify；旧的 execute/verify/rollback Tool 仍全局注册，且不属于该 Skill 的允许列表。 |
| stable error mapping / no untrusted body leak | 部分满足 | HTTP body message 不再回传，但 `error.code` 未做稳定白名单校验，任意响应体提供的 code 仍会被拼进 Tool 错误。 |
| Skill contract vs registered Tools | 不满足 | Skill 声明的允许 Tool 与冻结 P4 表不一致，缺少 `create_change_plan`/`request_approval`/`get_run_state`/`get_diff` 契约；同时 adapter 的嵌套 patch schema 没有 `additionalProperties=false`。 |
| P6 前真实/FC backend fail closed | 不满足 | 普通非-Fake/FC backend 会被拒绝，但 `isinstance(FakeExecutionBackend)` 接受任意 Fake 子类；无服务端能力快照、验证套件版本和过期校验。 |
| manifest/tests/static/migration/secret/markdown/lock evidence | 部分满足 | manifest 正确指向实现提交和冻结计划 hash，规范化 LF 后的锁文件/Artifact 文档 hash 可核对；但缺少 license evidence、Windows 目标路径结果、实际 G-01 运行 Artifact hash/raw evidence，且记录的是 dirty live tree 的 `clean_tree=false`。 |

## observed behavior

### F-01 — blocker：G-01 的 evidence provenance/hash 仍可由测试伪造

`src/devpilot/workflows.py:163-176` 只对每个 evidence id 检查：它属于目标项目，并且其 Run kind 是
`repo-analysis`。`evidence_hash` 在 `src/devpilot/workflows.py:189-191` 只是 evidence id 列表的 SHA-256。
它不包含 inspect/search/read 的事实内容、读取路径、行号、read SHA-256 或事件序列。

更严重的是，`src/devpilot/code_access.py:218-325` 的 `search/read` 本身不写 provenance event。
当前 Golden 的 `tests/test_p4_feature_delivery.py:65-90` 是在调用 search/read 后由测试辅助函数手工
`append_event`，所以测试能够构造出看似完整的事件序列，但这不是生产 workflow 产生的证据。
任意同项目的 `repo-analysis` Run（甚至没有真实 search/read 事件）都可以作为 evidence id 通过
`FeatureDeliveryWorkflow.propose()`。

这使当前 G-01 只证明“测试可以补齐证据字段”，没有证明
`inspect/search/read → provenance/hash → plan` 的机器链路，属于核心验收失败。

### F-02 — blocker：P6 fail-closed 可被 Fake 子类绕过

`src/devpilot/workflows.py:257-265` 与 `src/devpilot/control_plane.py:304-323` 都使用
`isinstance(backend, FakeExecutionBackend)` 作为 P4 前唯一执行门。调用方可以提供一个继承
`FakeExecutionBackend` 但覆盖 `run()` 的 backend；该对象仍会通过 `isinstance` 检查，并可执行真实
代码或访问外部服务。当前测试 `tests/test_p4_feature_delivery.py:529-560` 只覆盖一个不继承 Fake 的
远程 backend，因此没有覆盖这个绕过方式。

同时，`ControlPlaneManager` 接受构造参数中的任意 `ExecutionBackend`，代码没有读取服务端登记的
backend identity、version、capability hash、verification suite version、verified_at 或失效条件。
这不满足冻结计划的能力快照与 P6 Gate 语义。当前 FC 环境变量对普通 FC backend 会被后续类型检查
拒绝，这是正向行为，但不能弥补可伪造 Fake 类型和缺少 server-attested gate 的问题。

### F-03 — blocker：P4 计划的 check/stop/evidence 绑定没有进入持久事实源

`FeatureDeliveryWorkflow.propose()` 只在返回的字典上执行
`preview.update({... "evidence_hash", "check_ids", "stop_conditions" ...})`
（`src/devpilot/workflows.py:192-217`）。但 `src/devpilot/storage.py:377-390` 创建
`ChangePlanRecord` 时将 `check_ids_json` 和 `stop_conditions_json` 保存为空数组。
Approval 只绑定 patch/scope（`src/devpilot/control_plane.py:261-270`），不绑定 P4 的 evidence hash、
check set 或 stop conditions。`FeatureDeliveryProposal.preview` 还是普通可变 dict，不是不可变的
服务端计划对象。

因此重启、跨调用方或篡改 caller-visible preview 后，不能从数据库证明用户批准的正是含有这些
check/stop/evidence 约束的计划。当前测试只比较内存 preview 的字段相等，没有重启后的完整计划和
审批绑定断言。

### F-04 — must-fix-before-next-node：Skill contract 与注册 Tool 不一致

冻结计划 P4 `feature-delivery` 允许的 contract 包含 inspect/search/read、plan、propose、request approval、
get state、diff；当前 `skills/catalog/feature-delivery/SKILL.md:9-15` 只列出五个 Tool，并用新的
`devpilot_feature_delivery_propose` 替代了部分冻结契约。`pyproject.toml:20-28` 另外注册了
`devpilot_execute_changeset`、`devpilot_verify_changeset`、`devpilot_rollback_changeset`，这些虽然
不应由 proposal-only Skill 直接调用，却没有被 Skill/注册边界以可机器校验的方式区分。

此外，`src/devpilot/nanobot_tools.py:541-555` 的 patch item schema 只有 `{"type": "object"}`，
没有冻结 Tool 契约要求的完整字段与 `additionalProperties=false`。当前 Skill 验证测试只确认它仍是
`draft/reviewed:false`，并不能证明 contract 与实际注册 Tool 一致。

### F-05 — must-fix-before-next-node：Artifact failure 的状态事实不一致

非零 verification 路径在 `src/devpilot/workflows.py:300-346` 仍先写
`feature-delivery.md`，再以非零 exit code 将 Run 标为 failed；没有专用的失败 Artifact 语义或
失败 Artifact 的 DB 记录。

Artifact DB failure 时，成功文件已经由 `ArtifactService.write_markdown()` 写入，随后
`store.record_artifact()` 失败；代码再写 `feature-delivery-failure.md`，但不会为该失败文件写
ArtifactRecord，且 ChangeSet 已被 `ControlPlaneManager.verify(... finalize_run=False)` 迁移到
`verified`。Run 确实会变为 failed，这是一个已关闭的正向点；但“文件、DB Artifact 索引、ChangeSet、
Run”不是同一事实状态。当前测试只断言失败文件存在和无 `run.completed`，没有断言成功文件清理/不可见、
失败 Artifact 的索引与哈希或 ChangeSet 状态。

### F-06 — must-fix-before-next-node：错误 code 仍直接信任不可信响应体

`src/devpilot/nanobot_tools.py:440-456` 不再返回 HTTP body message，这是已修复的正向变化；但它
仍直接返回 `error['code']`，只检查该值是字符串，没有稳定错误码 allowlist、长度限制或字符限制。
因此响应体 `{"error":{"code":"<任意敏感内容>"}}` 仍可将该内容泄露给 nanobot。当前测试只把 secret 放在
`message` 字段，未覆盖恶意/超长/秘密型 `code`。

### F-07 — must-fix-before-next-node：阶段 evidence manifest 仍不足以支撑公共 Gate

已核对的正向事实：

- `docs/evidence/phase-4/manifest.json:5-11` 精确绑定 `61179185473de93ac34af2b949de1eaa982f5caa`
  与冻结计划 hash。
- `6117918..df855f4` 在 `src`、`tests`、配置、迁移、Skill、lock 文件上没有差异；clean review worktree
  `HEAD=df855f4` 且 `git status --short --branch` 无未提交文件。
- manifest 声明的 acceptance/review/lock hash 在按 Git 规范将 checkout 的 CRLF 规范化为 LF 后可以核对。
- manifest 声明的 `114 passed, 3 skipped`、Ruff、Pyright、Alembic、secret scan、Markdown 和 lock check
  是可追溯的命令声明。

仍缺少或不完整的公共 Gate 证据：

- 没有 license/依赖许可证扫描结果；
- 没有 Windows 目标路径检查结果；
- artifacts 列表没有本次 G-01 运行产生的成功 Artifact、nonzero failure Artifact、Artifact DB failure
  fallback 的路径和 SHA-256；
- 没有可复现运行结果中的事件序列、审批消费状态和回滚 hash 作为独立 machine-readable evidence；
- manifest 记录原 live checkout `clean_tree=false`，虽然已解释为两个用户-owned Node fixture edits，
  但没有重新生成一份以 clean checkout 为基线且 `git status` 可验证的阶段 manifest。

用户-owned Node fixture edits 不计入实现缺陷，也没有在 clean worktree 中被修改；它们只使当前 manifest
不能单独充当干净工作树证据。

## evidence inspected

- 冻结计划：`D:\Users\tianmin.li\Documents\Codex\2026-08-10\new-chat\outputs\DevPilot-AI-Handoff-Portable-2026-08-31-v2\plans\DEVPILOT_FROZEN_PLAN.md`。
- 实现/证据提交：`61179185473de93ac34af2b949de1eaa982f5caa`、`df855f4d1003e20ad5998775ce61f4a5b4394d65`。
- `src/devpilot/workflows.py`、`control_plane.py`、`storage.py`、`patches.py`、`code_access.py`、
  `services.py`、`artifacts.py`、`redaction.py`、`execution.py`、`verification.py`、`nanobot_tools.py`、
  `skill_manager.py`。
- `skills/catalog/feature-delivery/SKILL.md`、`pyproject.toml`、`uv.lock`。
- `tests/test_p4_feature_delivery.py` 及 P2A/P2B/P3 相关边界测试文件清单。
- `docs/evidence/phase-4/p4-01-acceptance.md`、`docs/evidence/phase-4/manifest.json`、已有 P4 review/closure
  文档（仅作历史证据交叉核对，未把其结论当作本次独立结论）。
- `migrations/versions/0005_p4_run_link.py`。
- clean worktree 的 Git 状态、提交历史、实现提交 diff/stat、证据提交 diff/stat 和 diff check。

未运行会改变 worktree 的测试、服务、迁移或外部 backend；本次结论来自提交内容、测试源码和已提交
机器结果的只读交叉核验。

## severity

- F-01、F-02、F-03：`blocker`，直接破坏 G-01 的证据真实性、不可变授权或 P6 fail-closed 不变量。
- F-04、F-05、F-06、F-07：`must-fix-before-next-node`，在 P4-02 前必须补齐，否则 Skill、Artifact 事实源和阶段公共 Gate 仍不可审计。
- 用户-owned Node fixture edits：`observation`，不属于实现提交，不作为拒绝理由；但 manifest 必须明确隔离它们。
- P6 真实 Sandbox 尚未完成本身：不是本次拒绝理由；冻结计划允许 P6 因外部方案保持 blocked。拒绝理由是 P4 的执行门无法被伪造绕过，以及 P4 证据不能证明 Fake/失败/回滚链。

## reproduction steps

以下为只读复现说明，不在本次评审 worktree 执行：

1. **Evidence provenance**：创建一个目标项目的 `repo-analysis` Run，只写 `run.started`/普通 inspect 事件，
   不调用 search/read；把该 Run id 作为 `evidence_ids` 传给 `FeatureDeliveryWorkflow.propose()`。
   现有 `workflows.py:163-176` 会接受它，因为没有 search/read 事件或 read hash 的要求。
2. **Fake subclass bypass**：定义 `class Rogue(FakeExecutionBackend)` 并覆盖 `run()` 以记录/执行外部副作用；
   将其传入 `ControlPlaneManager(execution_backend=Rogue())`，完成 Apply 后调用 Verify。两个
   `isinstance` 门都会放行该对象，现有 `_UnapprovedRemoteBackend` 测试不会捕获它。
3. **Non-persistent plan contract**：创建 proposal 后从数据库读取 `ChangePlanRecord`，确认
   `check_ids_json`/`stop_conditions_json` 仍为空；再重启 manager，确认恢复对象只能从 patch plan 恢复，
   无 P4 evidence hash/check/stop binding。
4. **Untrusted error code**：令 HTTP 失败 body 为 `{"error":{"code":"sensitive-value"}}`，调用
   `FeatureDeliveryProposeTool.execute()`；`_http_error_message()` 会产生包含 `sensitive-value` 的错误。
5. **Artifact DB failure**：让 `record_artifact()` 抛出 `SQLAlchemyError`；现有路径会保留成功文件、写未入库
   的失败文件，并将 ChangeSet 留在 `verified`，同时只将 Run 标记为 failed。

## recommendation

主 Agent 必须以新的实现提交关闭以下项目，然后重新提交独立 Review：

1. 建立服务端拥有的 Evidence record/event contract：search/read 真正写入可验证 provenance，绑定 project、
   Run、路径、行段、结果截断状态与 read SHA-256；proposal 只接受已完成、类型正确且内容 hash 可核验的
   evidence，并用 canonical facts 计算 evidence hash。增加伪造、跨项目、重启和 hash 变化测试。
2. 将 `check_ids`、`stop_conditions`、evidence hash、risk/scope 和 plan version 写入不可变持久 ChangePlan，
   并将 Approval 绑定完整 plan/patch/scope/check 集合；proposal caller 只能取得不可变序列化视图。
3. 将 P6 gate 改为服务端登记、版本/能力 hash/验证套件/平台/过期时间均可校验的记录；P4 只允许精确的
   `FakeExecutionBackend` 实例或专用注册 fake，拒绝 Fake 子类和未登记 backend。增加 Rogue subclass、FC
   环境开关、缺失/过期/能力变化的 fail-closed 测试，并断言没有 Verify 进程/副作用启动。
4. 按冻结计划明确 Skill contract：补齐或正式替代 plan/request-approval/state/diff Tool，并让实际注册
   Tool、Skill allowed list、描述、参数 schema 和错误包由合同测试一致校验；所有嵌套对象使用严格 schema。
   不要通过把 Skill 保持 draft 来掩盖 contract 不一致；review 通过后再由 owner 按生命周期推进。
5. 定义 Artifact failure 的唯一事实状态：nonzero 使用明确失败 Artifact/状态；DB failure 时不能留下可被
   当作成功的未索引 Artifact，失败 Artifact 必须有可审计索引或明确的服务端失败记录；ChangeSet/Run/Artifact
   的状态与事件必须一致，并增加 hash/重启/重复 finalize 测试。
6. 对 adapter 错误 code 使用服务端错误码 allowlist、长度和字符限制；未知 code 统一映射为稳定的通用错误，
   并测试 code/message/headers/token-shaped 内容均不会泄露。
7. 在 clean checkout 重新生成 phase manifest：包含 license evidence、Windows path 结果、迁移升级（空库和
   上一版本）、锁文件 hash、secret/Markdown/link 结果、完整 G-01 成功/失败/DB failure/rollback
   Artifact hash、事件/审批/终态证据、命令退出码和 clean `git status`。用户-owned Node fixtures 单独列出，
   不混入实现提交。

## regression risk

修复会触及 P2B ChangePlan/Approval 持久化、Run/Artifact 状态转换、nanobot Tool contract、Fake backend
注册和 P3 能力边界，回归风险为中高。最低回归矩阵应保留：P1/P2A/P2B 全量、G-01 真实 FastAPI
fixture、Apply/Verify/Rollback 重放与并发、Artifact DB failure、manager restart、跨项目 evidence、
secret/error redaction、P3 backend contract，以及两个用户-owned Node fixture 不被改动的 clean-tree 检查。

## unanswered questions

1. P4 的正式 Skill 是否必须严格采用冻结计划中的七类允许 Tool，还是允许把 plan/propose 合并为
   `devpilot_feature_delivery_propose`？若要合并，需由 owner 记录不改变安全边界的正式决策，并更新可追溯合同，
   不能只改 Skill 文案。
2. Artifact DB failure 时，产品期望是保留“未索引失败文件”、删除成功文件，还是写入独立 durable failure record？
   需要明确唯一事实源和恢复/清理规则。
3. P6 gate 的权威记录由哪个服务端配置/迁移实体持有，如何处理 backend 版本、平台或能力 hash 变化后的立即失效？
4. `exact confirmation` 是否必须由控制面服务端持久化为一次性用户动作，还是仅作为 nanobot Tool 的二次确认？
   在该决定明确前，不能把直接 HTTP Apply 当作完整 G-01 证据。

## precise unblock evidence

本报告的 `request-changes` 可由以下精确证据解锁：

- 一个新实现提交，包含真实 Evidence provenance/hash、持久 P4 plan/approval binding、不可绕过的 server-attested
  P6 fail-closed backend gate，以及严格一致的 Skill/Tool contract；
- 同一新提交上的 G-01 Golden 原始机器结果，覆盖成功、nonzero failure Artifact、Artifact DB failure、终态
  不可伪造完成、外部 r1.apply/r2.verify/r1.rollback approval、exact confirmation、Fake receipt 和回滚 hash；
- 重启、重放、跨项目、Rogue Fake subclass、FC environment、错误 code 泄露和 Artifact index 一致性测试结果；
- clean checkout 的完整 phase manifest，准确绑定新实现提交、冻结计划 hash、lock/migration/secret/Markdown/link/
  license/Windows 证据和所有运行 Artifact SHA-256；
- owner 对 unanswered questions 的明确产品决定，以及 Review 后才推进 Skill 生命周期的证据。

在上述证据出现并通过新的独立 Review 前，P4-02 状态应保持 `in_progress` 或 `blocked`，不能以当前
`114 passed, 3 skipped` 或已有 acceptance 文档自动放行。

