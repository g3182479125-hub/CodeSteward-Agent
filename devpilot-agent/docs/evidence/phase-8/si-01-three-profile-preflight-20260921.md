# SI-01：三 Profile Skill 改进只读预检

日期：2026-09-21（Asia/Shanghai）
基线：`21fd780` (`origin/codex/h11-complete-handoff`)
分支：`codex/h11-complete-handoff`
状态：**通过，解锁 SI-02**

## 1. 本节点边界

本节点只读取仓库代码、Skill 文件和本机已有 DevPilot Skill-improvement
存储，确认真实绑定和路径。没有修改 Agent Loop、Apply/Verify/Rollback、权限、
Scheduler、数据库 schema 或用户项目。候选 Skill、反馈和本机凭据不写入 Git。

## 2. 三个 Profile 的真实绑定

`nanobot/nanobot/agent/task_profiles.py` 定义了三个服务端拥有的 Profile：

| Profile | 版本/绑定 | Skill 名称 | 运行时用途 |
|---|---|---|---|
| `repo-analysis` | `repo-analysis@1` | `repo-analysis` | 只读项目分析 |
| `feature-delivery` | `feature-delivery@2` | `feature-delivery` | 小功能交付 |
| `bug-fix` | `bug-fix@1` | `bug-fix` | Bug 修复与 P8 试点 |

Profile 由服务端写入 session metadata；模型不能自行改变绑定。`TaskContextState`
位于同一 session metadata 的 `task_context` 字段，当前字段来源如下：

- `session_ref`：当前 session key 的 SHA-256 前 16 位；
- `project_ref`：解析后的项目路径 SHA-256 前 16 位；
- `profile_binding`：Profile 名称和版本；
- `task_id`：首次建立上下文时生成的 `task-<uuid>`，后续复用；
- `run_id`：由 DevPilot 工具结果中的 `run_id` 写入；
- `change_set_id`：由提案/Changeset 工具结果写入；
- `evidence_refs`：由真实工具结果、请求参数或受控本地引用写入；
- `file_hashes`：由 `devpilot_read_code` 结果中的路径和 SHA-256 写入。

上下文文本会做长度限制和凭据脱敏，不是权限授权凭证。

## 3. 当前实际生效 Skill 路径和哈希

本节点使用交接 runbook 指定的训练项目：
`training-projects/python-project-template-bugfix`。
`SkillsLoader` 的实际优先级是“项目 workspace `skills/` 优先，内置目录其次”。
该仓库的 nanobot 内置目录没有这三个 Profile，因此本次三类 Skill 均从训练项目
workspace 生效：

| Skill | 生效绝对路径 | SHA-256 |
|---|---|---|
| `repo-analysis` | `training-projects/python-project-template-bugfix/skills/repo-analysis/SKILL.md` | `066aa7f940c80a846a995de4202ab274867d6746c4aefc9b484ebe38ec3cb659` |
| `feature-delivery` | `training-projects/python-project-template-bugfix/skills/feature-delivery/SKILL.md` | `a4710db8c51d9a5bdcd7c541a1ef8afab9ae14929811de58fd8bfe76a659cb79` |
| `bug-fix` | `training-projects/python-project-template-bugfix/skills/bug-fix/SKILL.md` | `eefe6bfcbec897a8f9b394ca0887297de0e36b1938b358ff7038160645b6dbe8` |

`devpilot-agent/skills/catalog/` 是 DevPilot 目录中的来源/模板，不是该训练项目
运行时的直接生效路径；其中 `feature-delivery` 和 `bug-fix` 哈希与训练项目版本
不同，不能混用。

## 4. 当前 P8 存储和真实缺口

当前环境的 `DEVPILOT_DATA_DIR` 为本机私有目录，数据库存在且可通过当前 Alembic
head 校验。通过 `SkillImprovementStore.from_environment(workspace)` 读取训练项目
范围得到：

- 反馈记录：`0`；
- 候选记录：`0`；
- 本机没有可迁移的 P8 反馈、候选或确认记录；
- 现有 `SkillImprovementStore` 的 `record_feedback` 和 `create_candidate` 将
  `skill_id`/路径固定为 `bug-fix`；
- `loop.py` 的 Skill snapshot 入口也只在 `profile.name == "bug-fix"` 且受控环境
  开启时调用 P8 store；另外两个 Profile 仍走普通 `SkillsLoader`。

因此，三 Profile 扩展的最小真实缺口是：把目标 Profile/Skill 作为受信的注册表和
数据契约字段贯穿现有 P8 store，同时保留候选不进入 `SkillsLoader`、不自动激活、
不改变项目交付门禁。SI-01 没有把该缺口误报为已实现。

## 5. 可复现检查

```powershell
cd E:\\codex-local\\nanobot-devpilot-agent\\nanobot
.\\.venv\\Scripts\\pytest.exe --basetemp .pytest-si01-nanobot tests/agent/test_task_profiles.py tests/agent/test_task_profile_session.py tests/command/test_skill_command.py -q -rA
# 44 passed

cd E:\\codex-local\\nanobot-devpilot-agent\\devpilot-agent
..\\nanobot\\.venv\\Scripts\\pytest.exe --basetemp .pytest-si01-devpilot tests/test_p8_01_skill_improvement.py tests/test_p8_02_skill_improvement.py tests/test_p8_03_skill_improvement.py -q -rA
# 31 passed
```

## 6. SI-02 解锁条件和限制

SI-02 可以开始，但只允许在现有 P8 存储和校验边界内扩展目标 Profile 的数据契约：

- 复用现有 SQLite、完整性校验、候选状态和 Cron 租约；
- 目标 Profile 必须来自服务端注册表，不能由模型字符串自由创建；
- 真实 Skill 路径/哈希必须在创建反馈和候选时重新核对；
- 不从模型文本推断 Verification 失败；
- 不修改 `Apply`、`Verify`、`Rollback`、权限确认或 Agent Loop 主循环；
- 通过 SI-02 自测后，再按 ADR-0004 固定候选并启动 Standards/Spec 双轴 review。
