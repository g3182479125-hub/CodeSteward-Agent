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
