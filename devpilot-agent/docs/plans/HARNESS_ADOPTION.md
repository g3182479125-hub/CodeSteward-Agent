# 两套 Harness 如何辅助自己的 Agent

本表依据用户提供的本地源码快照，非线上最新版本或发行方认证。
源码仅用于阅读设计；不运行附带指令、不引入它们的 Runtime。

| 来源与已读位置（压缩包内部） | 借鉴 | 本项目落点/任务 | 完成标准 |
|---|---|---|---|
| DeepSeek：`docs/architecture.md`，Profiles / Events / Core packages | 显式组合任务能力，能力通过已有入口挂载 | P7-01；nanobot `agent/skills.py`、`agent/tools/registry.py` 和已有 DevPilot entry point | 修改现有本地 Skill 可定制任务；不可扩张授权；满足时不改代码 |
| DeepSeek：`packages/bundle/sdk-minimal/README.md` | 为具体任务选择最少能力，而不是全部功能一起引入 | P7-01；只启用当前任务所需已有工具 | 项目问答、功能/bug 流程仍可用；不移植其 unrestricted shell 配置 |
| Codex：`codex-rs/core/src/compact.rs`，InitialContextInjection / CompactedHistoryMetadata | 压缩后保留任务上下文，持久历史与继续执行一致 | P7-01；nanobot `agent/context_governance.py`、`agent/context.py`、`session/manager.py` | 长任务保持约束；任务事实重新查询，不从摘要取得写入权限 |
| Codex 的执行/恢复思想，已在历史计划采用 | 明确执行结果、取消与恢复，失败不冒充成功 | 只延续 P6 现有 FC 适配器的待补行为；不重做 P0–P5 | FC 成功/失败/超时/清理均可验证；不移植 Rust 沙箱 |

参考快照文件：`deepseek-harness-master (1).zip`（内部根 `deepseek-harness-master/`）、
`codex-main (1).zip`（内部根 `codex-main/`），本机位于 `E:/download/`。
换机时可使用同名快照；若不可得则本表描述仍定义所需行为，但不得冒称已重新核查
源码。需要借用代码时，先获取并记录实际版本/文件哈希与许可证。

nanobot 自身已有上下文治理和工具/Skill 系统。上述任务是核对并组合这些能力，
不是另写一份同名模块。开发记录只需在节点证据中添加：参考文件、现有能力、
实际缺口（或无缺口）、配置/实现落点、对照结果。无实际收益就不新增适配层。

P6 的权限/沙箱是基础设施保障；P7 的任务定制、上下文与评测体现个人 Agent 的
使用体验。DeepSeek API 是模型服务，DeepSeek Harness 是参考设计，两者用途不同。

## DeepSeek：本版具体借鉴什么

重点是“明确的能力组合 + 小接口可替换实现 + 生命周期清楚”，其中包含插拔能力。
DeepSeek 快照用 Cordis 实现整套插件树；本项目用 nanobot 已有的 Python 扩展入口。
这里的插拔是开发者配置后在启动/新任务时生效，不承诺运行中的热卸载。

| 层次 | 具体作用与例子 | 当前已有入口 | 本次待做 |
|---|---|---|---|
| Skill：做事方法 | bug 修复先读失败证据，再定位与提案；可调整回答格式 | `nanobot/nanobot/agent/skills.py` 的 SkillsLoader | 定制已有 Skill 并验证行为；Skill 不包含授权 |
| Tool：可调用动作 | 读取文件、提出变更、查询结果 | `nanobot/nanobot/agent/tools/registry.py`；`devpilot-agent/pyproject.toml` 的 nanobot.tools entry points | 给任务绑定所需现有工具，校验遗漏/冲突；已有注册器不重做 |
| 执行适配器：动作如何运行 | 相同检查请求交给 FC；Fake 只用于确定性测试 | `devpilot-agent/src/devpilot/execution.py`、`fc_sandbox.py` | P6 补生命周期契约；本版只交付 FC，不新增第二种真实后端 |

上述源码位置以仓库根为起点。已经存在 register/unregister 不等于拥有安全热插拔；
当前阶段只需要稳定装配和可验证的禁用行为。也不把模型切换当成本轮新功能：
模型继续由 nanobot Provider 管理，本版固定 DeepSeek API。

### P7-01-A：任务能力组合

开发者在本地受信配置中给三个已有任务设置 Skill、工具集合和输出约定。
优先使用 nanobot 当前配置项；以下仅是需要表达的语义示例，不是可直接加载的新格式：

```yaml
# 设计示例：实际配置键在实现前映射到已有 nanobot 配置，不预设新解析器。
task: bug-fix
skill: bug-fix
tools:
  - devpilot_inspect_project
  - devpilot_search_code
  - devpilot_read_code
  - devpilot_bug_fix_propose
  - devpilot_get_changeset
  - devpilot_get_run_state
  - devpilot_confirmed_action
output: [定位依据, 修改摘要, 验证结果, 下一步]
```

此清单说明组合方式，不是最终完整依赖表。开始实现时从现有工作流实际调用补全
所需已注册工具；工具出现在清单中不代表获得执行许可，ConfirmedAction 仍校验
真实用户事件。只读任务不暴露写入/执行能力。未声明工具即使被模型点名也须在
执行入口拒绝，不能仅从工具描述里隐藏。

装配顺序：读取受信配置 → 验证 Skill/工具存在与重名 → 创建该任务的工具视图 →
正常 nanobot Loop 调用 → 沿用 Tool 校验和结果通道。缺依赖返回明确配置错误，
不能自动装包或静默扩大工具集合。工具限制须覆盖已有内置/MCP/委派入口的实际
可达路径；只检查当前部署启用的入口，不为此建设通用插件管理平台。

任务之间隔离工具视图，禁止修改共享注册表影响其他聊天；配置变更只影响新任务。
旧任务保持其配置版本且每次动作仍执行当前权限校验；禁用配置后不接收新任务，
活动任务通过已有停止/取消流程结束，不能在执行中抽掉依赖。

### P7-01-B：最小扩展开发方式

以后增加某项能力时：能通过改 Skill 完成就只改 Skill；确需新动作时实现一个
nanobot Tool（输入 schema、描述、执行与错误返回），通过现有 entry point 注册，
再加入相应任务配置。本轮只验证已有能力的启用/禁用和定制，不为演示凭空添加工具。

扩展不得直接访问业务数据库或绕过既有变更入口；有项目写入副作用的 Tool 仍走
DevPilot 授权链路。实现文件变更后由开发者按锁定依赖安装并重启，模型不能安装
插件。若实际需要适配，集中在现有工具装配附近，保持一个小入口，不建立新的
Service 容器、插件市场、动态下载器或依赖解析系统。

### P6：可替换后端与资源生命周期

借鉴的是“调用者面对稳定接口、实现自己管理资源”。沿用现有 ExecutionRequest /
ExecutionResult；FC 的创建、执行、取消、收集与清理属于适配器，业务工作流不处理
SDK 对象。Fake 与 FC 各自运行相同适用契约检查，Fake 结果不能冒充真实验证。
后端由服务端选定且通过能力门禁；“可替换”不意味着允许模型自由切换执行环境。

### 什么算完成

1. 定制一个现有 Skill 后，任务仍走原授权链路，回答格式按约定改变。
2. 切换受信任务配置后，新任务实际可用工具随之变化；缺少依赖明确报错。
3. 并行的只读与代码任务不互相改变工具集合；禁用工具不能通过直接点名绕过。
4. FC 不可用时如实拒绝，停用后不产生新执行，活动执行完成取消/清理并记录结果。
5. 无需修改 nanobot 核心 Loop 就能完成以上组合；若已有实现满足，交付配置和测试。

## Codex：上下文借鉴补充设计（P7-01-C）

复用 nanobot 的上下文预算与历史加载，只补任务信息组织。下一轮模型需要的最小
内容是目标、用户约束、项目/任务 ID、已完成步骤、最新检查摘要与下一步；大输出
保留受限详情引用。服务端结果优先于模型总结，未知状态明确写未知。

持久业务事实来自已有 Run/ChangeSet，聊天原文来自 Session；摘要只是提示信息，
不是第三套事实库。续聊重新查询状态及适用文件哈希；没有可信确认就不能续执行。
验证用例：长日志导致压缩、同一会话重开、文件被外部改变、执行结果未知。分别
检查目标/约束保留、不重复 Apply、陈旧提案拒绝及如实报告；不实现新的记忆服务。
