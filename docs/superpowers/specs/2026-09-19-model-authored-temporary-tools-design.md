# 模型自生成临时工具与会话热加载设计

## 状态

冻结，待按本设计分阶段实施。日期：2026-09-19。

## 目标

当模型判断当前会话可见的工具无法完成用户任务时，它可以在同一次 Run 内生成一个 Python 工具插件，在隔离环境中完成协议和行为验证，将其热加载到当前会话，并在后续模型回合调用该工具继续原任务。

用户选择 Full Access 时，临时工具继承该会话的本地和网络执行权限。对疑似密钥、个人文件、聊天记录、浏览器数据或敏感数据外发，仍只在实际读取、外发或回显前进行一次明确确认。

临时工具默认只属于当前会话：Run 结束即卸载，不写入持久插件目录。Run 结束后，WebUI 以加粗文本询问用户是否将该工具沉淀为正式可复用插件；仅在用户同意后持久化、登记并可供后续会话挂载。

## 非目标

- 不让模型在未验证时直接执行生成的插件代码。
- 不把临时工具默认暴露给其他 session 或项目。
- 不让临时工具默认跨进程、跨重启存在。
- 不创建第二个 Agent Loop、第二个 WebUI 或新的远程插件市场。
- 不将“生成成功”写成“任务成功”；工具必须返回真实的运行结果。

## 现有能力与复用边界

| 已有模块 | 本设计中的用途 |
|---|---|
| `nanobot/agent/tools/registry.py` 的 `ToolRegistry` | 基础工具定义、Schema 呈现及调用分发 |
| `nanobot/agent/plugins/manager.py` 的 `PluginManager` / `DynamicPluginRegistry` | 按 session/project 覆盖、路由、卸载和调用排空 |
| `devpilot/candidate_runner.py` | 候选运行记录、预算、FC 运行和结果持久化模式 |
| `devpilot/model_plugins.py` | 模型候选/管理工具及用户权限模式接入 |
| `devpilot/nanobot_tools.py` | 模型可见 Tool 对象、Full Access 语义及 WebUI 结果契约 |
| `fc_sandbox.py` | 隔离的语法、协议和行为验证后端 |

新实现只能补上“能力缺口识别、候选工具包、验证、动态挂载、Run 终止清理、沉淀确认”这条链，不重写上述基础设施。

## 核心对象和接口

### 1. 能力缺口记录 `ToolCapabilityGap`

保存到当前 Run：请求目标、已有工具清单、模型说明的缺口、建议工具名、权限模式、session ID、project ID 和状态。它是可观察记录，不是授予插件权限的凭据。

模型只有在现有工具不能完成任务时才可创建该记录；不得把“模型不知道该用哪个工具”伪装成能力缺口。

### 2. 临时工具清单 `TemporaryToolManifest`

每个候选目录包含一个声明文件和一个 Python 模块。清单至少包括：

```text
tool_id / version / session_id / project_id
name / description / parameter_schema / output_schema
entry_module / entry_class / source_hash
declared_capabilities / created_at / expires_at
```

实现类必须继承 nanobot `Tool`，并提供稳定 `name`、参数 Schema 和 `execute()`。加载器只接受该目录内、与清单哈希匹配的模块；不接受任意 import 路径。

### 3. `TemporaryToolLifecycleService`

新增会话级编排服务，负责状态转换：

```text
gap_detected
→ generated
→ validating
→ validated | rejected
→ active
→ retiring
→ disposed | persistence_requested | persisted
```

它是唯一能调用 `PluginManager` 挂载/卸载临时工具的入口。任何失败都必须携带阶段、错误摘要、候选 ID 和可重试性；不得留下半注册对象。

### 4. 四个模型可见工具

- `devpilot_report_tool_gap`：记录并解释缺口；不生成代码。
- `devpilot_create_temporary_tool`：根据受控模板生成候选工具包。
- `devpilot_validate_and_activate_temporary_tool`：提交 FC 验证，通过后挂载至当前 session。
- `devpilot_persist_temporary_tool`：只接受 Run 完成后的用户明确同意，将候选复制到持久插件目录并登记版本。

第一版可以由高层编排工具把“生成 → 验证 → 激活”串成一次调用，减少模型步骤；但内部仍保留四个独立阶段，以便诊断、测试和未来 UI 展示。

## 运行流程

```text
用户任务
  → 现有工具集合和任务 Profile 被提供给模型
  → 模型判定能力缺口
  → 记录 ToolCapabilityGap
  → 生成临时工具包（只写会话临时目录）
  → FC：编译/导入/Schema/最小行为验证
  → 验证失败：保留失败证据，模型改用已有工具或如实说明
  → 验证成功：PluginManager 挂载至当前 session
  → DynamicPluginRegistry 在下一轮模型决策中呈现新工具
  → 模型调用新工具，继续同一 Run
  → Run 完成、取消或不可恢复失败
  → 停止新调用、排空在途调用、卸载并删除临时目录
  → **这个临时工具已完成本次任务，要保存为可复用插件吗？**
```

已经开始执行的旧调用不可中途替换；新工具对下一轮模型工具选择生效。工具只向创建它的 session 暴露，除非用户完成沉淀并在后续会话显式/按配置加载。

## Full Access 与隐私边界

1. 未启用 Full Access：本期不自动创建或激活模型生成工具，返回“该能力需 Full Access”及原因。
2. 已启用 Full Access：临时工具可使用当前会话可用的文件、命令和网络执行能力，不能绕过项目现有的取消、审计和结果回执。
3. 隐私检测命中时：在读取、上传、发送或输出敏感内容前阻塞该单次动作，要求用户确认；确认后只恢复该动作。
4. 临时工具的源码、清单、验证日志和调用记录必须标注 session、Run、工具版本和哈希，且日志不得回显秘密原文。

## 验证与加载

候选必须先在 FC 沙箱通过以下四类检查：

1. Python 语法/导入检查：禁止无法导入、清单入口不匹配和依赖缺失。
2. Tool 契约检查：继承 `Tool`，名称唯一，参数和输出 Schema 可序列化，`execute()` 可被调用。
3. 最小行为检查：使用生成时同时产生的 fixture 或模拟下游服务，覆盖成功、参数无效和受控异常各一例。
4. 边界检查：候选目录不越界、哈希未被验证后篡改、工具名不覆盖基础工具或同 session 已激活工具。

FC 返回通过后，主进程再次校验清单、哈希和 session 绑定才允许动态导入。导入失败、注册冲突或激活失败时，必须执行清理并返回明确状态；不能静默回退为本机 `exec`。

## 卸载、沉淀和恢复

- Run 完成/取消/失败：停止向模型呈现临时工具，等待已有调用结束或超时取消，再调用 PluginManager 的清理路径。
- 进程重启：临时目录按 session/Run 扫描并清理，不恢复为活动插件；持久化确认尚未完成则丢弃候选，仅保留脱敏回执。
- 用户选择“保存”：复制经过验证的源文件和清单至受信持久目录，生成不可变版本，重新验证，然后登记为可选择的正式插件。
- 用户拒绝或未响应：删除临时源代码及测试 fixture，保留最小脱敏元数据用于诊断。

## 可观察性和 WebUI

WebUI 不新增复杂管理页，只在现有活动流显示：能力缺口、候选生成、验证状态、激活状态、临时工具调用和 Run 结束后的沉淀询问。不得把内部堆栈、完整生成源码或敏感日志默认展示给用户。

`persistence_requested` 状态的消息必须使用加粗文案：

> **这个临时工具已完成本次任务，要保存为可复用插件吗？**

## 验收标准

1. Full Access 会话中，固定缺工具任务可自动生成、验证、激活工具，并由该工具完成同一 Run 的后续步骤。
2. 临时工具不出现在其他 session/project 的工具 Schema 中。
3. Run 结束后工具已卸载，临时源码被清理；UI 显示加粗沉淀询问。
4. 用户确认保存后，新会话可加载持久版本；拒绝保存后不能恢复该工具。
5. 无效 Python、错误 Schema、激活冲突、FC 失败和主进程导入失败均可观测、可清理，且不会污染基础注册表。
6. 敏感数据动作仍触发一次确认；普通 Full Access 工具生成/验证/调用不产生额外确认。

