# P4-01 Product Review Final

## Decision

`blocked`

P4-01 不通过，P4-02 不允许开始。必须修复以下 P1/P2 项后重新进行独立 Review。

## Findings

### P1 — P6 fail-closed 仍不是全局、不可伪造的门禁

`ControlPlaneManager.verify()` 在无 gate 时会拒绝非 Fake 后端，但 `P6ExecutionGate.permits()` 只比较后端名称和过期时间，不校验 `capability_hash`、验证套件、版本或服务端不可变证明。使用任意未来 gate 记录即可让任意同名后端执行；实际复核已用任意 gate 值使 rogue backend 返回 `verified`。`DEVPILOT_ENABLE_FC_SANDBOX=1` 仍会注册 FC 后端，不能替代 P6 证明。

证据：`src/devpilot/control_plane.py:301-320,719-742`、`src/devpilot/execution_gate.py:20-55`；复核命令结果：任意 `P6ExecutionGate('rogue','0'*64,'unverified', future)` 下非 Fake 后端 `calls=1`、状态 `verified`。

### P1 — G-01 仍未覆盖冻结的真实 Golden 链路

测试确实覆盖了 HTTP proposal、Fake 成功和显式 rollback，但 G-01 只把 FastAPI 源码写入 fixture，未真正启动/调用新增受限接口；`search/read` 仅被断言执行，传入 Proposal 的 evidence id 仍只是 inspect 的 run id，未验证 evidence 类型、内容/hash provenance 或与计划绑定。主成功路径还直接调用 `manager.approve()`，没有完整证明控制面用户审批请求/批准/一次性消费边界；失败、Artifact failure 和状态终态也不在 G-01 中覆盖。

证据：`tests/test_p4_feature_delivery.py:35-123,147-215`、`src/devpilot/workflows.py:118-180`。

### P2 — 失败/Artifact failure 的终态与事件一致性仍不完备

通用 backend 异常在 `ControlPlaneManager.verify()` 中记录 Run failed，却未把 ChangeSet 从 `VERIFYING` 转为确定失败状态；`store.record_artifact()` 的数据库失败不在 Workflow 捕获范围内，可能留下已完成 Run 但无 Artifact。`finish_run()` 允许已终态 Run 再次改写并追加第二个终态事件，Artifact 写失败路径因此可能产生 `run.completed` 后 `run.failed`。现有测试只覆盖非零 Fake，不覆盖这些故障路径。

证据：`src/devpilot/control_plane.py:346-361,386-394,704-706`、`src/devpilot/workflows.py:243-264`、`src/devpilot/storage.py:269-287`。

### P1 — phase-4 manifest 与本次审查 commit 不一致，Gate 证据不可追溯

已提交的 `docs/evidence/phase-4/manifest.json` 的 `commit` 仍是 `068ac07dc36dbae726bddce8dff810b57c0e78fc`，而审查目标是 `722d724b1562af361e74596e9bbcbe8cc3a69f03`。其中 acceptance/review artifact hash 与文件本身一致，依赖锁 hash、迁移 head、时间和已知用户修改也已记录；但错误的 commit 指针使 manifest 不能证明当前实现树，且未提供完整配置摘要/clean-tree 状态字段。

证据：`docs/evidence/phase-4/manifest.json:5-78`；当前 `HEAD=722d724b1562af361e74596e9bbcbe8cc3a69f03`，工作树仍有两处已知用户 fixture 修改。

### P2 — Skill 的声明工具已接入但未全部形成可复现测试合同

`/v1/feature-delivery/proposals` 与 `devpilot_feature_delivery_propose` 已实现，`pyproject.toml` 也注册了 Skill 声明的五个工具；但现有回归直接测试了 HTTP 路由和旧 `devpilot_propose_changeset` adapter，没有直接测试 `FeatureDeliveryProposeTool` 的请求/响应、错误映射和 proposal-only 行为。因此“声明的 Tool 均已注册并测试”尚未成立。

证据：`pyproject.toml:19-28`、`src/devpilot/control_plane.py:787-800`、`src/devpilot/nanobot_tools.py:621-675`、`tests/test_p2b_control_plane.py:217-249`。
