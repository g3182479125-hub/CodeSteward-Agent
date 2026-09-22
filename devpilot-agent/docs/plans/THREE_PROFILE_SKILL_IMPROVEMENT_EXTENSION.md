# 三 Profile Skill 受控迭代扩展计划

版本：0.1；2026-09-21；状态：**SI-01–SI-07 已实施；SI-08 待实施**。

## 0. 定位、效力与禁止事项

本文是对现有 P8 试点的后续扩展计划，目标是把受控 Skill 改进的适用范围从单一
`bug-fix` 试点扩展到三个已经存在的 DevPilot Profile：

```text
repo-analysis
feature-delivery
bug-fix
```

本文不改写下列历史事实，也不重新认证已完成节点：

- `CURRENT_PLAN.md` 的 P0–P8 和 H1–H11 状态；
- `SKILL_IMPROVEMENT.md` 中 P8-01/P8-02/P8-03 已完成的实现、验收和 review；
- 既有私有候选、聊天确认、原子启用、恢复、Cron 租约、去重和预算边界。

现有 P8 的“只处理 `bug-fix`、固定文本评测、每批最多两次模型调用”是历史试点的
事实；本计划实施后，仅在本文定义的新三 Profile 流程内以本文件为准。它不是对旧
数据的静默重解释。实施首节点必须记录旧字段到新字段的兼容映射。

本文不授权：

- 自动启用、自动恢复或直接写入生效 Skill；
- 自动修改用户真实项目、生产项目或非隔离工作区；
- 对所有聊天消息进行意图识别；
- 新建第二个 Agent Runtime、第二个调度器、第二个数据库或管理页面；
- 将 `test-repair`、普通 nanobot 内置 Skill、Dream 记忆或动态插件纳入本次优化范围；
- 放宽 Apply、Verify、Rollback、工具权限或用户确认要求。

开发前仍必须完整阅读根 `AGENTS.md`、`CURRENT_PLAN.md` 第 0/6/7 节、
`ADR-0004-batched-review-and-incremental-recheck.md`、`SKILL_IMPROVEMENT.md` 和
本文件。本文的代码变更依旧适用现有测试、固定候选、Standards/Spec 双轴 review 与
增量复审要求。

## 1. 产品规则：不做意图识别，只处理明确 Profile 的真实 Run

### 1.1 任务锚点

不要在每条用户消息进入时让模型判断“它属于哪个任务类型”。那是意图识别，错误
分类会污染后续反馈和候选归因。

当前系统已有受信事实：

- 用户通过 `/task-profile` 显式选择 Profile；
- `TaskContextState` 已保存 `profile_binding`、`task_id`、`run_id`、`change_set_id`
  和 `evidence_refs`；
- DevPilot 工具和服务端 Run 已保存项目、验证及操作结果。

本计划**复用**已有 `task_id`，不新建 Task/TaskEpisode 表。每条可优化问题的主锚点是
`origin_run_id`；`task_id` 只作为同一 Profile 会话上下文的辅助关联字段。

```text
用户显式选择 Profile
→ 系统创建/延续已有 TaskContext
→ DevPilot 实际执行产生 run_id
→ 发生一个受信失败或用户明确反馈
→ 建立 ImprovementCase(origin_run_id=该 run_id)
```

第一版不支持“用户隔两天自然语言说上次不对”后由系统自动猜测历史 Run。用户反馈
只能绑定当前仍在进行的 Profile 会话和其中的 `run_id`。没有 `run_id` 时必须拒绝，而
不是猜测或创建无来源案例。

### 1.2 仅允许的三类触发

新增的三 Profile 改进流程只接受下表中的信号。

| Profile | 允许的 trigger_type | 产生者 | 额外条件 |
|---|---|---|---|
| `repo-analysis` | `user_confirmed_missing_fact` | 用户直接命令 | 当前 Profile 为 `repo-analysis@*`、当前 `run_id` 存在、用户提供可验证的期望事实 |
| `feature-delivery` | `trusted_verification_failed` | 服务端 Verification 完成点 | 精确 ChangeSet 已经用户确认、Apply 已执行、服务端明确记录 Verification 失败 |
| `bug-fix` | `trusted_verification_failed` | 服务端 Verification 完成点 | 有保存的失败证据、精确 ChangeSet 已经用户确认、修复后的回归验证明确失败 |

下列情况必须记录为普通运行信息或普通聊天，**不得**建立 `ImprovementCase`：

```text
没有 Profile 的对话
不在三个 Profile 内的对话
模型自我反思或模型口头承认可能错误
普通工具输出、Provider 超时、网络故障、取消
用户临时改变需求、模糊负面情绪、没有预期结果的评价
提案尚未执行、用户尚未确认、没有服务端验证结果
没有 origin_run_id、项目不匹配或来源不可信
```

## 2. 目标注册表和有效 Skill 路径

在 `devpilot-agent/src/devpilot/skill_improvement.py` 定义唯一的受信注册表；不要在
命令、Cron、Tool 和评测器中分别写三份 if/else。

```python
@dataclass(frozen=True)
class SkillImprovementTarget:
    skill_id: str
    profile_name: str
    allowed_trigger_types: frozenset[str]
    grader_kind: str


SKILL_IMPROVEMENT_TARGETS = {
    "repo-analysis": SkillImprovementTarget(
        skill_id="repo-analysis",
        profile_name="repo-analysis",
        allowed_trigger_types=frozenset({"user_confirmed_missing_fact"}),
        grader_kind="analysis_fact",
    ),
    "feature-delivery": SkillImprovementTarget(
        skill_id="feature-delivery",
        profile_name="feature-delivery",
        allowed_trigger_types=frozenset({"trusted_verification_failed"}),
        grader_kind="delivery_verification",
    ),
    "bug-fix": SkillImprovementTarget(
        skill_id="bug-fix",
        profile_name="bug-fix",
        allowed_trigger_types=frozenset({"trusted_verification_failed"}),
        grader_kind="bug_fix_verification",
    ),
}
```

实现前置检查必须定位 SkillsLoader 实际加载的生效文件，并为每个 target 记录：

```text
skill_id
实际生效的绝对路径
内容 SHA-256
所属 workspace
对应 TaskProfile 名称和版本
```

不得仅因 catalog 文件存在就硬编码路径。若运行时加载路径与候选启用路径不一致，
必须先修复/测试这个解析映射；在映射明确前，不得给该 Skill 创建可启用候选。

候选只允许改变目标 Skill 的正文。下列内容为不可变保护区：

```text
metadata.allowed_tools
metadata.risk_level
Profile 工具白名单
审批/确认契约
系统提示词、AGENTS、SOUL、Dream、工具注册、执行脚本
```

## 3. 数据契约和持久化

继续使用 `SkillImprovementStore` 的现有私有数据根、完整性 MAC 和已有 SQLite 记录
机制；不得创建第二数据库。候选仍不得进入 SkillsLoader 扫描目录。

### 3.1 新记录：ImprovementCase

新增不可变数据类和存取方法：

```python
@dataclass(frozen=True)
class ImprovementCase:
    case_id: str
    skill_id: str
    profile_binding: str
    origin_run_id: str
    task_id: str
    project_id: str
    workspace: str
    user_id: str
    session_id: str
    base_skill_hash: str
    trigger_type: str
    trigger_summary: str
    expected_behavior: str
    evidence_refs: tuple[str, ...]
    replay_snapshot_ref: str | None
    assertion_spec: dict[str, object]
    status: str
    selected_batch_id: str | None
    created_at: str
```

允许状态只有：

```text
eligible          可由定时任务选择
claimed           正由一个带租约的批次处理
not_replayable    缺快照、快照失效或不满足隔离要求
evaluated         已产生最终评测结果
ignored           用户明确忽略
```

不要为第一版增加“待模型分析”“模型怀疑”“跨会话关联”等状态。

`SkillCandidate` 增加 `case_ids`；`EvaluationReport` 增加 `case_id`、
`baseline_trial_id` 和 `candidate_trial_id`。保留旧字段，读取旧 P8 记录时默认：

```text
旧候选没有 case_ids → ()
旧 ScheduledSkillBatch 没有 target_skill_ids → (skill_id,)
```

旧 `FeedbackEvidence` 仍可保留作审计来源，但它不再是新流程的调度单位。新 Cron
只领取 `ImprovementCase`，不能把任意 `verified` feedback 当作可优化案例。

### 3.2 新记录：ReplayTrial

每次真实评测产生一个只追加的记录：

```python
@dataclass(frozen=True)
class ReplayTrial:
    trial_id: str
    case_id: str
    skill_id: str
    skill_hash: str
    version: str  # baseline | candidate
    model_id: str
    parameters: dict[str, object]
    snapshot_ref: str
    started_at: str
    finished_at: str
    token_count: int
    duration_seconds: float
    tool_trace_ref: str
    final_output_ref: str
    assertion_result: dict[str, object]
    status: str  # passed | failed | timeout | cancelled | infrastructure_error
```

完整日志和项目文件不得直接塞入记录；只保存脱敏摘要和私有受限引用。

## 4. 受信 Case 创建入口

新增模块：

`devpilot-agent/src/devpilot/skill_improvement_cases.py`

该模块只暴露两个 public method：

```python
record_user_confirmed_analysis_issue(...)
record_trusted_verification_failure(...)
```

它负责验证 Profile、项目、Run、Skill hash、证据、触发类型和去重。模型、普通工具
输出和前端任意 payload 不能直接写 `ImprovementCase`。

### 4.1 repo-analysis 用户反馈

修改 `nanobot/nanobot/command/builtin.py` 中现有 `/skill-improvement feedback` 分支。

新格式：

```text
/skill-improvement feedback <遗漏/错误说明> --expected <确定性事实>
```

例子：

```text
/skill-improvement feedback 漏掉了测试入口 --expected tests/test_api.py
```

命令必须逐项校验：

1. 发起者是当前用户 turn；
2. `task_context` 存在；
3. `profile_binding` 为 `repo-analysis@*`；
4. `task_context.run_id` 是非空字符串；
5. `--expected` 能与当前 Run 的 inspect/search/read 证据匹配；
6. 该 Run、项目、workspace 和用户与当前会话一致；
7. 当前有效 Skill 的 hash 与 Case 基线 hash 一致。

任何一项不满足都要返回稳定错误，不保存半条记录。第一版不提供历史 Run 选择器。

### 4.2 feature-delivery 和 bug-fix 服务端失败

在 DevPilot 服务端的最终 Verification 事实落库位置调用
`record_trusted_verification_failure`，不得从模型 Markdown、聊天文本或前端展示状态
反推失败。

该调用必须接收服务端已有的：

```text
profile_binding
origin_run_id
task_id
project_id
change_set_id
verification result
allowed file scope
failure evidence refs
task-start snapshot ref
```

`feature-delivery` 只有“用户已确认 → Apply 已发生 → Verification failed”才创建 Case。
`bug-fix` 在此基础上还要求已有原始 failure evidence。Provider/FC 不可用、取消或未知
结果仅记录原有失败状态，不归因为 Skill。

## 5. 可复测快照和隔离规则

本计划不允许在用户真实项目目录评测 Skill。

第一版只接受能证明可复测的 Git 任务：

```text
项目已注册且已 trust
任务开始时工作区 clean
已记录任务开始时 Git commit SHA
已记录允许修改文件范围和验证 Profile
可由受信服务端创建临时 Git worktree
```

任务启动时增加受信快照捕获。快照引用至少包括：

```text
repository identity
start_commit_sha
working_tree_clean=true
allowed_file_scope
verification profile
source evidence refs
```

任一条件不满足时，Case 写为 `not_replayable`，原因可见但不调用模型。严禁退化为：

```text
直接在真实目录复跑
复制整个用户目录/完整聊天记录
自动 git commit 用户未提交的修改
根据当前工作区猜测旧任务状态
```

临时 worktree 必须位于受信私有临时根，清理失败要记录待清理状态。评测工具范围保持
原 Profile 的受限集合；不能借评测权限使用 Shell、联网下载依赖或访问其他项目。

## 6. 定时批处理

继续复用 nanobot 原生 Cron 和现有 `gateway_runtime.py` 调用链；不建第二调度器。

`ScheduledSkillBatch` 扩展为有序的：

```python
target_skill_ids = (
    "repo-analysis",
    "feature-delivery",
    "bug-fix",
)
```

默认关闭。用户显式启用后，建议默认计划为每天 02:00、`Asia/Shanghai`；保留现有
`every`、`cron` 和手动运行方式。一次 Cron 触发最多领取并处理 **一个** Case。

选择算法必须确定：

1. 只查询 `eligible` 且所属 target 的 Case；
2. 排除 workspace/project/user 不匹配、已领取、已评测和 `not_replayable` 的 Case；
3. 使用持久化的上次选中 Profile 进行轮转；
4. 在选定 Profile 内按 `created_at, case_id` 排序选择最早 Case；
5. 领取时写租约和 `selected_batch_id`；
6. 重启、并发触发或租约过期时，沿用现有 claim/recovery 语义，不能重复执行。

没有 eligible Case 时必须得到 `no_new_evidence`，并保证：

```text
0 次模型执行
0 条用户通知
不创建候选
```

通知只在候选进入 `pending_confirmation`，或批次出现需要用户处理的终止错误时发给
配置该 Cron 的原聊天。它是独立系统消息，不附加在后续普通回答末尾。

## 7. 候选生成和真实 A/B Replay

### 7.1 候选生成

保留 `devpilot_skill_candidate` 和私有候选存储，但从“仅接受 bug-fix”改为“只接受
注册表中的 target”。调用时必须核对：

```text
candidate.skill_id == case.skill_id
candidate.base_version_hash == case.base_skill_hash
candidate 只引用当前 claimed case
candidate 不修改保护区
candidate 不进入 SkillsLoader
```

候选生成的模型输入只能是：当前 Skill、Case 触发摘要、经过脱敏的可信证据摘要、
预期行为和不可变约束。不得传输完整会话、密钥、token、完整 stdout/stderr 或用户项目
的无关文件。

每个 Case 只允许一次候选生成；格式错误、Provider 失败或越界内容直接结束该批次，
不允许模型无限自我修正。

### 7.2 Replay Runner

新增：

`nanobot/nanobot/agent/skill_replay.py`

其 public interface 保持最小：

```python
async def run_skill_replay(
    request: ReplayRequest,
    skill_content: str,
) -> ReplayTrialResult:
    ...
```

内部必须复用现有 `AgentRunner`/`AgentRunSpec` 的实际 Agent 执行与 Profile 工具限制，
不得另写一个简化 Agent Loop。每次 Trial 建立全新的相同 Git worktree：

```text
baseline：任务开始快照 + 旧 Skill
candidate：任务开始快照 + 候选 Skill
```

候选 Skill 和基线 Skill 不得共享可写 worktree、会话、模型历史或工具结果。

### 7.3 受信断言

评测器必须由 Python 受信代码决定结果，候选 Skill 和模型不得自报通过。

| grader_kind | 通过条件 |
|---|---|
| `analysis_fact` | 输出中包含用户指定的确定性事实，并绑定正确文件/证据，且没有写工具调用 |
| `delivery_verification` | 服务端固定验证通过，且变更没有超出 Case 允许文件范围，也没有绕过确认 |
| `bug_fix_verification` | 原失败检查通过，变更没有超出允许范围，且没有伪造验证成功 |

现有 `FIXED_SKILL_SCENARIOS` 的字符串检查只可保留为兼容回归单测；它们不能作为候选
具有真实收益的最终证据。

### 7.4 预算

真正的流程至少包含候选生成、基线 Replay 和候选 Replay。因此本扩展不能继续把
“每批最多两次模型调用”当成真实 A/B 的预算。

新批次硬限制：

```text
3 次逻辑模型执行
总计最多 20,000 tokens
总计最多 600 秒
候选生成最多 4,000 tokens
每个 Replay 最多 8,000 tokens
```

若一次 Agent Replay 因工具调用产生多轮模型请求，以总 token 和总时限为最终硬限制。
预算原子预留；未知调用成本保持预留，不自动重试以规避上限。

## 8. 评测结论、确认和恢复

候选只有全部满足以下条件时才可进入 `pending_confirmation`：

```text
候选版目标断言通过
基线版失败，或候选版严格优于基线版
候选版无安全/范围违规
候选未改动保护区
两个 Trial 都有可读的结果和受信断言
```

否则写 `evaluation_failed`，保存原因和 Trial 引用，保持当前生效 Skill 不变。

第一版只对“产生该问题的真实 Case”作 A/B 判断，不能宣称对所有任务普遍更好。后续若
要引入跨 Case holdout，必须以新的计划节点和真实案例库实现，不能把固定字符串 fixture
包装成真实泛化证据。

通过时沿用已有聊天命令展示：来源 Run、问题摘要、Skill Diff 摘要、基线/候选结果、
预算和限制。可用动作仍只有：

```text
view
enable
ignore
restore
```

`enable` 必须继续绑定用户、会话、workspace、project、Skill、基础/候选 hash、评测
结果和过期时间；只影响新任务。任何内容、基础版本、结果或权限变化都使旧确认失效。
候选未确认、评测超时、Cron 取消和负面反馈均不得自动启用或自动恢复。

## 9. 实施节点和验收

每次只实施一个节点；完成代码、测试和节点证据后固定候选，再按 ADR-0004 进行
Standards/Spec 双轴 review。不得把节点 N+1 的代码塞入节点 N。

### SI-01：目标注册表与生效路径预检

- 新增三 target 注册表和统一查找方法；
- 证明三个 Profile 的实际 SkillLoader 源路径、内容 hash 和候选启用路径一致；
- 保持旧 bug-fix P8 数据和命令可读。

退出：非 target 被拒绝；三个 target 均有解析映射测试；没有路径映射时拒绝创建候选。

### SI-02：ImprovementCase 数据契约

- 新增 `ImprovementCase`、`ReplayTrial` 记录及旧记录兼容读取；
- 以 `origin_run_id` 为主锚点；
- 只允许目标 trigger 和同 scope 证据；
- 完成删除/失效传播：删除基础反馈或快照失效时，关联未启用候选必须失效。

退出：重复记录幂等；跨用户/项目/workspace 拒绝；模型推断和任意工具输出不能创建 Case。

### SI-03：三类受信触发入口

- 收紧 `/skill-improvement feedback`，只支持当前 repo-analysis Run 的明确事实反馈；
- 在 feature-delivery 与 bug-fix 的服务端最终 Verification 位置记录失败 Case；
- Provider/基础设施错误不进入 Case。

退出：每种允许 trigger 有正向测试；所有拒绝情形有负向测试；前端/模型无法伪造
`trusted_verification_failed`。

### SI-04：可复测快照

- 在 Profile Run 开始时捕获干净 Git commit、文件范围和验证 Profile；
- 实现私有临时 worktree 创建、校验和清理；
- 不可复测任务标记 `not_replayable`。

退出：真实项目目录零写入；dirty/non-Git/missing-commit 均被跳过；清理失败可见。

### SI-05：三 Profile Cron 选择器

- 将批处理选择单位改为 `ImprovementCase`；
- 支持三个 target、轮转、每次一个 Case、持久租约和恢复；
- 兼容旧单 bug-fix batch 配置。

退出：无 Case 零模型调用；并发/重启无重复；三个 Profile 不会永久饿死；停用/取消
不产生候选或启用。

### SI-06：候选生成约束

- 将候选工具从单 bug-fix 泛化到注册 target；
- 强制 Case/Skill/hash/保护区校验；
- 限制为一次 4k token 候选生成。

退出：候选不被 SkillsLoader 发现；越界 metadata/权限修改拒绝；错误 Case 无法为其他
Skill 建候选。

### SI-07：真实 Replay 和受信 grader

- 实现隔离的 `AgentRunner` Replay；
- 基线/候选使用不同但同源的 worktree；
- 实现三个 deterministic grader；
- 记录 ReplayTrial 和 EvaluationReport。

退出：候选 Skill 无法改 grader；真实目录零写入；基线/候选结果可追溯；timeout/
cancel/infrastructure error 不被当成通过。

### SI-08：用户确认、恢复和端到端证据

- 用真实 A/B 结果驱动 `pending_confirmation`；
- 复用现有 enable/ignore/restore 保护；
- Cron 通知展示为独立消息；
- 完成一条自有合成 Git fixture 的端到端演练。

退出：未确认零激活；确认后新任务使用候选而活动任务保留旧快照；restore 成功；全链路
证据明确标注为合成 fixture，不声称已覆盖所有真实项目。

## 10. 必测清单

至少新增并通过以下测试：

```text
repo-analysis 反馈缺少 run_id 被拒绝
repo-analysis 的自由反馈没有 --expected 被拒绝
普通聊天、非 target Profile、模型推断不创建 Case
feature-delivery 只有确认后的服务端 Verification 失败才创建 Case
bug-fix 只有保存失败证据加验证失败才创建 Case
跨 user/project/workspace 的 Run 或证据被拒绝
dirty/non-Git/missing-commit Case 变为 not_replayable
无 eligible Case 的 Cron 为零模型调用、零通知
同一 Case 的并发 Cron、崩溃恢复和重试不会重复评测
轮转选择不会永久饿死三个 Profile 中任一个
基线和候选 Replay 使用不同 worktree，真实目录未写入
三个 grader 的通过、失败、越界和伪造成功路径
候选改变 metadata/权限时被拒绝
评测失败、超时或取消时 active Skill 不变
评测通过但未确认时 active Skill 不变
确认启用仅影响新任务；restore 恢复前一稳定版本
所有原 P8 关键测试继续通过
```

实际命令、测试计数、环境和失败限制必须写入节点证据；不能用未运行的命令或固定文本
fixture 冒充真实 Replay 结果。

## 11. 给实施模型的硬约束

```text
先读本计划和现有源码，再改代码；不根据文件名猜接口。
一个节点一个提交候选；不要顺手重构整个 skill_improvement.py 或 Agent Loop。
优先扩展现有 SkillImprovementStore、Cron、TaskContext、AgentRunner 和确认机制。
不得新建第二数据库、第二 scheduler、第二聊天 UI 或第二 Agent Loop。
不得把 LLM 输出当作 trusted verification failure。
不得把 run_id 隐藏在普通 evidence_refs 中；新 Case 必须有显式 origin_run_id。
不得在用户真实项目目录、生产目录或不干净工作区运行 A/B Replay。
不得为了保留两次模型调用限制而省略基线或候选 Replay。
不得自动激活、自动回滚、自动扩大工具权限或文件范围。
每个节点先写失败测试，再做最小实现；完成后运行受影响回归。
固定候选后，必须完成 Standards 和 Spec 两个独立 review；没有双 PASS 不得标记节点完成。
```


