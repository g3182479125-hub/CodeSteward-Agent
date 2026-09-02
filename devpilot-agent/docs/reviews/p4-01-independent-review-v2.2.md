# DevPilot P4-01 独立只读产品/质量 Review（v2.2）

## decision

`request-changes`

P4-01 当前不能批准，P4-02 不解锁。发现 1 个 `blocker` 和 3 个 `major`。这些问题涉及 nanobot Tool 的审批边界、Skill/注册契约、项目安全绑定和核心 evidence 追溯，不属于可用 minor follow-up 放行的问题。

评审基线：

- clean checkout：`D:\Users\tianmin.li\Documents\Codex\2026-08-10\new-chat\work\p4-review-20260902-r8`
- HEAD：`92ef68a6cb2d52609cee770e788d06eef43dc8f9`
- 工作树：clean；主仓库中的 `tests/fixtures/node-project/src/index.js` 与 `tests/fixtures/node-project/test/index.test.js` 两个用户-owned 修改未计入本评审。
- 冻结计划：版本 `2.2.0`
- 冻结计划 SHA-256：`A36B88E8AE287DFE63A39B5EEF6DD6E5CA2954A20C8DAD48B3472CC604378459`，与用户指定值一致。

## requirement coverage

| 要求 | 结论 | 证据/说明 |
|---|---|---|
| G-01 主链：inspect/search/read → provenance/hash → immutable plan/changeset → 外部 r1.apply 审批+精确确认 → Apply → 外部 r2.verify 审批 → Fake 验证 → 服务端 Artifact → 外部 rollback | 部分满足 | `tests/test_p4_feature_delivery.py::test_feature_delivery_g01_completes_approved_fake_path_and_artifact` 通过；Workflow 层能跑完整链路。全局 nanobot 注册仍暴露了直接消费 token 的 R1/R2 Tools，因此冻结的 Tool 边界未满足。 |
| nonzero verification failure Artifact | 满足（测试范围内） | 非零 Fake 返回 `verification_failed`、Run 为 `failed`、写入 `feature-delivery-failure.md`，且不产生 `run.completed`。 |
| Artifact DB failure | 满足（测试范围内） | `record_artifact` 失败时删除成功 Artifact，生成 failure Artifact，ChangeSet 为 `delivery_failed`，Run 为 `failed`。 |
| 终态不可伪造完成 | 满足（测试范围内） | 已完成 Run 再次 `finalize_run(..., completed, ...)` 被拒绝；持久化层不允许覆盖已终态 Run。 |
| Skill/Tool/adapter proposal-only | 不满足 | `pyproject.toml` 注册了 `devpilot_execute_changeset`、`devpilot_verify_changeset`、`devpilot_rollback_changeset`；这些 Tool 为 `read_only=False` 并接收 `approval_token`。 |
| 无 command/shell/任意环境/backend/token 字段直传 | 不满足 | Proposal Tool 没有这些字段，但已注册的 Execute/Verify/Rollback Tool 有 `approval_token`，且 R1/R2 入口由 nanobot Tool 可达。 |
| 注册契约一致 | 不满足 | Skill 声明的 Tool 集与实际 entry points 不一致，且缺少冻结契约中的独立 approval-request/diff contract。 |
| P6 前 fail-closed | 代码路径总体满足，但被上述 Tool 边界阻断 | `ControlPlaneManager.verify()` 与 `FeatureDeliveryWorkflow.verify()` 对非精确 `FakeExecutionBackend` 拒绝；但该安全结论不能抵消模型可接收 R1/R2 token 的注册契约违规。 |
| 路径、Secret、项目/任务/Run 绑定、审批、hash、幂等、回滚 | 部分满足 | 目标测试覆盖路径/hash/审批消费/幂等/回滚和 evidence Run 归属；但当前 `ProjectRecord` 仅含 `id/real_path/created_at`，未看到冻结计划要求的 `trust_state/repo_fingerprint/allowed_root_id` 或显式 trusted gate。 |
| evidence/manifest 绑定同一提交并支持核心断言 | 不满足 | 当前 HEAD 为 `92ef...`，但 `manifest.json` 的 `commit`/`review_target` 仍为 `6d9c...`，`implementation_commit` 为 `79c...`，计划为旧版 `2.1.0` 与旧 SHA。 |

## observed behavior

正向观察：

- P4 相关 pytest 通过；完整 pytest 通过，输出为 3 个 symlink 权限/平台条件跳过。
- Ruff、Pyright、lock check、Secret scan、Markdown/link check 通过。
- Workflow 能绑定 inspect/search/read 事件及 read SHA-256，生成递归不可变 proposal，保存 contract hash/check id/stop conditions，并在 Apply 与 Verify 使用分离 approval。
- Fake 成功、非零验证、Artifact DB failure、终态保护、restart Run link、exact confirmation 和非 Fake pre-P6 拒绝均有测试覆盖。
- Artifact 路径由 Workflow 限制为服务端配置根，内容经过共享 redaction；验证输出不写入成功 Artifact。

阻断性观察：

### B-01 — blocker：nanobot 注册了可携带 approval token 的 R1/R2 Tools

`devpilot-agent/pyproject.toml:19-30` 实际注册了：

- `devpilot_execute_changeset`
- `devpilot_verify_changeset`
- `devpilot_rollback_changeset`

对应 `devpilot-agent/src/devpilot/nanobot_tools.py:837-1057` 的 schema/实现直接接收 `approval_token`，并且 Execute/Verify/Rollback 的 `read_only` 为 `False`。`ExecuteChangeSetTool` 可把 token 交给 `/apply`，`VerifyChangeSetTool` 可把 token 交给 `/verify`，Rollback Tool 可把 token 交给 `/rollback`。

这违反冻结计划 §4.1：nanobot 直接注册的模型 Tool 只能做 R0 与请求审批；R1/R2 必须由控制面在用户点击后携带 capability token 触发，token 不得进入 Tool 参数、Prompt、Session 或 SSE。它也直接违反本次评审计划的“proposal-only、无 token 字段直传”要求。即使 token 已由外部签发，模型 Tool schema 仍构成不允许的 token 传递边界。

### M-01 — major：Skill contract 与实际注册契约不一致

`devpilot-agent/skills/catalog/feature-delivery/SKILL.md:42-56` 声明的允许 Tool 为 inspect/search/read、feature proposal、changeset proposal、changeset/run state；但实际 entry points 还包括上述 R1/R2 执行 Tool，且没有发现独立的 `devpilot_request_approval` 或 `devpilot_get_diff` entry point。Skill 同时混用 `devpilot_feature_delivery_propose` 与旧的 `devpilot_propose_changeset`，没有一份合同测试证明 Skill allowed list、entry point、schema、风险级别和动作边界一致。

这使“Skill 只能建议固定 Tool、Tool 结果是唯一事实来源”的主链不可由当前注册契约可靠执行。当前 Skill 仍为 `status: draft`、`reviewed: false`，这本身是 review 尚未通过时合理的保护状态，但也意味着 P4-01 不能作为已启用的依赖交付给 P4-02。

### M-02 — major：核心 evidence manifest 未绑定本次评审提交和冻结计划 v2.2.0

`devpilot-agent/docs/evidence/phase-4/manifest.json:5-11,82-128` 当前记录：

- `commit`/`review_target`：`6d9c11a...`
- `implementation_commit`：`79c56f...`
- plan version：`2.1.0`
- plan SHA：旧版 `4f0650...`

实际 review HEAD 是 `92ef68a...`，冻结计划是 v2.2.0、SHA `A36B88...78459`。Manifest 的 artifact hash 也不能替代 commit/plan binding；因此它不能作为同一可复现提交上的核心 G-01 证据清单。

此外，manifest 中声明的 uv lock SHA 与当前 `uv.lock` 实际 SHA 不一致，进一步降低该 manifest 的可复现性。

### M-03 — major：冻结的项目 trust/来源绑定没有在当前持久化模型中可证

冻结计划 §3.4/§6.1 要求 Project 具备 `trust_state`、`repo_fingerprint`、`allowed_root_id`，并要求未 trusted 项目拒绝 R1/R2。当前 `devpilot-agent/src/devpilot/storage.py:36-42` 的 `ProjectRecord` 只有 `id`、`real_path`、`created_at`；`ProjectStore.ensure_project()` 会为允许根内的路径直接创建项目记录。`FeatureDeliveryWorkflow.propose()` 也会调用 `ensure_project()`，而不是验证已由用户显式设为 trusted 的项目。

现有测试证明了 allowed-root 和 evidence Run 归属绑定，但没有证明 frozen Project trust/fingerprint gate。因此路径边界不能被等同为完整项目信任边界。该问题涉及 R1/R2 安全前置，不应按文档或截图 minor 处理。

## evidence inspected

- 冻结计划：`D:\Users\tianmin.li\Documents\Codex\2026-08-10\new-chat\outputs\DevPilot-AI-Handoff-Portable-2026-08-31-v2\plans\DEVPILOT_FROZEN_PLAN.md`；已完整读取 708 行并核验版本与 SHA-256。
- 评审计划：`D:\Users\tianmin.li\Documents\Codex\2026-08-10\new-chat\work\remote-integrate\devpilot-agent\docs\reviews\p4-01-product-review-plan-v2.2.md`。
- 实现：`src/devpilot/workflows.py`、`control_plane.py`、`nanobot_tools.py`、`patches.py`、`code_access.py`、`storage.py`、`artifacts.py`、`redaction.py`、`execution.py`、`verification.py`、`fc_sandbox.py`。
- Skill/注册：`skills/catalog/feature-delivery/SKILL.md`、`pyproject.toml`，以及实际 `nanobot.tools` entry point 枚举结果。
- 测试：`tests/test_p4_feature_delivery.py`、相关 P2A/P2B 测试及完整 pytest。
- P4 evidence：`docs/evidence/phase-4/p4-01-acceptance.md`、`manifest.json`、`g01-runtime-evidence.json`。
- 相关迁移：`migrations/versions/0005_p4_run_link.py`、`0006_p4_contract_binding.py`。
- 已存在的历史 review 文档只作为仓库材料检查，未将其结论当作本次独立结论。

## severity

- `blocker`：B-01。nanobot Tool 直接接收并转发 R1/R2 approval token，违反不可绕过的审批/调用通道边界。
- `major`：M-01。Skill、entry points 和冻结 Tool contract 不一致，核心工作流不可按声明可靠运行。
- `major`：M-02。manifest 没有绑定当前 review commit 和 v2.2.0 计划，核心证据不可复现/不可追溯。
- `major`：M-03。当前代码没有证实 frozen Project trust/fingerprint gate，R1/R2 项目安全前置不足。
- 未将额外截图、Markdown 排版、非关键性能整理或历史文档状态误升为 blocker/major。

## reproduction steps

1. 在 clean checkout 执行：

   ```text
   git rev-parse HEAD
   git status --short --branch
   ```

   结果为 HEAD `92ef68a6cb2d52609cee770e788d06eef43dc8f9`，工作树 clean。

2. 枚举实际 nanobot Tool 注册：

   ```text
   uv run python -c "from importlib.metadata import entry_points; print('\\n'.join(sorted(f'{ep.name}={ep.value}' for ep in entry_points(group='nanobot.tools') if ep.name.startswith('devpilot_'))))"
   ```

   结果包含 `devpilot_execute_changeset`、`devpilot_verify_changeset`、`devpilot_rollback_changeset`。

3. 查看 `src/devpilot/nanobot_tools.py:837-1057`，可复现三个 Tool 的 `approval_token` 输入和 `read_only=False`；查看 `pyproject.toml:19-30` 可复现 entry point 注册。

4. 查看 Skill `skills/catalog/feature-delivery/SKILL.md:42-56`，与 entry point 列表对比，可复现允许 Tool 集不一致且不存在独立 request-approval/diff entry point。

5. 查看 `docs/evidence/phase-4/manifest.json:5-11,120-128`，再执行：

   ```text
   git rev-parse HEAD
   Get-FileHash docs/evidence/phase-4/manifest.json -Algorithm SHA256
   Get-FileHash uv.lock -Algorithm SHA256
   ```

   可复现 manifest 的 commit/plan 字段仍为旧值，且其 lock hash 与当前文件不一致。

6. 执行已授权的确定性回归：

   ```text
   uv run pytest -q tests/test_p4_feature_delivery.py tests/test_p2a_migration.py tests/test_p2b_control_plane.py tests/test_p2b_durable_control_plane.py
   uv run pytest -q
   uv run ruff check src scripts tests
   uv run pyright
   uv lock --check
   uv run python scripts/secret_scan.py
   uv run python scripts/check_markdown.py
   ```

   结果：pytest、Ruff、Pyright、lock、Secret、Markdown 检查通过；完整 pytest 有 3 个 symlink 权限/平台条件跳过。默认 `uv run alembic check` 因 clean checkout 没有 `local-data/devpilot.db` 失败；以 `DEVPILOT_DATABASE_URL=sqlite://` 执行 `uv run alembic upgrade head` 可从空内存库升级至 `0006_p4_contract_binding`。

## recommendation

在重新启动独立 Review 前，主 Agent/实现责任人应：

1. 移除或隔离所有 nanobot 直连的 R1/R2 Execute/Verify/Rollback Tool；nanobot 侧只保留 proposal、读取状态/Diff 和请求审批所需的 proposal-only contract。R1/R2 必须由外部控制面用户动作触发，token 不得成为模型 Tool 参数、Prompt、Session 或 SSE 内容。
2. 冻结一份单一 Tool contract：明确 plan、propose、request approval、get state、diff 的实际命名/注册/schema；让 Skill allowed list、entry points、描述、`additionalProperties=false` 嵌套 schema 和错误包由合同测试逐项校验。只有完成 independent Review 后再推进 Skill lifecycle。
3. 重新生成 `docs/evidence/phase-4/manifest.json` 和相关 evidence，使 `commit`、`review_target`、artifact hashes、uv lock hash、测试结果和 plan version/SHA 全部指向 `92ef...` 所代表的最终评审树及冻结计划 v2.2.0；重新运行核心 Golden 并记录真实输出。
4. 恢复/实现 Project trust、repo fingerprint 和 allowed-root 绑定的服务端 gate，证明未 trusted、路径指纹变化或恢复未知状态时 R1/R2 fail closed；补充跨项目、跨 Run、跨 Task 的拒绝测试。
5. 修复后必须在新的 clean checkout 上重跑本报告第 6 步，并提交新的 evidence manifest 与独立 Review。P4-02 只有在 B-01/M-01/M-02/M-03 全部关闭并复测通过后才能解锁。

责任人与期限：

| follow-up | 责任人 | 证据路径 | 期限 |
|---|---|---|---|
| B-01 Tool/approval boundary | DevPilot 主 Agent/实现责任人 | `src/devpilot/nanobot_tools.py`、`pyproject.toml`、新增/更新 `tests/test_p4_feature_delivery.py` contract tests；新的 `docs/evidence/phase-4/manifest.json` | P4-01 重新 Review 前，且必须早于 P4-02 解锁 |
| M-01 Skill/注册契约 | DevPilot 主 Agent/实现责任人 | `skills/catalog/feature-delivery/SKILL.md`、`pyproject.toml`、Tool registry/schema contract test | P4-01 重新 Review 前，且最迟 P4 Gate 前 |
| M-02 evidence binding | DevPilot 主 Agent/证据维护责任人 | `docs/evidence/phase-4/manifest.json`、`p4-01-acceptance.md`、`g01-runtime-evidence.json` | P4-01 重新 Review 前，且最迟 P4 Gate 前 |
| M-03 Project trust/fingerprint gate | DevPilot 主 Agent/Domain/Storage 责任人 | `src/devpilot/storage.py`、项目策略/状态机实现、跨项目/跨 Run/信任状态 security tests 与 phase-4 evidence | P4-02 解锁前；最迟 P4 Gate 前 |

本次没有可在 P4-02 开始后再处理的 minor follow-up；所有列出的事项均为 blocker/major，不能以 follow-up 形式放行。

## regression risk

- 若保留 B-01，模型或 Skill 上下文可能接触/转发控制面 capability token，形成审批通道与模型 Tool 通道耦合，存在未经符合冻结边界的控制面动作风险。
- 若只修 Skill 文本而不统一 entry points，旧执行 Tool 仍会继续暴露，契约漂移会在后续 bug-fix/test-repair Skill 中复制。
- 若不修 M-02，未来 Review 无法确定 G-01 runtime evidence、实现代码、lock 依赖和冻结计划是否来自同一可复现树。
- 若不修 M-03，仅有 allowed-root 与 Run 归属测试不足以防止未经显式信任的项目进入 R1/R2 流程；后续 P4-02 的缺陷修复工作流会扩大该风险面。
- 现有正向回归基线较好；修复时需要特别保留 Fake-only pre-P6、失败 Artifact、终态保护、幂等、回滚冲突和 P1 Tool 兼容性。

## unanswered questions

1. 冻结计划 §4.1 的“nanobot 只能 R0/请求审批”是否适用于所有全局 entry points（本评审按适用处理），还是仅适用于 `feature-delivery` Skill 的 allowlist？若有例外，必须由冻结计划允许的 ADR 明确记录，不能由 Tool schema 隐含决定。
2. `devpilot_feature_delivery_propose` 与 `devpilot_propose_changeset` 是否保留为两个正式 contract？如果保留，哪一个是 P4-01 canonical path，另一条如何防止绕过 evidence/contract 绑定？
3. P4-01 是否必须在本节点内把 Skill lifecycle 推进到 `reviewed/enabled/published/golden_passed/active`，还是允许在独立 Review 通过后的单独证据提交中推进？无论选择哪一种，都应在 P4 Gate 前有机器可读证据。
4. `/v1/change-sets/{id}/apply` 的 exact user confirmation 是否由控制面 UI 外部持久化/审计，还是仅由 Python Workflow/Tool 层校验？当前 HTTP apply 路由本身只验证外部 token 和幂等键。
5. `ProjectRecord` 缺少 frozen trust/fingerprint 字段是否是 P2A/P2B 未完成的遗留范围，还是 P4-01 预期已经继承但未展示的能力？在该边界澄清前，不应宣称项目安全绑定已完整验收。

## P4-02 解锁结论

**不允许解锁 P4-02。**

解锁所需的精确条件是：B-01、M-01、M-02、M-03 全部关闭；在新的 clean checkout 上以同一实现 commit 重新生成 manifest；重新通过 P4 Golden、Tool contract/security tests、pytest、Ruff、Pyright、lock、Secret、Markdown 和迁移检查；由新的独立只读 Review 复核并将 decision 改为 `approve` 或仅包含明确登记的非阻塞 minor follow-up。
