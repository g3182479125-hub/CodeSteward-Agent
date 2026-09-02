# P4-01 独立产品/质量 Review Plan（冻结计划 v2.2.0）

## 评审身份与范围

- 评审节点：`P4-01`，`feature-delivery` Skill/Workflow 与 G-01。
- 目标实现提交：当前分支 `handoff-2026-09-01` 的 `HEAD`，评审时固定并记录完整 SHA；本次目标不得包含用户未提交的 fixture 修改。
- 冻结计划：`D:\Users\tianmin.li\Documents\Codex\2026-08-10\new-chat\outputs\DevPilot-AI-Handoff-Portable-2026-08-31-v2\plans\DEVPILOT_FROZEN_PLAN.md`，版本 `2.2.0`，评审前记录 SHA-256。
- 评审方式：独立只读 Review subagent；不得修改 `src/`、`tests/`、配置、迁移、Skill、前端或产品实现。允许新增本 Review 计划和 Review 报告。

## 用户结果与非目标

P4-01 应让用户把边界明确的 FastAPI 功能需求转化为：只读事实、冻结 Change Plan、外部审批、不可变 ChangeSet、Diff、受控 Fake 验证、服务端 Artifact、真实失败状态和显式回滚。它验证的是受控代码交付闭环，不是开放式 Coding Agent。

本节点不包含：`bug-fix`、`test-repair`、自动修复循环、真实项目代码执行、任意 Shell、依赖安装、网络下载、自动 commit/push/deploy、完整 React 控制台和 P6 Sandbox 能力。Fake 只能模拟验证。

## 节点验收问题

1. G-01 是否有一条可复现主链：`inspect/search/read → provenance/hash → immutable plan/changeset → external apply approval + exact confirmation → Apply → external verify approval → Fake verification → server-owned Artifact → explicit rollback`。
2. 是否覆盖非零验证失败、Artifact 数据库写入失败和终态不可伪造；失败不能生成成功状态或成功 Artifact。
3. Skill、Tool、nanobot adapter 是否 proposal-only；是否没有 `command`、`shell`、任意环境、后端选择或直接 R1/R2 token 输入。
4. P6 前是否 fail closed，不回退到 nanobot Shell、任意 subprocess 或真实自动修复；路径、秘密、项目绑定、审批、哈希、幂等和回滚是否仍受策略保护。
5. evidence 是否绑定同一可复现实现提交，且能由机器结果支持核心断言；非关键格式、额外截图和未影响当前节点的性能整理按 follow-up 处理。

## 证据清单

- `git status --short`、目标提交完整 SHA、diff/stat 和变更文件；用户-owned fixture 修改单独记录，不计入实现。
- `docs/evidence/phase-4/p4-01-acceptance.md`、`manifest.json`、`g01-runtime-evidence.json`。
- P4-01 实现、Skill、Tool 注册/schema、工作流、策略、状态机、Artifact/DB 代码及相关测试。
- `uv run pytest -q`、相关 P4 测试、Ruff、Pyright、迁移/secret/lock/Markdown 检查结果；只执行与评审相关的安全、确定性、只读命令。
- Apply/Verify/Rollback 外部审批、精确确认、拒绝/消费、Fake 结果、Artifact 哈希、终态和事件序列证据。

## 严重度与决策

- `blocker`：未批准写入、审批/权限绕过、路径或网络逃逸、秘密泄漏、哈希/回滚不可信、执行后端虚假声明、数据损坏或核心测试失败。不得放行。
- `major`：G-01 核心行为、兼容性契约、核心错误处理或当前节点必需证据缺失。必须在进入 P4-02 前修复。
- `minor / follow-up`：文档措辞、证据排版、额外截图、非关键 UI polish、未影响依赖节点的性能/可观测性补充。可 `approve-with-follow-ups` 放行，但需责任人、证据路径和阶段 Gate/发布前期限。
- `observation`：不影响当前验收的建议或未决产品问题。

决策状态：`approve`、`approve-with-follow-ups`、`request-changes`、`blocked`。只有 `blocker/major` 阻塞 P4-02；独立 Review subagent 缺失时不能自行放行。

## 报告模板

报告必须包含：`decision`、`requirement coverage`、`observed behavior`、`evidence inspected`、`severity`、`reproduction steps`、`recommendation`、`regression risk`、`unanswered questions`。如为 `request-changes/blocked`，必须给出精确解锁证据和复测条件。
