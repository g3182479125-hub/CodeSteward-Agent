# 三 Profile Skill 受控迭代：换机交接文档

日期：2026-09-21（Asia/Shanghai）  
仓库：`https://github.com/g3182479125-hub/devflow-agent.git`  
分支：`codex/h11-complete-handoff`

## 当前状态

本次只完成计划登记和交接材料，没有实施三 Profile Skill 改进代码。

已登记的后续计划：

- `devpilot-agent/docs/plans/THREE_PROFILE_SKILL_IMPROVEMENT_EXTENSION.md`
- `devpilot-agent/docs/plans/CURRENT_PLAN.md` 第 10 节

该计划状态仍是 `proposed`。它不改写 P0–P8、H1–H11 的历史完成状态，也不自动重开
已完成节点。后续实施必须严格按 SI-01 → SI-08 顺序推进。

当前分支还包含此前已经完成并审查通过的“移除最终交付验收”修复：验证完成后不再要求
用户输入“接受交付”，保留停止和回滚；旧 `accepted` 数据只读。相关最近提交包括：

```text
95d3d44 record final acceptance removal review
c39ff5d expose legacy delivery error clearly
ebc4319 make legacy accepted deliveries read-only
eef766c remove final delivery acceptance step
```

## 换机拉取

在另一台电脑执行：

```powershell
git clone -b codex/h11-complete-handoff https://github.com/g3182479125-hub/devflow-agent.git
Set-Location .\devflow-agent
git pull --ff-only origin codex/h11-complete-handoff
git status --short
```

如果仓库已经存在：

```powershell
git fetch origin
git switch codex/h11-complete-handoff
git pull --ff-only origin codex/h11-complete-handoff
```

## 开始后续开发前的阅读顺序

1. 根目录 `AGENTS.md`；
2. `devpilot-agent/docs/plans/CURRENT_PLAN.md` 第 0、6、7、10 节；
3. `devpilot-agent/docs/plans/THREE_PROFILE_SKILL_IMPROVEMENT_EXTENSION.md` 全文；
4. `devpilot-agent/docs/plans/SKILL_IMPROVEMENT.md` 全文；
5. `devpilot-agent/docs/adr/ADR-0004-batched-review-and-incremental-recheck.md`；
6. 与当前节点相关的工程 Skill 和现有 P8 实现。

## 下一步：只做 SI-01 只读预检

下一轮不能直接写 `skill_improvement.py` 或 Agent Loop。先确认以下事实并形成 SI-01
节点证据：

- 三个 Profile 的实际 `TaskContext` 绑定方式、`profile_binding`、`task_id`、`run_id`、
  `change_set_id` 和 `evidence_refs` 来源；
- `SkillsLoader` 实际加载的三个生效 Skill 绝对路径；
- 当前生效 Skill 内容 SHA-256、workspace、Task Profile 名称和版本；
- 候选 Skill 的实际启用路径，以及它是否与运行时生效路径一致；
- 现有 `SkillImprovementStore`、P8 记录、MAC、Cron 租约和旧字段兼容方式；
- 哪些 Verification 位置能提供可信的失败事实，不能从模型文本推断失败。

SI-01 预检完成前：

- 不新建 `ImprovementCase` 或 `ReplayTrial` 表/结构；
- 不修改真实项目、生产项目或用户工作区；
- 不修改 Apply、Verify、Rollback、权限或确认契约；
- 不启动新的 Scheduler、Agent Loop、数据库或管理页面；
- 不把 `test-repair`、普通聊天、Dream 记忆或动态插件纳入范围。

## 计划摘要

后续计划新增的主要概念是：

- `ImprovementCase`：以可信 `origin_run_id` 为主锚点；
- `ReplayTrial`：在独立 Git worktree 中记录 baseline/candidate 评测；
- 三 Profile 目标注册表；
- 复用现有 Cron 的单 Case 领取、轮转、租约和恢复；
- 受信 grader 判断事实，不接受模型自报通过；
- 候选通过后仍必须由用户 `enable`，不能自动启用。

实施节点为 SI-01 至 SI-08：目标注册表与路径预检、数据契约、可信触发、快照隔离、
Cron 选择、候选生成约束、真实 Replay/grader、用户确认与端到端证据。

## 本地配置和测试边界

模型配置、FC 凭据、数据库、会话、临时目录和工作区数据均不在 Git 中。换机后使用
目标电脑自己的本地模型配置，不复制另一台电脑的密钥或 `.nanobot` 数据。

本分支此前与最终验收移除相关的聚焦测试为 9 passed，Ruff 和 `git diff --check` 通过。
历史全量测试仍有 3 个与本次改动无关的既有失败：工具注册数量/Skill 清单不一致，以及
临时工具持久化链路失败；它们不能被描述为本计划已通过，也不是 SI-01 的执行证据。

## 交接完成标准

换机后确认：

1. 分支为 `codex/h11-complete-handoff`，工作区干净或仅有自己的本地配置；
2. 能读到本文件、CURRENT_PLAN 第 10 节和完整三 Profile 计划；
3. 先完成 SI-01 只读证据，再决定是否进入 SI-02；
4. 每个节点单独提交、测试、双轴 review，并记录实际命令和限制；
5. 没有证据时不宣称三 Profile Skill 改进已经实现或启用。
