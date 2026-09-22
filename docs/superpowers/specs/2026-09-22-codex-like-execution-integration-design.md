# Codex-like Execution Integration 设计

## 状态

- 日期：2026-09-22
- 状态：待用户确认后进入实现计划
- 目标分支：`codex/h11-complete-handoff`
- 设计范围：在现有 DevPilot Runtime、Control Plane、FC Sandbox 和 nanobot 工具体系上，补齐“可控 Full Access”下的项目自主执行闭环。

## 1. 背景与问题

当前项目已经完成以下基础能力：

- 项目检查、项目信任和项目测试工具；
- FC 沙箱执行和结果持久化；
- Failure Run 与 bug-fix Skill 的绑定；
- ChangeSet、Diff、Apply、Verify、Rollback 代码交付闭环；
- 当前 Run 内的临时工具生成、校验、激活和清理；
- Skill 迭代、调度和恢复相关能力。

但在真实外部项目验证中，端到端流程仍存在断点：

1. 用户已经在 Full Access 下明确指定项目，但测试入口仍可能卡在 `PROJECT_TRUST_REQUIRED`，没有“当前会话接入项目”的完整动作。
2. 新项目的 Python 或 Node 依赖没有统一的隔离准备流程。项目没有预装 `pytest` 或 npm 依赖时，Agent 不能自行完成环境准备。
3. 测试执行只暴露固定入口，环境准备、测试执行、失败恢复之间没有统一的协调层。
4. `CONTROL_PLANE_ERROR` 等错误目前缺少稳定的错误分类、恢复建议和有限重试信息，Agent 难以判断是项目失败、环境失败还是控制层暂时失败。
5. 临时工具机制解决的是“当前 Run 缺少工具”，不应被用来绕过项目执行策略，也不能替代项目环境初始化。

本设计要解决的是新的集成缺口，不否定已完成的 H1-H11 和 T1-T7 计划节点。

## 2. 目标

在用户选择 Full Access 且明确给出项目后，Agent 能在一个 Run 内自主完成：

```text
项目发现
  -> 当前会话接入
  -> 能力和依赖预检
  -> 隔离环境准备
  -> 固定 Profile 测试
  -> 结果持久化
  -> Failure Run
  -> bug-fix Changeset
  -> Apply
  -> Verify
  -> 有界恢复或 Rollback
```

用户不需要手工先执行 `uv sync`、`pip install`、`npm install` 或手工登记项目。Agent 必须报告每一步的真实结果，不能因为控制层或环境失败而伪造测试成功。

## 3. 非目标

本次不做以下事情：

- 不创建第二个 Agent Runtime、第二套 Loop、第二个 WebUI 或新的后端服务；
- 不允许模型提交任意宿主机 Shell 命令作为测试命令；
- 不让模型生成未校验的安装脚本并直接执行；
- 不把临时工具自动变成跨会话永久工具；
- 不移除普通代码修改的 ChangeSet、Apply、Verify、Rollback 门禁；
- 不自动向外部服务发送代码、日志、Token 或个人数据；
- 不自动提交 Git、发布部署或删除用户未明确授权的数据；
- 不建设新的远程 Skill 市场或插件市场。

## 4. 核心设计

### 4.1 统一执行协调层

在现有 Runtime 内增加轻量的执行协调能力，负责编排已有组件，而不是替代它们。协调层至少包含以下阶段：

1. `discover`：解析项目路径、项目类型、入口和测试文件；
2. `enroll`：校验项目是否位于允许根目录内，并将用户明确指定的项目绑定到当前会话；
3. `preflight`：检查锁文件、依赖声明、测试 Profile、工具链和执行环境；
4. `prepare`：在 FC/隔离环境中创建或复用项目环境，并按服务器选择的 Profile 准备依赖；
5. `test`：执行服务器拥有的固定测试命令；
6. `persist`：保存环境指纹、命令 Profile、退出码、日志摘要和文件快照信息；
7. `recover`：对可恢复的控制层、环境初始化和孤儿 Run 情况做有界重试；
8. `handoff`：把真实失败 Run 交给现有 bug-fix Skill 和 ChangeSet 流程。

协调层不能改变现有的审批语义：它可以自动准备环境和运行测试，但不能代替普通代码交付的最终 Apply 确认。

### 4.2 当前会话项目接入

当用户在 Full Access 下明确提供项目路径时：

- 服务端先通过 `AllowedRoots` 做绝对路径和目录边界校验；
- 项目只在当前会话内自动登记和信任；
- 项目 ID 与会话 ID、工作区根目录绑定；
- 重启后不自动恢复未持久化的信任；
- 用户要求跨会话继续使用时，单独触发持久化确认；
- 项目接入不等于允许读取隐私文件，也不等于允许外发数据。

这样可以消除正常 Full Access 流程中的重复 `PROJECT_TRUST_REQUIRED`，但不会把任意路径变成全局可信路径。

### 4.3 项目类型与执行 Profile

模型不提交原始命令字符串。服务端根据项目文件选择固定 Profile，并将 Profile ID 传给 FC：

| 项目特征 | Profile | 执行策略 |
| --- | --- | --- |
| `pyproject.toml` + `uv.lock` | `python-uv-pytest` | 使用锁文件创建隔离 Python 环境并运行 pytest |
| `requirements.txt` 或 `pyproject.toml` | `python-pytest` | 使用固定 Python bootstrap 安装声明依赖并运行 pytest |
| `package.json` + lockfile | `node-npm-test` | 使用锁文件安装 npm 依赖并运行项目测试脚本 |
| 无法识别 | `unknown` | 返回可行动诊断，不执行猜测命令 |

具体 Profile 的命令、网络策略、超时、工作目录和依赖来源均由服务端固定。模型只能选择项目和目标测试范围，不能覆盖后端、权限、网络和命令。

### 4.4 隔离环境准备

环境准备必须发生在 FC/隔离执行环境中：

- 依赖安装使用项目锁文件优先；
- 安装过程保存依赖来源和结果摘要；
- 环境按项目指纹和锁文件哈希缓存；
- 同一项目重复 Run 可以复用成功环境；
- 锁文件变化后创建新环境版本；
- 网络默认关闭，只有 Profile 明确允许时才打开；
- 依赖安装失败不能被记录为测试失败，必须标记为 `environment_failed`；
- 不允许把宿主机密钥、用户目录或未声明文件带入 FC。

### 4.5 结构化错误与恢复

控制层和执行层需要返回稳定错误类别，而不是只返回一个笼统的 `CONTROL_PLANE_ERROR`。至少包括：

- `project_boundary_rejected`
- `project_enrollment_required`
- `profile_unsupported`
- `dependency_prepare_failed`
- `test_process_failed`
- `test_assertion_failed`
- `sandbox_unavailable`
- `sandbox_timeout`
- `control_plane_transient`
- `orphan_run_recoverable`
- `retry_exhausted`

每个错误需要包含：

- 可读错误说明；
- 稳定错误码；
- 是否允许重试；
- 建议的下一步；
- 原始 Run ID 和 sandbox ID；
- 是否已经产生副作用；
- 是否需要用户确认。

只有标记为可恢复且幂等的错误才允许自动重试。重试必须有次数、时间和 Run 去重限制。

### 4.6 与 bug-fix Skill 的衔接

测试失败时保留真实失败事实：

- 退出码；
- 测试收集结果；
- 失败测试名称；
- 标准输出和标准错误摘要；
- 项目版本和环境指纹；
- 代码快照或 Revision/CAS 信息。

Failure Run 可以直接绑定 bug-fix proposal，但仍需经过：

```text
proposal -> Diff -> 用户确认 -> Apply -> Verify -> Rollback（必要时）
```

环境失败或控制层失败不得伪装成代码缺陷，也不能直接生成“修代码”的 Changeset。

### 4.7 临时工具边界

现有临时工具系统继续保持：

- 只在当前 Run 或当前会话生效；
- 必须经过 FC 校验后才能激活；
- Run 结束后清理；
- 默认不跨会话恢复；
- 持久化必须单独询问用户；
- 涉及隐私读取、外部发送或敏感数据回显仍需确认。

本设计不通过临时工具生成任意 Shell 执行器。项目环境准备和测试执行必须走服务器拥有的固定 Profile。

## 5. Full Access 权限语义

### 当前会话内自动允许

- 接入用户明确指定且位于允许根目录内的项目；
- 创建和复用隔离环境；
- 按固定 Profile 安装项目声明的依赖；
- 执行项目测试；
- 保存测试结果、日志摘要和失败 Run；
- 对可恢复的执行错误做有界重试；
- 生成临时工具并在当前 Run 内使用。

### 仍需确认

- 读取密钥、Token、个人隐私文件；
- 向外部服务发送项目代码、日志或数据；
- 将临时工具、项目授权或环境能力持久化到后续会话；
- 普通代码 Changeset 的最终 Apply；
- 超出用户明确项目范围的文件删除、覆盖或迁移。

## 6. 数据与生命周期

执行协调层应复用现有持久化结构，至少关联：

- `session_id`
- `project_id`
- `run_id`
- `sandbox_id`
- `profile_id`
- `project_revision`
- `dependency_fingerprint`
- `execution_status`
- `exit_code`
- `error_code`
- `retry_count`
- `failure_run_id`

生命周期：

```text
discovered
 -> enrolled
 -> preflighted
 -> preparing
 -> ready
 -> running
 -> passed / failed / environment_failed / control_plane_failed
 -> recovery_pending
 -> recovered / retry_exhausted
 -> handed_off
 -> cleaned
```

## 7. 验收场景

### 场景 A：Python 缺陷项目

对 `loop-engineering-demo` 的 `seed_repo` 执行：

```text
基线：5 failed, 5 passed
Agent 自动准备环境
Agent 识别失败并生成修复 Changeset
Apply 后 Verify
目标：10 passed
```

### 场景 B：新项目无预信任记录

用户在当前会话直接给出项目路径，Agent 应自动完成当前会话接入，不再出现无法恢复的 `PROJECT_TRUST_REQUIRED`。

### 场景 C：依赖未安装

项目没有本地虚拟环境或 `node_modules`，Agent 应在隔离环境中完成准备，并把准备结果与测试结果分开记录。

### 场景 D：依赖安装失败

Agent 应报告 `dependency_prepare_failed`，给出具体依赖和恢复建议，不生成虚假的代码修复结果。

### 场景 E：控制层瞬时失败

可恢复的控制层错误最多按固定上限重试；每次重试复用幂等键，不能重复创建项目、Run 或 Changeset。

### 场景 F：测试失败后修复

失败 Run 能被 bug-fix Skill 消费，生成 Changeset，经确认 Apply 后重新 Verify；Verify 失败时可回滚。

### 场景 G：临时工具生命周期

当前 Run 激活的临时工具在 Run 结束后被清理，下一会话不能自动使用，除非用户明确同意持久化。

## 8. 验证指标

- 新项目从路径到首次测试执行不再因未预信任而直接失败；
- Python/Node 固定 Profile 覆盖率达到当前支持项目类型的 100%；
- 环境失败与代码测试失败分类准确率 100%；
- 所有成功或失败执行都有真实退出码或明确的控制层错误码；
- 自动重试不会产生重复 Run、重复 Changeset 或重复 Apply；
- 缺陷项目修复后 Verify 结果可复现；
- 隐私和外发确认路径保持有效；
- 临时工具不会跨 Run 泄漏；
- 现有 H1-H11、T1-T7 回归测试全部通过。

## 9. 实施阶段

实现计划将在本设计确认后单独生成，建议分为：

1. 执行状态和错误模型；
2. 当前会话项目接入；
3. Profile 预检与隔离环境准备；
4. 测试执行协调和有界恢复；
5. Failure Run 与 bug-fix 绑定；
6. WebUI/Agent 可观测性；
7. 全量回归、真实缺陷项目验收和 subagent review。

## 10. 明确的设计决策

- 采用可控 Full Access，不开放模型任意宿主机 Shell。
- 当前会话自动接入用户明确指定的项目，但只在允许根目录内生效。
- 依赖安装和测试执行统一在 FC/隔离环境中完成。
- 模型选择项目和目标范围，服务端选择执行 Profile 和具体命令。
- 代码交付仍保留 ChangeSet、Apply、Verify、Rollback。
- 隐私读取、外部发送和权限持久化仍需再次确认。
- 临时工具不作为环境执行策略的逃生通道。
