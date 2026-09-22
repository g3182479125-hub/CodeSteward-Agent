# Codex-like Execution Integration 实施计划

## 目标

在现有 nanobot 单 Runtime、DevPilot Control Plane 和 FC 执行链上，实现设计文档
`docs/superpowers/specs/2026-09-22-codex-like-execution-integration-design.md`
定义的可控 Full Access 项目执行闭环。

用户明确给出项目后，Agent 能在当前会话内自动完成项目接入、能力预检、隔离环境准备、
固定 Profile 测试、失败分类、有限恢复和 Failure Run 衔接；代码交付仍保持
ChangeSet、Apply、Verify、Rollback，隐私读取和外发仍需要确认。

## 当前进度（2026-09-23）

- 已完成：执行结果错误分类、Full Access 当前 Run 项目接入、服务器固定 Profile、嵌套测试目录解析、依赖指纹、WebUI 任务绑定和验证失败 Run 收尾。
- 已完成：Full Access 代码修改现在只生成 ChangeSet/Diff；Apply/Verify 必须经过已有的用户确认 Broker，已移除无确认的直通 Apply/Verify 接口。
- 已验证：真实隔离缺陷项目基线为 `5 failed, 5 passed`，修复三处业务缺陷后通过 `10 passed`；组合回归命令退出码为 `0`。
- 仍待完成：更完整的独立 prepare 持久化阶段、控制层有限重试协调器、跨重启恢复矩阵，以及 Standards/Spec 双轴 review 的最终收口。

## 现状基线

已存在并复用：

- `devpilot-agent/src/devpilot/inspector.py`：项目事实检查和路径边界；
- `devpilot-agent/src/devpilot/control_plane.py`：项目登记、信任、项目测试、ChangeSet；
- `devpilot-agent/src/devpilot/test_runs.py`：服务器拥有的 Python/Node 测试 Profile、快照、Failure Run；
- `devpilot-agent/src/devpilot/execution.py`、`fc_sandbox.py`：FC 执行与生命周期；
- `devpilot-agent/src/devpilot/storage.py`：Project/Run/Event/Failure 记录；
- `devpilot-agent/src/devpilot/nanobot_tools.py`：Agent 工具契约和控制层错误映射；
- 现有 `temporary_tools.py`、`model_plugins.py`：当前 Run 工具生命周期，不作为任意命令执行器。

已知缺口：

1. `ProjectTestRunner.run()` 在测试前要求持久信任，当前会话没有自动接入语义；
2. 测试 Profile 没有独立的依赖/环境预检和准备结果；
3. 测试请求的错误类别不足以驱动自动恢复；
4. 控制层、FC、环境准备和测试断言失败没有统一执行状态；
5. Agent 工具目前只能直接调用测试入口，不能获得“先接入、再准备、再测试”的可恢复流程。

## 约束

- 不新增第二个 Runtime、Loop、WebUI、数据库或独立后端；
- 不允许模型提交任意宿主机命令、后端或权限参数；
- 依赖安装和测试执行使用服务器固定 Profile，在 FC/隔离环境完成；
- 用户明确指定且位于 `AllowedRoots` 内的项目可以当前会话自动接入；
- 普通代码修改仍经过 ChangeSet、Apply、Verify、Rollback；
- 不读取或上传敏感文件，隐私读取/外发/持久化仍需确认；
- 不修改已完成 H1-H11、P0-P8、T1-T7 的历史契约，发现集成缺口时做最小增量；
- 每个阶段完成后固定候选 SHA，运行受影响回归，再做 Standards/Spec 双轴 review。

## 实施步骤

### 1. 固定执行状态与错误模型

**目标**：让 Agent 能区分项目边界、接入、环境、测试、控制层和恢复失败。

**入口**：

- `devpilot-agent/src/devpilot/test_runs.py`
- `devpilot-agent/src/devpilot/control_plane.py`
- `devpilot-agent/src/devpilot/execution.py`
- `devpilot-agent/src/devpilot/nanobot_tools.py`
- `devpilot-agent/tests/test_agent_test_runs.py`

**工作**：

1. 先定位当前 `ProjectTestRunResult`、`ExecutionResult`、Run event 和错误码契约，
   不另造与现有存储冲突的结果模型。
2. 为项目测试结果补充稳定的执行阶段/错误分类，至少覆盖：
   `project_boundary_rejected`、`project_enrollment_required`、`profile_unsupported`、
   `dependency_prepare_failed`、`test_process_failed`、`test_assertion_failed`、
   `sandbox_unavailable`、`sandbox_timeout`、`control_plane_transient`、
   `orphan_run_recoverable`、`retry_exhausted`。
3. 保留兼容的 `status`、`run_id`、`failure_run_id`、`exit_code`、`command` 字段；
   新字段只增加可选信息，旧 Agent 工具仍能解析。
4. 让 `_http_error_message` 和 `ProjectTestRunTool` 返回可读的稳定错误码、是否可重试、
   Run ID 和下一步建议；不把异常堆栈或凭据回显给模型。
5. 控制层瞬时错误只允许在幂等键存在且服务端标记可恢复时重试，不能重复创建 Run。

**测试**：

- 错误分类和序列化单测；
- 旧响应字段兼容测试；
- 控制层错误映射测试；
- 不完整/未知 receipt 不能变成成功；
- 同一幂等键不创建第二个 Run。

**完成条件**：任何测试失败都能明确判断是环境、控制层、测试进程还是断言失败，且不改变已有 Failure Run 语义。

### 2. 当前会话项目接入

**目标**：在 Full Access 下，用户明确给出的合法项目不再因为缺少预先信任记录而卡死。

**入口**：

- `devpilot-agent/src/devpilot/control_plane.py`
- `devpilot-agent/src/devpilot/storage.py`
- `devpilot-agent/src/devpilot/paths.py`
- `devpilot-agent/src/devpilot/nanobot_tools.py`
- `devpilot-agent/tests/test_agent_test_runs.py`
- 现有 session/request context 与 H11 Full Access 策略入口

**工作**：

1. 新增或扩展一个服务器拥有的“当前会话接入”内部动作，复用现有
   `AllowedRoots.require_project()`、`binding_for()`、`ensure_project()` 和 trust 存储。
2. 接入动作必须绑定 `session_id`、项目 ID、允许根 ID、仓库指纹和访问模式；
   缺少会话上下文时保持原有 fail-closed 行为。
3. 只有 Full Access 且项目路径由用户当前请求明确给出时允许自动信任当前会话；
   Default 模式继续要求显式项目 trust。
4. 不能把接入授权扩展成敏感文件读取、外部网络发送、跨会话持久化或代码 Apply 授权。
5. Run 结束清理临时会话绑定；若用户要求跨会话复用，走已有的持久化确认语义。
6. 在项目测试前由协调层调用接入动作，避免模型先手工调用多个容易失败的工具。

**测试**：

- Full Access 合法项目自动接入并可测试；
- Default 项目仍拒绝未信任请求；
- 越界路径、符号链接逃逸和未知会话仍拒绝；
- 不同会话之间不共享临时信任；
- 接入不会改变 Apply/Verify/Rollback 授权。

**完成条件**：真实测试流程不再因用户明确指定项目却缺少预信任而直接返回 `PROJECT_TRUST_REQUIRED`。

### 3. Profile 预检和隔离环境准备

**目标**：在固定服务器 Profile 下自动识别 Python/Node 项目，并把依赖准备和测试分开记录。

**入口**：

- `devpilot-agent/src/devpilot/test_runs.py`
- `devpilot-agent/src/devpilot/fc_sandbox.py`
- `devpilot-agent/src/devpilot/execution.py`
- `devpilot-agent/src/devpilot/verification.py`
- `devpilot-agent/src/devpilot/storage.py`
- `devpilot-agent/tests/test_fc_sandbox_contract.py`
- `devpilot-agent/tests/test_agent_test_runs.py`

**工作**：

1. 复用项目快照识别 `pyproject.toml`、`uv.lock`、`requirements.txt`、
   `package.json` 和 npm lockfile；识别结果只用于选择服务端 Profile。
2. 扩展实际支持的 Profile 契约，优先复用 ADR-0008 的服务端 pytest bootstrap；
   不接受模型传入安装命令、网络参数或自定义后端。
3. 将环境准备结果单独记录为 prepare 阶段，区分：成功、依赖失败、工具链缺失、
   网络/供应商失败、超时和未支持项目。
4. 对锁文件和项目声明生成依赖指纹；成功环境可按项目/锁文件/模板指纹复用，
   锁文件变化必须失效旧环境。
5. 继续排除 `.env`、凭据、虚拟环境、缓存和敏感文件；上传清单必须可审计。
6. 对 Node 支持只暴露已有且有证据的 `node-test` 能力；不为了覆盖率宣称未验证的 npm bootstrap。

**测试**：

- uv/pytest、requirements/pytest、Node lockfile 的识别和 Profile 选择；
- 环境成功、依赖安装失败、缺工具链、锁文件变化和缓存失效；
- 依赖准备失败不创建代码 Failure Run；
- 上传排除、网络策略和输出限制回归；
- FC 重启后按 sandbox ID 恢复，不重复创建环境或测试。

**完成条件**：项目没有本地 pytest 或 node_modules 时，Agent 可以在隔离环境完成准备；准备失败时给出环境错误而不是伪造测试结果。

### 4. 执行协调层与有界恢复

**目标**：把接入、预检、准备、测试、持久化和恢复串成一个 Agent 可调用的动作。

**入口**：

- 现有 `ControlPlaneManager.run_project_test()`
- `ProjectTestRunner`
- `execution.py` / `fc_sandbox.py`
- `nanobot_tools.py`
- `storage.py` 的 Run/Event 查询接口

**工作**：

1. 在现有 Runtime 内增加最小协调器或服务方法，不创建新的 Loop；协调阶段使用已有 Run/Event。
2. 对外提供一个高层“准备并测试”工具或扩展现有 `devpilot_test_run` 参数，
   让模型只提交项目路径、目标类型和测试范围；服务端完成其余阶段。
3. 每个阶段写入状态和关联 ID：`session_id`、`project_id`、`run_id`、`sandbox_id`、
   `profile_id`、项目 revision、依赖指纹、错误码、重试次数和 `failure_run_id`。
4. 只对幂等、无重复副作用的控制层/恢复查询做有限重试；测试进程非零退出不自动反复重跑。
5. 对控制层返回 409、FC 暂时不可用、sandbox 超时、清理失败和未知状态分别处理；
   未知副作用必须查询状态或阻止重放。
6. 测试通过后不创建 Failure Run；真实非零退出才创建 Failure Run；环境或控制层失败不能进入 bug-fix。

**测试**：

- 完整阶段状态转移；
- transient error 有界重试和幂等；
- sandbox timeout/cancel/recovery；
- 清理失败可见且不报告完整成功；
- 失败 Run 与 bug-fix proposal 绑定；
- 通过、环境失败、控制层失败、测试失败四类结果互不混淆。

**完成条件**：单次 Agent 请求可以从项目路径走到真实测试结果，并在失败时给出可继续执行的下一步。

### 5. Failure Run 到代码交付闭环

**目标**：验证现有 bug-fix、ChangeSet、Apply、Verify、Rollback 在新协调层下不被破坏。

**入口**：

- `devpilot-agent/src/devpilot/test_repair.py`
- `devpilot-agent/src/devpilot/workflows.py`
- `devpilot-agent/src/devpilot/control_plane.py`
- `devpilot-agent/src/devpilot/patches.py`
- `devpilot-agent/tests/test_agent_test_runs.py`
- 现有 Apply/Verify/Rollback 回归集合

**工作**：

1. 只允许 `test_assertion_failed`/真实测试非零退出生成代码 Failure Run。
2. 保留快照哈希、目标文件哈希、退出码、测试输出摘要和环境指纹作为修复事实。
3. 失败 Run 直接供 bug-fix Skill 使用，但提案、Diff、Apply、Verify 和 Rollback 仍走既有服务。
4. Apply 后自动触发同一 Profile Verify；验证失败记录“已应用但验证失败”，不隐式回滚。
5. Verify 成功才报告完成；Apply 失败、验证未运行、验证失败分别展示。

**测试**：

- 真实缺陷项目基线失败 → proposal → Apply → Verify 通过；
- 变更后目标文件哈希不匹配时拒绝使用旧证据；
- Apply 失败回滚；
- Verify 失败保留真实状态；
- Rollback 冲突保护；
- 未确认零写入和重复 Apply 防护。

**完成条件**：代码修复闭环和原有安全门禁全部保留。

### 6. Agent 可观测性与错误体验

**目标**：让模型和用户知道当前在哪一步、失败是什么、是否可以继续。

**入口**：

- `devpilot-agent/src/devpilot/nanobot_tools.py`
- `devpilot-agent/src/devpilot/event_bridge.py`
- 现有 WebSocket/WebUI 事件投影和 Run 查询路径

**工作**：

1. 为 prepare、test、recovery、handoff 复用现有事件流；不新增独立状态页面。
2. Agent 输出固定压缩格式：完成了什么、测试结果、失败原因、Run ID、下一步。
3. 只展示脱敏摘要和必要引用，不把内部 token、密钥、完整环境变量或原始异常堆栈送给模型。
4. 断线后通过 Run 查询恢复状态，不重发测试或 Apply 动作。
5. 当前 Run 的临时工具在结束时清理，并保留已有加粗持久化询问。

**测试**：

- WebSocket 断线/重连；
- 服务重启后查询已完成或未知 Run；
- 错误信息包含可行动建议；
- 敏感字段脱敏；
- 临时工具不跨 Run 泄漏。

**完成条件**：用户不会再看到只有“CONTROL_PLANE_ERROR”的无上下文失败。

### 7. 真实缺陷项目验收和双轴 review

**目标**：用真实外部缺陷项目验证从基线到修复完成的完整路径。

**验收项目**：

- `E:\codex-local\external-agent-tests\loop-engineering-demo` 原始 clone 保持不变；
- 使用隔离副本 `E:\codex-local\nanobot-state\workspace\loop-engineering-demo-external`；
- 基线记录为当前实际结果 `5 failed, 5 passed`，不把 README 根目录导入错误混入业务失败；
- 目标是 Agent 自动准备环境、识别三类缺陷、生成 Changeset、Apply、Verify，最终测试通过。

**验收矩阵**：

| 场景 | 预期 |
| --- | --- |
| Full Access + 合法项目 | 当前会话自动接入 |
| 缺少本地依赖 | FC 隔离环境自动准备 |
| 真实测试失败 | Failure Run + bug-fix proposal |
| 控制层瞬时错误 | 分类、有限重试、不重复 Run |
| Apply/Verify | 保持确认和真实结果 |
| 隐私文件/外发 | 单独确认 |
| Run 重启/断线 | 查询恢复，不重放副作用 |
| Run 结束 | 临时工具清理并询问是否持久化 |

**Review**：

1. 固定候选 SHA 和实际测试命令/退出码；
2. 启动 Standards 与 Spec 两个 subagent review，提供设计文档、计划、diff、测试结果和排除项；
3. 批量修复真实发现，运行增量回归；
4. 复核通过后才写入完成证据和更新 `CURRENT_PLAN.md`；
5. review 未通过不能正式验收。

## 验证命令

开发过程中优先使用项目已有环境和脚本。至少覆盖：

```powershell
Set-Location E:\codex-local\nanobot-devpilot-agent\devpilot-agent
uv run pytest tests/test_agent_test_runs.py tests/test_fc_sandbox_contract.py -q
uv run pytest tests/test_p2a_code_access.py tests/test_p2a_patches.py -q
```

根据实际改动追加：

- `tests/test_control_plane.py` 或对应控制层测试；
- `tests/test_h6_model_plugins.py`、`tests/test_h11_candidate_autonomy.py`；
- nanobot 工具/Runtime 集成测试；
- WebUI 构建和相关前端检查；
- 真实 FC fixture 与隔离缺陷项目验收。

每个阶段都记录实际命令、退出码、候选 SHA、未运行项和失败原因；不能用缺失测试冒充通过。

## 交付顺序

1. 执行状态/错误模型；
2. 当前会话项目接入；
3. Profile 预检与隔离环境准备；
4. 执行协调和有界恢复；
5. Failure Run 与代码交付回归；
6. Agent/WebUI 可观测性；
7. 真实缺陷项目验收；
8. Standards/Spec subagent review；
9. 更新交接文档和当前计划。

## 完成定义

- 真实缺陷项目可以通过 Agent 自主完成“接入 → 准备 → 测试 → 修复 → 验证”；
- 环境失败、控制层失败和代码测试失败分类正确；
- 失败 Run 可被 bug-fix Skill 消费，成功后 Apply/Verify 结果真实可查；
- 无任意模型命令执行、无敏感数据外发、无跨会话临时工具泄漏；
- 现有回归测试和新增验收全部通过；
- 两个 review 轴无待修复项；
- 有完整交接文档，包含提交 SHA、命令、结果和已知限制。
