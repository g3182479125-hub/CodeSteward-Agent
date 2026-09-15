# DevFlow：运行中可重组 Harness 扩展计划

版本：0.4，2026-09-15；状态：H1–H9 已完成节点验收；H9 已完成最终双轴增量复核；H10-A/H10-B/H10-C 已完成，H10-D 等待本轮最终双轴增量复核。
当前实现：H1 已在固定候选 `2930119` 完成并通过 Standards / Spec 双轴审查；H2 已在固定候选
`b8141f0` 完成并通过增量 Standards / Spec 双轴复核；H3 已在固定候选 `6159add` 完成并通过
增量 Standards / Spec 双轴复核；H4 已在固定候选 `78456c6` 完成并通过增量 Standards / Spec 双轴复核；H5 已在 `e08299e` 完成，并由同一对 reviewer 对 `9d216c7...e08299e` 做增量 Standards / Spec 双轴复核且均为 `PASS`；H6 代码、自动化测试、真实 DeepSeek + nanobot WebUI + 杭州 FC 场景验收及最终增量复核均已完成；H7 已在 `0cdb1eb` 完成 Host/Driver 边界、托管检查点和双轴增量复核；H8 已完成真实验收；H9 已完成固定 Host oracle、插件/Loop 选择账本、安全回执和最终双轴复核。H10-A 已通过真实 DeepSeek + nanobot WebUI + 既有 FC 配置完成全链路证据，H10-B/H10-C 已完成；本轮本地 fake 仍单独标注，不计入 H10-A。本文件仍是 H 阶段的
范围和验收约束，不能把计划内容当作已具备能力。

## 1. 目标、范围及与旧计划的关系

让个人 Agent 在服务持续运行、任务信息保留的情况下，发现、定义、试用、更新和卸载插件；最终能在步骤边界替换 Agent Loop，并根据实际任务结果选择更合适的实现。
保留 nanobot 为主体、Python 开发、原生 WebUI、DeepSeek 模型、现有 DevPilot 代码交付和杭州 FC provider-default。

本次用户最初授权的是拟定扩展计划和一次子代理审查、一轮修订；随后已明确授权按计划实施 H1。采用 H1–H10 编号，避免把已完成 P0–P8 改成未完成，也不把新要求混进历史验收。
用户后续要求按本计划开发时，本文件是 H 阶段范围来源；CURRENT_PLAN 3.6 继续约束历史能力及开发审查。
本文件在 H 范围内明确取代 HARNESS_ADOPTION 中“只能启动时生效”“模型不能安装插件”“不改 Loop”“不建生命周期/服务容器”的排除项。
这里允许的是受管理的动态版本加载、局部依赖管理和 Loop 接口；不是允许模型执行任意 pip 命令、改权限内核或重写整个仓库。
P8 既有 Skill 启用规则继续有效；本文没有通过改名绕过 P8 确认。

三个候选路线：

| 路线 | 优点 | 代价/决定 |
|---|---|---|
| 直接换为 DeepSeek/Cordis Runtime | 直接得到其插件语义 | 需要迁移现有 Python Loop、WebUI 和集成，偏离保留 nanobot；不选 |
| nanobot + Python 小型生命周期层 | 保留已有系统，逐节点验收，接口易理解 | 需自己实现清理、依赖和接续；本计划选择 |
| nanobot 外挂完整 Cordis 第二主 Runtime | 可运行 TS 插件 | 双重会话和调度归属复杂；不选 |

选择第二条不等于复刻 Cordis 全部框架。核心交付是可逆注册、依赖响应、版本切换、模型操作入口、Loop 接续和反馈选择。受限插件执行进程属于执行器，不拥有第二份 Agent Loop、会话或调度器。

## 2. 参考证据与需要诚实说明的边界

- 论文：A Programming Paradigm for Spatiotemporal Composability，arXiv:2608.25512v1；本机 E:/download/2608.25512v1.pdf。重点 §1.2.2、§5.1、§5.2、§6.1、§6.2、§6.4。
- DeepSeek 本地快照：E:/download/deepseek-harness-master (1).zip，根 deepseek-harness-master/；开始借用代码时记录 SHA256 和许可证，参考设计不冒充直接移植。
- 快照中 packages/extensions/tool-cordis/README.md、src/index.ts：inspect/define/run/update/stop/undefine 的模型入口。
- vendor/hmr/src/index.ts：缓存更新、失败恢复、部分底层变化要求完整重启。
- packages/core/agent-loop/src/index.ts：Loop 作为服务，卸载有取消和清理；持久会话支持恢复，并非任意调用栈迁移。
- 官方说明：https://deepseek-harness.github.io/deepseek-harness/en/develop/practice/dynamic-cordis
- 官方 HMR：https://deepseek-harness.github.io/deepseek-harness/en/develop/cordis-tutorial/06-composition-and-hmr

核心映射：

| Cordis 思想 | 本计划对应 | 可证明的结果 |
|---|---|---|
| 可逆 effects | H1/H3 PluginContext 管理注册和资源 | 卸载无遗留注册/监听/任务 |
| reactive coeffects | H4 明确 provides/requires | 缺依赖等待，恢复后重新激活 |
| HMR/配置协调 | H2/H3 不可变版本和切换操作 | PID 不变，新增/更新/卸载可用 |
| 模型操作自身插件 | H5/H6 inspect/define/trial/activate/stop | 模型在当前会话试出新工具 |
| 可替换 Loop | H7/H8 稳定 Host 与 Driver | 步骤边界迁移，任务 ID 和副作用记录连续 |
| 实战选择架构 | H9 固定对照和晋升规则 | 只报告测试范围内收益，无收益不晋升 |

服务不重启不代表零延迟；卸载允许等待正在执行的调用结束。持久历史不等于全部内存状态。不能迁移线程、协程栈、打开的 SDK 对象或正在生成的模型 KV cache。
卸载撤销注册/资源归属，不会抹去已发出的网络请求、业务写入或付费调用。结果不确定沿用未知状态，不自动重放。

## 3. 保留功能与不可被插件接管的稳定部分

下列行为必须保留，并在变更影响到它时回归：

1. 原生聊天、中文输出、流式进度、取消、历史查询与网页重连。
2. 项目分析、feature-delivery、bug-fix 三个 Profile；任务范围隔离。
3. 提案/Diff 折叠、聊天确认、Apply+Verify、撤销单独确认、幂等和文件冲突检测。
4. FC 上传清单、排除凭据、固定验证契约、退出码/超时/取消/清理/恢复；继续 provider-default，不重启网络隔离项目。
5. P8 反馈、候选、固定评测、确认启用/恢复、原生 Cron/Dream 限制。
6. 已有开发者 entry point 插件继续可启动；未声明新契约的旧插件标 legacy/restart-required，不能谎称热卸载兼容。

稳定 Host 持有：用户身份、权限校验、确认消费、会话存储、操作日志、版本路由、预算、取消和接续协调。
插件可提供：Tool、Skill 内容、Prompt 片段、有限服务、Loop Driver/策略。权限执行点、凭据存储、插件管理器本身不交给模型在线替换。
这是一份有明确稳定内核的 Python 借鉴方案，不宣称“一切皆可替换”或与 Cordis 完全等价。

## 4. 让弱模型按步骤实现的固定规则

每次只实施一个 H 节点。读取 AGENTS、工程 Skill、CURRENT_PLAN §0/7、ADR-0004、本文完整契约及当前节点；涉及 P8 时完整读取 SKILL_IMPROVEMENT。
记录节点、HEAD、未提交改动、输入接口、计划文件、验收编号。新增路径下文标“拟新增”；先搜索相同职责，能扩展现有实现就不建第二份。
不得仅根据文件名推断能力，也不得整文件重写 agent/loop.py。每步先做一个可测行为，再加下一步。
每个节点交付：实际接口映射、代码和适用测试、使用例子、失败恢复、状态记录。没有验收证据不推进下一节点。
计划中的数据结构是语义契约：H1 固定实现名称和旧→新字段映射，后续沿用；若确需变更，一次记录所有调用者和兼容方式，不让下一模型猜。

### 现有入口

| 职责 | 实际路径（仓库根相对） |
|---|---|
| 工具发现/注册/调用 | nanobot/nanobot/agent/tools/{loader,registry,base,context}.py |
| Loop 与请求状态 | nanobot/nanobot/agent/{loop,model_runtime,context,context_governance}.py |
| 实际迭代/工具派发及委派调用者 | nanobot/nanobot/agent/runner.py 的 AgentRunner/AgentRunSpec；agent/subagent.py |
| 工具视图/任务配置 | nanobot/nanobot/agent/task_profiles.py；registry.restricted_to 当前复制对象引用 |
| 会话及继续 | nanobot/nanobot/session/{manager,turn_continuation,goal_state}.py |
| 原生命令/聊天 | nanobot/nanobot/command/builtin.py；channels/websocket/runtime.py |
| 项目写入/验证 | devpilot-agent/src/devpilot/{action_broker,services,execution,fc_sandbox}.py |
| Skill 改进 | devpilot-agent/src/devpilot/skill_improvement.py；nanobot/nanobot/agent/skills.py |

拟新增的集中模块放 nanobot/nanobot/agent/plugins/：contracts.py、manager.py、context.py、routing.py、store.py、runner.py。按节点需要创建，不提前建立空框架。
Loop Driver 接口拟放 agent/loop_driver.py，默认适配器留在现有 Loop 附近。插件工具拟放 agent/tools/plugins.py，命令处理提取到 command/plugin_commands.py，避免继续膨胀 builtin.py。
测试优先复用 nanobot/tests/agent、tests/command；最终证据统一放 devpilot-agent/docs/evidence/live-harness/。

## 5. 统一契约：先规定如何切换，再写代码

### 5.1 Manifest、版本和权限

PluginManifest 最少包含 plugin_id、version、content_hash、kind、api_version、scope、entry、requires、provides、requested_capabilities、execution_mode。
kind 只支持 tool/service/prompt/skill/loop；scope 为 session/project；global 仅开发者明确安装，模型候选不得全局注册。
plugin_id 稳定，version 内容不可变；替换代码就是新版本。工件位于独立版本目录，禁止覆盖正在运行的模块文件。纯 Python 首版禁止动态安装第三方依赖，依赖只能用已固定的执行环境。
权限由 Host 计算：用户为任务授予的权限 ∩ Profile 范围 ∩ 插件已批准声明；requested_capabilities 只是申请。不能按名称冒充既有受信工具。
受信 Profile 新增显式 dynamic_plugins 配置：允许的插件 kind、权限类别、管理动作、作用域和预算上限；默认关闭。用户授权不能突破该配置，模型不能修改它。
任务工具视图 = 原有名称白名单视图 + Host 在该任务授权下登记的动态插件视图；后者逐个按版本/能力核验，不用 demo_* 等名称通配。两部分的执行仍经过同一权限入口。
H2 开放受信命令装卸；H6 才按所需最少管理动作开放模型工具。关闭 dynamic_plugins 时原 Profile 行为完全一致。
授权区分两类：
- TrialGrant：用户/会话/任务/项目、允许的数据类别、kind/能力、累计调用/token/时限、过期时间和撤销代次。它允许未来合规候选在本任务试用，不预签某个未知版本；每次运行记录实际版本并在 Host 原子预留预算。过期/撤销拒绝新派发，已开始调用按取消契约处理。
- PromotionConfirmation：一次性确认，绑定用户/会话/项目及精确作用域 ID、预期旧版本/路由代次、候选内容哈希、固定评测配置/结果哈希、权限、有效期和 operation_id。版本/基线/权限变化即失效。消费与路由意图在同一数据库事务提交；重复 operation 查询原结果，新 operation 不能复用确认。
P8 的 Skill 确认继续绑定其固定评测。会话试用不改变跨任务的稳定版本。

### 5.2 生命周期与调用租约

部署状态：inactive → preparing → active → draining → inactive；failed 保存失败阶段和清理情况。缺依赖用 inactive + reason=missing_dependency，避免再造复杂状态树。
候选评测状态单独记录 defined/tested/rejected，不能把 active 当作测试通过。
PluginContext 提供 register_tool、subscribe、spawn_task、provide_service 和 on_dispose；每项取得一个幂等清理函数，按相反顺序释放。应用代码不得自行清掉其他插件的注册。
preparing 阶段只收集惰性资源描述；监听不收事件、任务不启动、服务不对消费者可见。健康检查只使用固定无业务副作用输入。提交后才统一打开该代次入口；插件初始化不得绕过 Context 自行启动后台任务。
依赖组合使用同一作用域锁提交一张完整路由表，受影响资源在同一事件循环提交段打开，不能在半张新表上派发。打开失败立即关闭本代次入口并清理惰性/已启资源；已发出外部效果不得声称被回滚。
每个实际调用持有 (plugin_id, version, scope, generation) 租约；管理器知道哪些旧实例仍有调用。Host 资源句柄不能进入模型。

切换流程：校验候选 → 准备新实例但不对外注册 → 健康检查 → 在作用域锁内切换路由代次 → 旧实例停止接新调用 → 等旧租约归还 → 释放旧实例。
准备失败保留旧实例；提交后的新版本失败可把新调用路由退回旧版本，但已经发生的业务动作只查询结果，不能重放。
正常卸载默认等待当前调用（默认 30 秒）；超时返回 draining 和 operation_id，不虚报卸载成功。明确强制停止才调用协作取消；无法取消的受信本地插件标 pending_cleanup，不强杀整台服务。
耗时排空由后台 operation 执行，命令立即返回 operation_id；不得占住全局消息消费协程等待 30 秒。
停用只撤新调用；移除登记使插件不能再被选用并使待确认失效；物理回收是第三步。工件无运行租约且无稳定路由、暂停 checkpoint、依赖组合、待确认、回退保留引用时才可回收。
存在引用返回 retained 和引用原因；待确认需先显式失效并释放引用，暂停任务引用不能偷偷删除。文件删除失败显示待清理，不影响路由撤销结果。

### 5.3 模型请求与旧工具视图

每一步开始固定工具 schema 和版本路由快照。流式模型请求发送后不更改其 schema。
更新后下一步取新代次；旧响应内尚未派发的调用若原版本已撤销，返回 TOOL_VERSION_RETIRED，下一步重新规划；不能悄悄用新实现执行旧 schema。
已派发的调用继续使用旧租约；停用优先级高于下一次派发。每次调用仍检查当前权限。
必须改造 restricted_to 的旧引用路径，让不同会话既隔离又能感知本作用域代次；仅清 loader 缓存不算完成。
实际接入 AgentRunner/AgentRunSpec 的迭代和派发：把 request_id→工具版本快照传给每个模型返回调用；任何 await hook 结束后，在执行前重新进入作用域锁，检查权限/撤销代次并取得租约，然后离开锁调用。不得在 hook 前查完权限后直接 tool.execute。
租约在 finally 中释放，覆盖成功、异常和取消。subagent.py 及直接调用 prepare_call/execute 的入口逐个登记，统一经过同一派发适配；不能只改 registry.execute。切换边界放在 runner 的实际步骤结算点，而非仅在下一次用户 turn。

### 5.4 持久化与重启

复用现有 SQLite/Alembic 存储插件版本、作用域路由和 operation 结果；工件存在 E 盘私有数据目录，数据库不存凭据或完整聊天。
H1 确认 nanobot 到此存储的现有调用方式；不再创建新数据库。事务不能同时覆盖文件系统，使用先写工件+哈希校验、再提交路由；孤立工件可后续清理。
操作 ID 幂等；记录预期旧版本及代次，比较并交换，冲突拒绝。启动时核对工件，恢复已确认的稳定版本；临时候选默认不自动运行。未知活动任务按已有恢复语义查询。

### 5.5 Loop 的跨存储接续协议

会话 JSONL 继续保存对话，现有业务表保存 Apply/FC 事实；新增的 SQLite handoff operation 是 Driver 所有权、路由、checkpoint 内容和剩余预算的权威记录。不要依赖两个存储同时原子提交。
步骤结算顺序固定：先确认副作用结果已持久化→保存并 fsync 会话至明确消息位置/哈希→将 checkpoint（包含该位置/哈希）、Driver 版本和预算写入 SQLite→在同一事务内消费切换确认并递增 owner_generation→发布内存路由→向会话追加操作展示引用。
执行动作时检查当前 owner_generation，旧 Driver 即使被唤醒也无法再派发。Host 唯一协调该任务，Driver 不写存储。消息展示引用丢失可以按 operation_id 补，不能补执行动作。
重启先核对 SQLite 已提交 operation 和其引用的 JSONL 前缀、工件哈希；匹配才恢复。数据库提交前沿用旧 owner；已提交但展示未写则补展示；工具结果已保存而结算未完成则查已有结果、推进 checkpoint，禁止重放。
现有 Loop 的“将未完成工具标中断并清 checkpoint”逻辑不得直接处理 H 阶段已托管的 handoff；H7 明确分支并保留旧任务恢复兼容。前缀不匹配、缺版本或未知动作均暂停，不根据摘要猜下一步。
Checkpoint 补充 driver_state、driver_state_schema，JSON 编码上限 64 KiB；包括结构化计划和当前位置，不含句柄/协程/凭据。Driver 提供 export_state/restore_state，跨类型不兼容则基于公共完成事实重新规划尚未完成部分，或拒绝切换。预算及完成事实只信 Host，不信旧策略快照。

### 5.6 模型插件每一次执行的输入与预算

模型生成的 Tool 和 Driver，无论 trial 还是 activate 后的真实调用，始终由受信 FC 代理执行；验收通过不会把代码升级成可宿主 import 的受信代码。
试验输入仅固定 fixture；真实 Tool 输入仅 schema 定义且 TrialGrant/持久授权允许的数据；远端 Driver 只接收目标的获准摘要、结构化计划、已脱敏工具 schema、允许的结果摘要和 Host 分配的步骤标识，不能传完整 StepInput/私有会话/确认 token。
无法提供足够且获准的投影时返回 DATA_SCOPE_DENIED，继续已有受信 Driver 或暂停，不默默上传更多数据。schema 校验和字段白名单在上传前执行，不能让候选自行决定。
同一任务的模型修订、trial、激活后调用、Driver 决策和评测分别记明细但共享总预算；调用前原子预留、完成后结算，未知调用保留预留且计入上限。H5 的 3 次/120 秒是试验子预算，不替代正常调用总预算。
每次请求保存 operation_id、版本、输入哈希和 sandbox/run ID；结果缺失标未知，只查询或暂停，不能自动重复调用以得到结果。取消关闭代理新派发、取消实际云调用并清理，清理失败独立可见。逐调用创建沙箱的延迟必须计入 H10 实测，首版不掩盖成本而引入无界常驻池。

## 6. 执行节点

### H1：固定插件契约和兼容基线

前置：用户开始实施本计划。目标：建立可测接口，不改变默认任务行为。
落点：contracts/context；现有 loader/registry；现有存储迁移入口。

1. 定位 ToolLoader 调用时机、所有 restricted_to/prepare_call 路径和会话归属；画一张简短调用表。
2. 定义第 5 节数据类型、PluginContext、管理器返回结构和错误码；同一测试验证两个独立工具实现，不凭想象建接口。
3. 包装一个现有 greeting 示例为 manifest 插件；普通 entry point 继续走旧路径。
4. 清单记录核心工具和受信权限入口，不让动态名称覆盖它们。
5. 提供 list/status，只读列出来源、版本、作用域和是否支持在线生命周期。

验收 H1-A：原三个任务 Profile 工具集合和已有插件加载保持一致。H1-B：同名覆盖、无效 manifest、越界 entry 拒绝。H1-C：两会话列举各自插件无串用。
失败恢复：关闭新路径即回到现有启动装配，原配置和历史不迁移/清空。

### H2：先实现无需重启的工具加载与停用

前置 H1。目标：用户在原生聊天控制一个受信本地工具；此节点不执行模型生成代码。
落点：manager/routing、registry、请求 schema 装配、原生命令。

1. 增加 /plugins list、load <已登记版本>、stop <id>、status <operation_id>。只接受登记 ID，不接受任意 import 路径。
2. 只支持已审的纯函数工具，加载到目标会话；管理器分配新 generation。
3. 下一步骤重新生成本作用域 schema；所有执行入口校验作用域和版本。
4. 停用先撤路由，再等已开始调用完成。旧聊天的历史文字不能成为“工具仍存在”的证据。
5. 管理命令可在同一长任务期间到达；不必取消长任务或重建会话才能操作。

验收 H2-A：PID 和 session_id 不变，load 后下一步能调用，stop 后下一步不可调用。H2-B：模型流式等待期间停用，旧响应不调用被撤销版本。H2-C：另一个 60 秒任务正常完成。H2-D：历史工具回复不能让调用绕过 registry。
补测 H2-E：await hook 期间停用，在最后派发点拒绝；异常/取消租约归零。H2-F：受控 Profile 仅在显式能力开关和授权下获得动态工具，内置写入旁路仍被拒绝；委派入口不例外。
失败恢复：注册失败撤销本次注册，原生聊天继续可用。

### H3：版本更新与完整资源清理

前置 H2。目标：工具更新失败可恢复，带资源插件也能被干净移除。
落点：context/manager/store；版本工件解析；新增 update/remove 命令。

1. 使用独立版本模块命名空间，不用 importlib.reload 原地修改共享模块；清理只处理管理器拥有的版本对象。
2. 按 5.2 实施准备/健康检查/路由提交/旧实例排空；在持久记录里区分路由成功和清理完成。
3. 扩展受信示例：注册一个事件监听和一个定时任务，通过 PluginContext 获得并释放。
4. 更新故意语法错误的 v2 应保留 v1；提交失败撤销 staging 的所有资源。
5. 操作重复、同时 update/stop 使用 operation_id 和期望代次协调；清理异常可重试，不影响其它插件。

验收 H3-A：v1 旧调用完成，v2 新调用返回新结果，参数版本不串用。H3-B：重复装卸 50 次，注册/监听/任务计数回到基线，记录内存趋势而非保证完全相同 RSS。H3-C：语法/初始化/清理失败各有独立结果。H3-D：两个并发管理操作只提交一次。H3-E：强制中断重启后稳定版本和操作状态可核对。
补测 H3-F：preparing 中事件/计时器不能产生效果；排空不阻塞其它消息。H3-G：暂停任务引用、稳定版本停用后重启、待确认版本移除均不产生缺失工件或陈旧激活。
失败恢复：恢复上一稳定路由；无法完成的旧清理继续可见，不能写完整成功。

### H4：有限依赖和能力组合

前置 H3。目标：替换服务后消费方正确跟随，不依赖加载顺序。
落点：manager/context；service binding；作用域视图。

1. requires/provides 使用带版本的明确接口 ID，首版只支持精确 api_version，不做复杂版本求解。
2. 对请求的插件子图做循环检测；缺依赖等待，有依赖才激活。
3. 服务撤销前让消费方停止接新调用并排空；服务恢复后按依赖顺序重新挂载。默认只重建受影响子图。
4. 一个作用域内同一接口绑定一个提供者；不引入负载均衡、跨进程服务网格。
5. 服务更新期间将消费方路由一起切换，拒绝混合新旧依赖；已开始的调用持有整组版本租约。

验收 H4-A：先装消费者后装提供者可激活。H4-B：撤换提供者后正确等待/恢复。H4-C：A↔B 循环明确拒绝。H4-D：另一个项目同名接口不受影响。H4-E：不兼容接口不进入路由。
失败恢复：保留原组合；依赖不满足的组件显示原因，不无限自动重试。

### H5：模型候选的执行边界

前置 H4。目标：让模型代码可以试运行，而不把任意生成的 Python 直接 import 到宿主。
落点：runner、manifest 校验、现有 FC 请求/预算/结果适配。

1. 分清受信本地插件（H2–H4）和模型生成插件；后者在 FC 受限运行，通过结构化输入/结果返回，Host 只挂载已验证的代理 Tool。
2. 新增独立的插件试运行契约和受信执行模板；不能借用项目 Verify 的授权运行任意候选代码。输入仅含批准的测试数据和源码，不含本机密钥/私有会话。
3. 首版生成代码只做 JSON 输入→JSON 输出的纯计算，固定 Python 环境，默认 30 秒、输出 64 KiB、一个试运行并发、无动态 pip；保留 FC provider-default 标签。
4. 禁止把 AST 黑名单或 subprocess 本身称为安全沙箱。FC 可联网的事实保留；敏感信息不进入候选输入。
5. 候选请求 Host 能力时由受信代理代办，首版仅纯计算；项目读取/写入仍走现有工具，不能拿宿主对象引用。
6. 试运行预算按任务累计（初值最多 3 次、共 120 秒），超限停止；将测试实例清理结果落盘。

验收 H5-A：正常计算返回。H5-B：超时/输出超限/异常/FC 不可用返回明确结果，无宿主执行 fallback。H5-C：上传清单没有凭据/数据库。H5-D：候选不能注册宿主权限或操作其它会话。H5-E：至少一例真实 FC 试运行及清理。
补测 H5-F：激活后的真实调用仍走 FC；超范围参数上传前拒绝，调用/修订并发共享预算，不确定结果不自动重试。
失败恢复：候选留存为失败，现有 Tool 和项目交付不受影响。

### H6：模型创建、试用、更新和卸载工具

前置 H5。目标：实现与 cordis 工具工作流对应的产品能力。
落点：agent/tools/plugins.py、manager/store、原生聊天输出。

1. 提供最少六个模型动作：inspect、define、trial、activate、stop、remove。update 是 define 新版本+activate，不再造平行接口。
2. inspect 返回实际可用接口/schema/限制；define 保存不可变代码和 manifest，不执行。trial 使用 H5；activate 必须核对同一候选哈希和当前执行契约的成功试运行记录、有效授权和预算，才挂受信代理并在下一步出现在当前会话 schema。改代码/契约后旧 trial 不再适用。
3. 用户可在任务开始授权“本会话纯计算插件试用”，明确范围/总预算/生命周期；范围内模型无需每次再次请求。未授权则仅生成候选，聊天中一次确认开启试用。
4. 模型可以停用自己的会话候选；不得卸载核心或别的会话插件。会话结束先排空再清理；保留可供用户选择的脱敏版本记录。
5. 工具说明由 manifest 生成并标来源，执行权限由 Host 决定；生成的 read_only 声明不作为可信依据。
6. 把结果反馈给模型：输入错误/契约错误/执行错误/预算错误分开；最多两次修订，基础设施错误不触发无止境重写。

当前 H6 实现落点：`devpilot-agent/src/devpilot/model_plugins.py` 提供一个 nanobot
管理 Tool 和动态 FC-proxy Tool；`nanobot/nanobot/agent/plugins/manager.py` 复用 H3
注册、租约、清理和原子更新。候选工件按 session/plugin/version/content hash 分目录
不可变保存，宿主只创建代理，不 import `candidate.py`。首次 `activate` 走候选专用加载；
已激活版本切换到新版本走 `update_candidate` 与原子更新路径，旧路由撤销新派发后再清理，
因此同一 AgentLoop 可在下一步得到新 schema。候选状态与 trial 的版本/源码/输入哈希绑定
并写入插件运行状态；重建 manager 后可按已持久路由恢复，但授权仍需当前会话重新明确授予。
当前离线证据覆盖未授权拒绝、跨会话隔离、工件变更拒绝、v1→v2 无重启切换和持久恢复；
真实 DeepSeek + 原生 WebUI + 杭州 FC 的 H6-A 已完成现场验收，证据见
`devpilot-agent/docs/evidence/live-harness/h6-model-candidates.md`；该验收采用
`provider-default`，不等同于严格网络隔离，不能用 Fake runner 冒领。

验收 H6-A：真实网页同一长任务“缺工具→define→trial→activate→调用→修正版本→再次调用→stop/remove”，PID 不变。H6-B：未授权只生成不激活。H6-C：跨会话更新拒绝。H6-D：中途更新保留任务目标和已完成结果。H6-E：整个演示无需人工编辑注册表或重启。
补测 H6-F：授权撤销/过期后拒绝新派发，未试运行/内容变化的版本不能 activate；状态成功不代替绑定校验。
失败恢复：上一版本继续供新调用，或标工具不可用让模型用现有能力继续；不能伪造工具结果。

### H7：把 Loop 的可变决策从状态所有权中分离

前置 H6。目标：为真实替换 Loop 准备接口，先保证默认 Loop 行为不变。
落点：loop.py、runner.py 的 AgentRunner/AgentRunSpec、subagent.py、拟 loop_driver.py、session/turn_continuation、现有事件与取消。

1. 先逐项列出现有 Loop 的状态：消息/待执行工具/当前步骤/预算/取消/确认/运行 ID；明确哪些持久化、哪些仅内存，不能用摘要顶替运行事实。
2. Host 拥有调用模型、执行工具、写会话和权限验证；Driver 接收 StepInput 并返回明确下一动作：request_model、dispatch_tools、finish、pause。Driver 不直接执行项目写入。
3. 默认 Driver 从现有循环中小步提取，不更改并行/流式/工具错误行为；在 Host 调度入口统一消费动作。
4. Checkpoint 只含可序列化数据：session/task/project ID、步骤序号、消息位置、待处理输入引用、已提交工具结果、版本集合、剩余预算、业务 Run/ChangeSet 引用、checkpoint_schema。
5. 步骤边界必须满足：没有未结算模型响应和工具动作，已提交 checkpoint。FC 长运行可保留持久 run_id 供接续查询，但不可重放创建/Apply。
6. 活动步骤期间只标 switch_pending，允许用户继续看流式输出并取消；不会为切换强制重启。

验收 H7-A：默认 Driver 跑过既有三 Profile 主链路。H7-B：pending 工具/取消/并发消息均被 checkpoint 明确处理。H7-C：不确定副作用阻止切换。H7-D：旧记录无需破坏性迁移即可读取。
失败恢复：特性开关关闭时回到原 Loop；新 checkpoint 可忽略但不删除会话。

### H8：真实 Loop 替换和长任务接续

前置 H7。目标：提供两个 Driver 实现并在同一个任务中切换，不能仅改配置字符串。
落点：Driver registry、manager、checkpoint、原生 /plugins switch-loop。

1. 实现 default-react 和一个有限 plan-then-act Driver；两者使用同一 Host 接口，后者的计划为结构化步骤，不增加另一套执行器。
2. 新 Driver 先验证 checkpoint_schema 和 dry-run 恢复；在边界原子转移唯一任务所有权，旧 Driver 不再调度。
3. Driver 版本也使用不可变工件；模型生成的策略在 H5 执行器中返回动作，Host 验证动作合法性/预算/权限后执行。远端不可用时不运行未经验证的本机代码。
4. 执行中申请切换会返回 operation_id 和等待原因；达到边界后自动接续，不要求用户重新描述任务。
5. 恢复失败在边界退回原 Driver；若新 Driver 已完成一个步骤，基于最新 checkpoint 恢复，不用旧快照重复该步骤。
6. 人工取消仍优先；切换过程中到达的新消息进 Host 收件队列，只消费一次。Driver 自己要求替换时同样经过管理器。

验收 H8-A：同一任务 default→plan→default，PID/session/task 不变且内容连续。H8-B：一次有副作用操作只执行一次。H8-C：模型流/工具运行中请求切换，等待边界后完成。H8-D：新 Driver 初始化失败原任务继续。H8-E：模拟进程崩溃恢复时只查不确定动作。H8-F：独立确认、FC 验证与取消都不能被新 Driver 绕过。
补测 H8-G：数据库提交前、路由提交后会话展示前、工具结果保存后三个崩溃窗口符合 5.5。H8-H：有状态计划切回和重启保持已完成位置；不兼容 state 明确重规划未完成部分或拒绝，不重复动作。H8-I：远端 Driver 数据投影不含未授权内容、预算不重置。
失败恢复：边界恢复默认 Driver；无法证明当前动作是否执行则暂停并显示已有事实，不从头重跑。

H8 已完成验收：已实现 `default-react@1` 与 `plan-then-act@1` 两个受信 Driver，
并由同一 Host/AgentRunner 调度；`loop_handoff_state` 使用既有 DevPilot SQLite
数据库中的独立表保存 owner generation、不可变 Driver state 和 H7 checkpoint。
`/plugins switch-loop` 仅创建 pending operation，工具运行/未知副作用期间不交接；
安全边界到达后同一 session/task 自动继续。当前仍需完成远端受限 Driver、跨存储崩溃
对账和 H8-A–I 现场验收均已通过；现场证据见
`docs/evidence/live-harness/h8-loop-handoff.md`，双轴审查见
`docs/reviews/h8-standards-review-2026-09-14.md` 与
`docs/reviews/h8-spec-review-2026-09-14.md`。H9 之前不接受远端或模型生成
Driver，避免未授权投影和本机执行。

### H9：以任务反馈选择插件/Loop 版本

前置 H8。目标：模型能用结果改进实现，但只发布被证明合适的版本。
落点：复用 P8 证据/评测思想、既有评测 runner；新增 Harness 候选记录，不把 Loop 伪装成 Skill。

1. 证据引用真实任务/工具结果/用户纠正，记录作用域、版本、成本。基础设施错误独立归因。
2. 一次只改变一个插件或一个 Driver，模型生成候选→固定测试→试用→比较→保留或退回，不同时搜索整棵插件树。
3. 固定 6 个 fixture 场景：2 个目标、2 个未用于生成的保留、2 个回归；每场景旧/新各 2 次，共 24 次。同模型/预算/输入；保留全部失败和实际用量。
4. 晋升必须契约全部通过、目标成功数严格提高、保留与回归不降低，且无重复副作用/权限越界；没有改善保留旧版。只声称本测试集结果，不声称全局最优。
5. 单轮最多 2 个候选，最多 48 次评测，token/费用预算启动前由用户设定；没有费用数据就显示 calls/tokens，不虚报费用保证。
6. 在既有授权内会话级试用自动进行；跨会话持久启用用一次原生聊天确认，绑定候选/结果哈希。Skill 仍走 P8 原确认。恢复上一版本可直接执行受信停用操作，不能以回滚为名重放业务动作。
7. 先手动闭环；定时只复用原生 Cron，需独立启用，默认不替用户创建新的付费评测任务。

验收 H9-A：预先植入一个确有缺陷的纯计算版本，通过修正得到可复现改善。H9-B：无改善/超预算/评测被候选篡改不得晋升。H9-C：Loop 候选也能走同一选择流程。H9-D：新任务选持久版本，旧任务按 H8 边界迁移而非被强制打断。
补测 H9-E：同一持久确认并发消费只产生一个 operation；结果/基线/作用域变化拒绝；版本回收遵守持久引用，不能删掉可恢复的稳定版本。
失败恢复：停止本轮选择，保留稳定版本、证据和剩余预算；不无限生成候选直到碰巧通过。

### H10：全链路与换机交付

前置 H9。目标：用实际演示证明核心能力，交付下一个模型可接手的版本。

1. 原生网页录制一个有多步数据处理和代码修复的自有场景：读取→发现缺工具→生成/试用→更新→卸载→Loop 切换→项目提案→聊天确认→FC 验证→结果查询→按需确认撤销。
2. 同时运行另一个会话，证明局部变更没有重启整个系统。记录 PID、任务 ID、版本、操作时间和调用事实；视频字幕不能代替日志证据。
3. 重放第 3 节保留功能的必要集成回归；历史无影响测试可复用，不能把旧全量计数当本次结果。
4. 在独立数据副本验证重启和工件缺失/版本不兼容；不覆盖用户现有配置、数据库或对话。新电脑使用既有目录时原地 pull，不另建同名产品副本。
5. 提供一份用户指南：新增/更新/停用/删除、试用范围、为何等待、怎样恢复、哪些模块仍需发布重启。所有本机路径/缓存用 E 盘。
6. 记录实测管理操作延迟 P50/P95（注明样本数）、50 次装卸残留数、并行任务完成率、24 次对照数据；不把目标值写成生产数据。

验收 H10-A：H2/H6/H8 三类真实网页证据齐全。H10-B：原有功能回归通过。H10-C：换机/故障恢复可复现。H10-D：文档逐项区分已实现、限制和未实现；只有 H1–H10 全部满足才写“运行中可重组 Harness 扩展完成”。

## 7. 审查、节奏与停止条件

本次计划工作：一个子代理只读审查 0.1 初稿→主代理一次汇总修订为 0.2→保存原发现和处理说明；不再启动第二轮。该规则来自用户本次明确要求，仅适用于计划审查，不冒充实现已通过双轴审查。
后续代码每个节点按 ADR-0004：整个节点预检→一次 Standards/Spec 并行审查→集中修复→原审查员只复核增量及关联风险。不要每个文件/每个问题重新全量 Review。
审查必须指出实际触发场景、违反哪一条、建议的最小修复；命名偏好/未来需求不阻塞。不因最后写 PASS 记录再次开审。
若一轮修订后还有未解决设计问题，如实列出，不用“只改一次”推断通过。代码审查也不得为赶节点忽略明确缺陷。

建议里程碑：H1–H3 用户可用热插拔；H4–H6 模型运行中造工具；H7–H8 Loop 接续；H9–H10 有证据的自改进与交付。每个里程碑可单独演示，但不能提前宣称最终目标完成。
没有真实云端凭据时可完成契约/离线工作；H5 已保留一次 `provider-default` 真实 FC
试运行与清理证据，H6/H10 的真实云端验收仍待完成；不能静默以本机执行代替。
不要承诺日历工期或收益百分比。每次交接更新下表和对应证据，下一模型只执行首个未完成节点。

| 节点 | 状态 | 实际证据 |
|---|---|---|
| H1 | 已完成并通过双轴审查 | `docs/evidence/live-harness/h1-contract-baseline.md`、`docs/reviews/h1-*-review-2930119.md` |
| H2 | 已完成并通过双轴审查 | `docs/evidence/live-harness/h2-plugin-lifecycle.md`、`docs/reviews/h2-*-review-b8141f0.md` |
| H3 | 已完成并通过双轴审查 | `docs/evidence/live-harness/h3-plugin-update-cleanup.md`、`docs/reviews/h3-*-review-6159add.md` |
| H4 | 已完成并通过双轴审查 | `docs/evidence/live-harness/h4-plugin-dependencies.md`、`docs/reviews/h4-*-review-78456c6.md` |
| H5 | 已完成并通过双轴增量复核 | `docs/evidence/live-harness/h5-candidate-execution.md`、`docs/reviews/h5-*-review-e08299e.md` |
| H6 | 已完成；最终增量复核 PASS | `docs/evidence/live-harness/h6-model-candidates.md`、`docs/reviews/h6-final-review-b55c027.md`；包含 H6-A/H6-F 现场证据及最终修复 |
| H7 | 已完成并通过双轴增量复核 | `docs/evidence/live-harness/h7-loop-driver-boundaries.md`、`docs/reviews/h7-final-review-0cdb1eb.md` |
| H8 | 已完成 | H8-A–I 真实验收、证据与双轴审查均已通过；可进入 H9 |
| H9 | 已完成并通过双轴增量复核 | `docs/evidence/live-harness/h9-selection.md`、`docs/reviews/h9-*-review-2026-09-14.md`；provider-default 无安全回执时 fail-closed |
| H10 | H10-A/H10-B/H10-C 完成，H10-D 待本轮双轴复核 | `docs/evidence/live-harness/h10-delivery.md`：启动门禁、原生 WebUI 会话、并发、50 次装卸、隔离恢复、生产 build、用户指南及命令历史回归的真实 WebSocket 验证已完成；本轮同一真实 WebUI 会话已补齐既有 FC 配置下的项目提案→聊天确认→FC 验证→结果查询→受保护撤销→刷新重连→回滚后哈希复核；fake 证据仍单独标注。 |

## 8. 下一条开发指令

“读取 AGENTS、CURRENT_PLAN、ADR-0004 和 LIVE_HARNESS_EXTENSION；H1–H9 已完成，不要重做。先核对 H9 证据、代码、测试和工作区，再从 H10 的真实演示、跨机恢复和交付清单开始；按 ADR-0004 完成批量 review。”

## 9. 本次一次审查与一轮修订记录

审查员：Boole，agent ID 01a08b75-5539-7a41-b25b-d8f5b868bac7；只读计划审查，未运行测试，未递归派发。
初稿 0.1 SHA256：0FADC4933343BEBB58123DE90642D85017EB12D8F8EF0C3C340B2D0253A0FE12；基线 2368a19。
原结论：方向可实施，但初稿不足以直接交给弱模型落实，须补以下八项。主代理接受全部建议并一次修订为 0.2；下表是修订处理，不冒充审查员复核关闭。

| ID/原严重度 | 原发现 | 0.2 处理位置 |
|---|---|---|
| LH-01/P1 | 未覆盖 runner 实际派发及 await hook 撤销竞态 | §4 入口、§5.3、H2-E、H7 落点 |
| LH-02/P1 | 固定 Profile 白名单无法接纳动态名称 | §5.1 明确能力开关及组合视图、H2-F |
| LH-03/P1 | 试用预授权与逐版本晋升确认混用 | §5.1 两种授权、H6-F、H9-E |
| LH-04/P1 | preparing 的监听/任务可能提前执行 | §5.2 惰性资源/原子入口、H3-F |
| LH-05/P1 | 激活后的执行/数据投影/预算未覆盖 | §5.6、H5-F、H8-I；activate 绑定成功 trial |
| LH-06/P1 | SQLite 与会话 JSONL 恢复权威不明确 | §5.5 写入顺序和对账、H8-G |
| LH-07/P2 | 缺少有状态 Driver 接续协议 | §5.5 driver_state、H8-H |
| LH-08/P1 | 工件删除未考虑持久引用 | §5.2 回收条件、H3-G、H9-E |

本次只更改设计文档与必读入口；文档本地链接/格式由主代理检查。八项均已在设计层落实，代码正确性须在相应节点测试和双轴审查中证明。
