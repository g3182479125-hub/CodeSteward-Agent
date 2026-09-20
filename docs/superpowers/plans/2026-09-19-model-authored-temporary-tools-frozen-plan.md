# 模型自生成临时工具：冻结实施计划

## 使用方式

本计划依赖并实现 `docs/superpowers/specs/2026-09-19-model-authored-temporary-tools-design.md`。后续工作严格按阶段推进：先读取当前工作区、确认已有 H1–H11 能力是否可复用，再实施一个阶段，运行该阶段验收，并记录证据。不得因为临时工具目标而回退 Full Access 已确认的直通体验。

## 当前实施进度

- T1 已完成：`ToolCapabilityGap`、`TemporaryToolManifest`、生命周期状态转换、Full Access/WebSocket 入口判断和契约测试；提交 `2b8aaee`。
- T2 已完成第一批：不可变 artifact store、源码 AST 协议预检、哈希/路径/冲突校验，以及 `devpilot_create_temporary_tool` Entry Point；提交 `35ca016`、`f56c29f`。
- T3 已完成：FC 合约验证器会在沙箱内编译、导入最小 Tool shim、实例化、序列化 schema 并执行成功参数、无效参数、受控错误和 output schema fixture；验证回执绑定版本、sandbox/run/hash，FC 失败不得激活。
- T4 已完成：`PluginSource.TEMPORARY`、`register_temporary_version`、`activate_temporary` 接入现有 `PluginManager`/`DynamicPluginRegistry`；真实工具可在当前 Run 下一轮 schema 中热加载，并通过 managed lease 排空。
- T5 已完成：`devpilot_ensure_temporary_tool` 串起生成、FC 校验和热加载；Full Access/WebSocket 才可用；隐私能力声明没有可信确认信封时返回一次确认要求。
- T6 已完成：Run 完成时停止、排空、移除临时 route，快照进入待用户决定区；加粗询问后，明确同意可转 project 持久插件，拒绝可删除；进程重启只清理遗留临时 route/源，不恢复活动临时工具。
- T7 代码与证据已完成：真实 FC 成功和导入失败证据、同一 WebUI Run 内的候选生成 → FC 验证 → 热加载 → 真实调用、受影响回归、过期快照清理、生命周期落盘、能力缺口记录、FC 回执落盘、隐私读取分类、清理失败重试状态、精确绑定确认、清理状态推进和激活失败回滚均已完成；当前执行最终独立子 Agent Standards + Spec review 门禁。详见 `devpilot-agent/docs/evidence/live-harness/2026-09-19-temporary-tool-chain.md`。
- 正式验收：临时工具链测试 `28 passed, 1 skipped`（live FC 证据另有真实 provider 通过）；H6 `20 passed`；H5 `46 passed`；nanobot 相关回归 `263 passed`；WebUI 专项测试 `131 passed`；本轮新增 project 路由隔离、路径隐私分类、gap 凭据脱敏、版本化 FC 回执、真实 checker fixture、外发命令分类和 cleanup_pending 重试测试均已通过；Ruff 和 `git diff --check` 通过。最终双重 Review 已通过，T7 已正式验收。

### Review 记录

- 首轮独立 review（子 Agent `01a0b919-2d7e-7650-8b39-556f92fd06da`）未通过：隐私确认未绑定具体工具动作、生命周期未显式落盘、T7 尚未完成门禁登记。
- 已修复：敏感能力确认现在绑定 session、Run、project、tool、version、source hash 和精确 capability action，并要求网关签名且一次性消费；每个 artifact 新增 `lifecycle.json`，状态历史经过转移校验；Run 清理推进 `retiring`，激活失败会回滚 route；补齐 19 条临时工具回归。
- 增量 review 后已修复：`privacy_read`（环境变量、浏览器/密钥环等）继续触发一次确认，普通 workspace `sensitive_read` 不额外询问；FC contract checker 增加成功参数、无效参数、受控错误和 output schema fixture；验证回执写入候选 `validation.json`；新增 `devpilot_report_tool_gap` 与脱敏 gap 记录；清理失败标记 `cleanup_pending`、保留绑定并允许下一轮重试，且禁止展示持久化询问；Run 完成询问抽为可测试的加粗常量。
- 最后一轮增量 review 指出的 6 项均已修复：临时 route 的 session+project 过滤、`../` 与运行时路径的 privacy gate、gap 文本凭据脱敏、候选实际 checker 执行及受控错误 fixture、`validation_version` 回执字段，以及 cleanup_pending→retry 的直接回归测试。
- 最终增量 review 指出的 4 项也已修复：`subprocess`/`os.system` 等外发路径分类、FC stdout/stderr 回执脱敏、WebUI 活动流与 Markdown 加粗专项证据，以及最终 checker 版本的真实 FC sandbox 证据。
- 最终复审已通过：同一任务内 subagent `01a0b95f-92cb-7201-8891-039a17d60b92` 返回 Standards Review PASS、Spec Review PASS、Blocking issues: none；T7 正式验收。

## 最终 Review 门禁

T7 的代码、测试、真实 FC 证据和 WebUI 证据全部完成后，必须启动独立子 Agent 做两条互相独立的 Review：

1. Standards Review：核对 Full Access、隐私确认、session/Run 隔离、候选不落地、FC 前置验证、清理和回滚等既有标准是否被违反。
2. Spec Review：逐条核对本文和设计说明的 T1–T7 需求、验收标准、失败处理和明确非目标是否实现，区分“未覆盖”与“失败”。

主 Agent 必须向子 Agent 提供 baseline/candidate、diff、实际命令与退出码、FC Run ID、覆盖范围和已知限制。两个 Review 都通过、没有待修复项，并完成受影响回归后，才能把本计划标记为正式验收；任一 Review 发现有效问题，都必须修复并进行增量 Review，不能先验收后补记录。

## 冻结产品约束

- 仅 Full Access 启用模型创作、验证、热加载临时工具。
- 自动执行生成 → 隔离验证 → 当前 session 激活 → 继续原 Run；验证失败才打断并说明。
- 临时工具默认仅当前 session、当前 Run 有效；Run 结束自动卸载和清理。
- Run 结束后只用加粗文案询问是否持久化；未同意不得写为正式插件。
- 隐私/秘密读取、外发或回显仍需要一次明确确认。
- 复用单一 nanobot Agent Loop、现有 PluginManager、FC 和 WebUI；不创建第二套运行时。

## 阶段 T1：明确契约和持久化模型

**目标：** 将临时工具视为一等候选对象，而非无记录的内存函数。

**实现：**

1. 在 `devpilot-agent/src/devpilot/` 新增临时工具领域模型：`ToolCapabilityGap`、`TemporaryToolManifest`、生命周期状态和脱敏回执。
2. 复用候选运行器/数据库迁移方式，建立 session、Run、project、源码哈希和状态的持久记录；不保存秘密原文。
3. 定义清单 JSON Schema 和 Python Tool 协议：入口模块、类、名称、参数/输出 Schema、声明能力和版本。
4. 在 `model_plugins.py` 与 task profile 中明确该能力只在 Full Access Profile 可见。

**验收：** 缺口/清单状态转移单测；跨 session 查不到临时记录；Full Access 与默认权限的可见工具集合测试。

## 阶段 T2：候选工具包生成

**目标：** 让模型可创建受控格式的 Python 插件候选，而不是任意散落脚本。

**实现：**

1. 在 `nanobot_tools.py` 增加能力缺口报告及临时工具创建 Tool；生成目录固定为数据目录下的 `temporary-tools/<session>/<run>/<tool-id>`。
2. 提供源码模板：继承 `Tool`、声明 Schema、实现 `execute()`；模型仅填写模块主体和测试 fixture。
3. 生成后计算源码/清单哈希并冻结写入；后续步骤只读取此快照。
4. 重名、基础工具名冲突、路径穿越、清单字段不完整、模型直接要求加载本地任意文件等请求必须返回稳定错误码。

**验收：** 成功生成一个最小工具；名称/路径/哈希/清单错误的拒绝测试；生成目录不出现在基础 Entry Point 工具发现路径。

## 阶段 T3：FC 验证执行器

**目标：** 在激活前证明候选可导入且至少满足声明的契约。

**实现：**

1. 新增 `TemporaryToolValidator`，基于 `fc_sandbox.py` 和候选运行器创建固定验证 profile。
2. 固定检查：编译、通过最小 Tool shim 导入、`Tool` 子类/`execute()`、Schema 序列化、成功/无效参数/受控错误 fixture；真实主机导入仍延后到 managed loader 的 staged load。
3. 保存 FC Run ID、退出码、输出摘要、沙箱 ID、哈希和验证版本；FC 不可用时不允许激活。
4. 在验证结束至加载前二次校验候选快照哈希，避免 TOCTOU 篡改。

**验收：** 有效候选通过；语法错误、导入错误、契约错误、fixture 失败、FC 超时、清理失败分别产生可解释回执且无主机副作用。

## 阶段 T4：会话级热加载和调用排空

**目标：** 把验证通过的工具加到当前 session，而不污染基础注册表或其他会话。

**实现：**

1. 在 `PluginManager` 新增受控的 temporary-tool route；只接受 T3 验证通过且 session 绑定一致的清单。
2. 复用 `DynamicPluginRegistry` 做 session 覆盖；新工具在下一次模型工具 Schema 组装时可见。
3. 用 `ManagedPluginTool` 的 lease/排空语义防止卸载中断已执行调用；工具调用日志绑定 Run 和候选版本。
4. 禁止同名覆盖基础工具、跨 project/session 挂载，以及绕开验证直接 `register()`。

**验收：** 当前会话能调用新工具；并行会话不可见；重复激活/名称冲突/卸载中调用/激活后源码篡改均有测试。

## 阶段 T5：缺口自动编排与 Full Access 体验

**目标：** 模型不需要用户手工分步操作，能在当前 Run 自动补齐工具后继续任务。

**实现：**

1. 新增高层编排入口，将 `gap → create → validate → activate` 串成一个可恢复操作。
2. Skill 指令明确：先确认现有工具确实不适用，再使用该入口；验证成功后在下一轮主动调用新工具完成原目标。
3. Full Access 下默认自动继续；默认权限下清楚说明需要切换 Full Access，不伪造成功。
4. 敏感读取/外发命中隐私分类时，接入现有一次确认信封；确认只恢复该动作。

**验收：** 端到端固定任务：模型识别缺口、生成工具、通过 FC、热加载、调用成功、完成原任务；默认权限拒绝；隐私命中需一次确认。

## 阶段 T6：Run 结束回收与用户沉淀

**目标：** 临时工具不会泄漏到未来会话，且用户可以一键保留有价值工具。

**实现：**

1. 将 Run 完成、取消、不可恢复失败和会话关闭连接至生命周期服务。
2. 先停止展示、等待调用排空/取消，再卸载路由并删除临时源与 fixture；记录清理结果。
3. 在成功完成 Run 后发送固定加粗询问：**这个临时工具已完成本次任务，要保存为可复用插件吗？**
4. 用户确认后将经过验证的不可变快照复制到持久插件目录，重新验证、登记版本；拒绝/超时则清理。
5. 进程重启只恢复清理任务和脱敏回执，不恢复活动临时工具。

**验收：** 完成/取消/崩溃恢复都清理；确认保存后新会话可加载；拒绝后不可加载；UI 文案和加粗渲染测试。

## 阶段 T7：全链路证据和回归

**目标：** 证明新增能力不破坏已有 DevPilot、动态插件和 Full Access 行为。

**执行：**

1. Python：运行临时工具模型、生成、验证、生命周期、PluginManager、动态注册表、权限和隐私相关 pytest。
2. nanobot：运行 plugins、registry、loop、WebSocket 相关回归。
3. WebUI：运行活动流、确认消息和加粗渲染的前端测试。
4. FC：在专用合成 fixture 跑至少一例成功、一例验证失败和一例超时/清理失败。
5. 真实 WebUI：录制一次缺工具任务的自动链路；不得用 Fake 结果替代真实 FC 证据。

**退出条件：** 所有 T1–T6 验收通过；已知失败保留；测试/FC/网页证据写入 `devpilot-agent/docs/evidence/live-harness/`；按项目既有双轴增量 review 规则完成评审。

## 实施顺序和依赖

```text
T1 契约
  → T2 生成包
  → T3 FC 验证
  → T4 动态挂载
  → T5 自动编排
  → T6 回收与沉淀
  → T7 真实链路与交付复核
```

不得跳过 T3 直接实现 T4，也不得在 T6 前把候选自动保存为长期插件。

## 失败处理

| 阶段 | 处理 |
|---|---|
| 模型没有正确识别缺口 | 返回现有工具清单和原因，不生成插件 |
| 代码/协议验证失败 | 候选标记 rejected，保留脱敏证据，继续使用已有能力或向用户说明 |
| FC 不可用 | 不激活候选，不回退为未经验证的本机执行 |
| 动态加载失败 | 回滚 route/资源，工具不进入可见 Schema |
| 工具执行失败 | 返回真实失败，不能因工具刚生成而自动继续重写/重试 |
| 卸载失败 | 标记 cleanup pending，阻止同名再次激活，进程重启后接续清理 |
| 用户不保存 | 删除临时源/fixture，保留最小记录 |
