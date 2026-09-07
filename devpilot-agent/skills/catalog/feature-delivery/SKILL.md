---
name: feature-delivery
description: Turn an approved feature request into a bounded, reviewable ChangeSet proposal and verified delivery.
metadata:
  devpilot:
    version: 0.1.0
    status: enabled
    reviewed: true
    allowed_tools:
      - devpilot_inspect_project
      - devpilot_search_code
      - devpilot_read_code
      - devpilot_feature_delivery_propose
      - devpilot_propose_changeset
      - devpilot_request_approval
      - devpilot_publish_artifact
      - devpilot_get_changeset
      - devpilot_get_delivery_report
      - devpilot_get_run_state
      - devpilot_watch_run
      - devpilot_confirmed_action
    risk_level: R1
    source: catalog
---

# Feature delivery

## P7 task profile contract

When selected through nanobot's `feature-delivery@1` task profile, keep the response
structured as `facts_and_evidence`, `change_summary`, `verification_result`, and
`next_step`. The profile narrows visible tools for this session; it does not grant
approval, Apply, verification, rollback, credential, or sandbox authority. If a
session resumes after a material project change, re-check the relevant evidence
before proposing a new ChangeSet.

## 适用场景

用户明确提出一个可交付的新功能，并希望先看到影响范围、风险、Diff 和验证方案，
再决定是否应用。适用于单个受信任项目内的有限代码变更。

## 禁用场景

不得用于模糊需求、生产仓库、批量重构、依赖安装、数据库手工修改、密钥或凭据文件、
任意 Shell 命令、未登记项目、未经用户批准的写入，或 P6 Sandbox Gate 尚未通过时的
自主修复循环。不得把 Fake 或 GuardedLocal 验证描述为真实 Sandbox 隔离。

## 前置条件

- 当前项目已登记，真实路径位于服务端配置的 `DEVPILOT_ALLOWED_ROOTS`。
- 主 Agent 已完整读取当前冻结实施计划，并记录计划版本、哈希、当前节点和回滚点。
- 用户需求已拆成明确的验收结果、允许修改文件、验证 profile 和停止条件。
- 已有 `inspect/search/read` 事实证据；没有事实证据时不得生成补丁。
- 控制面、数据库迁移和所需的 Fake ExecutionBackend 已通过启动检查。

## 允许 Tool

- `devpilot_inspect_project`：确认技术栈、入口、测试和风险，R0 只读。
- `devpilot_search_code`：在已登记项目中进行有界文本检索，R0 只读。
- `devpilot_read_code`：读取有界源码行并取得前置 SHA-256，R0 只读。
- `devpilot_feature_delivery_propose`：使用已绑定的 inspect/search/read 证据创建 FeatureDelivery
  Proposal；只能提案，不能请求审批或写项目文件。
- `devpilot_propose_changeset`：创建不可变 Preview/Diff；只能提案，不能批准或写文件。
- `devpilot_request_approval`：登记用户对精确 ChangeSet 的审批请求；不签发、不接收或返回 token。
- `devpilot_publish_artifact`：将已生成的安全项目摘要写入服务端 Artifact 根；不写项目目录。
- `devpilot_get_changeset`：读取服务端保存的 ChangeSet、Diff、哈希和完整契约；只读。
- `devpilot_get_delivery_report`：读取脱敏交付结果、Artifact 元数据和可用决策；只读。
- `devpilot_get_run_state`：读取服务端 Run 状态和已脱敏事件；只读。
- `devpilot_watch_run`：读取脱敏阶段事件，支持断点重放和持久快照降级；只读。
- `devpilot_confirmed_action`：仅把 Gateway 签名的用户确认交给受信任 Broker；不接受 token。

Skill 只能建议上述已注册 Tool。ChangeSet 的 Diff 通过 `devpilot_propose_changeset` 返回，
之后可用 `devpilot_get_changeset` 或 `devpilot_get_run_state` 查询服务端事实。用户在对话中明确确认精确的 ChangeSet
后，运行时可以调用 `devpilot_request_approval` 登记审批意图；真正的 Apply/Verification/Rollback
由控制面用户动作携带 capability token 完成。Skill 不得伪造确认、读取或传播审批令牌。

交付完成后，优先使用 `devpilot_get_delivery_report` 汇总事实。接受或停止必须由用户在
当前 nanobot 对话中明确确认；回滚只能转到现有的 R1 rollback 审批入口，不能由 Skill
自行取得 capability 或执行回滚。

## 执行步骤

1. 完整读取冻结计划，校验当前节点和前置 Gate；无法读取时立即停止。
2. 调用 `devpilot_inspect_project`，确认项目边界和技术栈。
3. 使用 `devpilot_search_code` 和 `devpilot_read_code` 定位最小实现位置，并保存 evidence id、
   文件路径、行号和前置哈希。
4. 生成 Change Plan：写明需求、非目标、允许文件、风险、验证 profile 和停止条件。
5. 生成不超过服务端限额的 FilePatch，并调用 `devpilot_propose_changeset`。
6. 向用户展示 ChangeSet id、patch hash、文件列表、Diff 摘要、风险、验证方式和回滚方式。
7. 明确停止并等待用户对这一个精确 ChangeSet 回复“执行”或拒绝；在等待期间不得调用 Apply。
8. 用户确认后，由运行时受控入口完成 Apply 和服务端 allowlist Verification；P4 阶段仅接受 Fake
   验证作为自动化 Golden 路径，真实自动修复必须等待 P6 Gate。
9. 输出结构化交付结果：Apply 状态、Verification 状态、后端能力、退出码、Artifact id、
   patch hash 和 evidence 引用。验证失败不得宣称功能完成，应保留失败证据并停止。

## 输出契约

提案阶段必须输出：

- `workflow=feature-delivery`；
- `change_set_id`、`patch_hash`、`scope_hash` 和项目标识；
- 需求事实、非目标、允许修改文件和最小 Diff；
- 验证 profile、停止条件、回滚方式和关联 evidence id；
- `requires_user_confirmation=true`，并明确当前未修改任何项目文件。

交付阶段必须输出 Apply 与 Verification 的结构化状态；成功生成服务端拥有的
`feature-delivery.md`，验证失败或 Artifact 持久化失败则生成
`feature-delivery-failure.md`。Artifact 只保存元数据和安全摘要，不复制 stdout/stderr、
凭据或完整环境；所有状态必须来自 Tool/控制面结果，失败状态不得伪装成成功。

## 失败处理

路径越界、秘密文件、前置哈希变化、补丁冲突、审批缺失、后端不可用、验证失败或 Artifact
写入失败时，立即停止并返回稳定错误类别。不得改用 nanobot Shell、重新扩大文件范围、
自动回滚未获用户确认的变更或编造成功结果。需要重试时必须生成新的子 ChangeSet 或由
用户明确选择恢复路径。
