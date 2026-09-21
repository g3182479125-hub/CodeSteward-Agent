# SI-05 / SI-06 跨电脑交接文档

日期：2026-09-22（Asia/Shanghai）  
仓库：`https://github.com/g3182479125-hub/devflow-agent.git`  
分支：`codex/h11-complete-handoff`  
当前提交：`dcf6ec29f80e9ecd93e2a395c39dcd373d022a66`

## 1. 明天在另一台电脑上恢复

```powershell
git clone -b codex/h11-complete-handoff https://github.com/g3182479125-hub/devflow-agent.git
Set-Location .\devflow-agent
git pull --ff-only origin codex/h11-complete-handoff
```

安装依赖并进入开发环境：

```powershell
nanobot\.venv\Scripts\python.exe --version
```

如果新电脑没有现成虚拟环境，先按仓库已有安装文档执行依赖安装，再使用仓库的
`nanobot\.venv\Scripts\python.exe` 和 `nanobot\.venv\Scripts\pytest.exe`。
不要复制本机的数据库、integrity key、token、日志、会话或临时目录；本次只上传了
代码、测试和本文档。

## 2. 本次已完成

### SI-05：三 Profile Cron 选择器

- 新 Cron 批次以 `ImprovementCase` 为调度单位，不再把任意旧 verified feedback 当作新案例。
- 支持 `repo-analysis`、`feature-delivery`、`bug-fix` 三个服务端注册 target。
- 按持久化的上次 Skill 选择结果轮转；同一批次每次只领取一个 Case。
- Case 领取会持久化 `selected_batch_id`、Case/evidence 绑定、租约和游标。
- 无可重放 Case 时返回 `no_new_evidence`，不会进入模型调用。
- 取消或 Provider/工具失败会释放 Case；失败累计保留模型调用、token 和失败次数，达到预算后返回
  `budget_exhausted`，不会无限重试。
- 租约过期、进程重启、并发 Cron claim 有持久化恢复和去重路径。
- 旧的单 bug-fix batch（缺少 `target_skill_ids` 的旧记录）仍走兼容路径。

### SI-06：注册 target 的候选生成

- 候选存储通过注册表解析 `skill_path`，不再硬编码 bug-fix。
- candidate 的 Skill、Case、baseline hash、workspace、project、user 和 evidence 必须一致。
- 保留 Skill frontmatter，拒绝 metadata/权限等保护区修改、敏感内容和超出大小限制的候选。
- Gateway 按当前 claim 的 `case.skill_id` 读取 baseline，并把 Skill/Case/evidence 绑定放入可信请求上下文。
- `devpilot_skill_candidate` 仍只创建私有 `pending_evaluation` 候选，不自动加载、启用或发布。
- SI-07 的真实 Replay、三个 grader、EvaluationReport 和用户确认启用，本次没有提前实现。

## 3. 关键提交

按时间顺序：

| 提交 | 内容 |
|---|---|
| `5af0b33` | SI-05 三 Profile Case scheduler 初版 |
| `401c4cd` | Case 失败/取消恢复、未支持 target gate、恢复/并发测试 |
| `33d96f5` | 失败重试预算累计与 `budget_exhausted` |
| `f17b345` | SI-06 按注册 target 泛化候选存储和工具 |
| `3f607a1` | Gateway 按选中 Case Skill 绑定 baseline；锁内复核 legacy/new 路径 |
| `dcf6ec2` | 明确初始化候选 target，消除静态类型检查的 possibly-unbound 问题 |

## 4. 验证结果

专项测试：

```powershell
nanobot\.venv\Scripts\pytest.exe --basetemp .pytest-handoff-final `
  devpilot-agent/tests/test_skill_improvement_scheduling.py `
  devpilot-agent/tests/test_skill_improvement_profiles.py `
  nanobot/tests/command/test_skill_command.py -q -rA
```

结果：46 passed。Ruff 和 `git diff --check` 通过。

完整受影响回归还执行过：

```powershell
nanobot\.venv\Scripts\pytest.exe --basetemp .pytest-si06-final `
  devpilot-agent/tests/test_skill_improvement_scheduling.py `
  devpilot-agent/tests/test_skill_improvement_profiles.py `
  nanobot/tests/command/test_skill_command.py `
  nanobot/tests/cli/test_commands.py -q -rA
```

受影响功能均通过；该运行仍报告 4 个仓库/环境基线问题，不要误认为是本节点失败：

1. `test_non_replayable_snapshot_is_recorded_without_model_eligibility` 依赖当前测试目录的 Git 工作区状态；
2. `test_commit_dream_changes_commits_real_edits` 受 nested Git workspace 行为影响；
3. `test_webui_dev_monitor_fails_when_sidecar_exits` 缺少 async pytest 插件；
4. `test_webui_dev_monitor_ignores_an_expected_gateway_shutdown` 缺少 async pytest 插件；

另有 `nanobot/tests/cli/test_commands.py:516` 因当前 Windows 环境没有 termios 而 skip。

## 5. Review 状态

- Spec 子代理已对 `3f607a1` 给出 PASS，确认 SI-05/SI-06 边界和专项验证。
- Standards 子代理先后发现并已修复：Case 失败卡死、失败预算清零、非 bug-fix baseline 错绑、legacy/new 路径锁窗口、候选 target 静态收窄问题。
- 最后一处静态检查问题已在 `dcf6ec2` 修复；明天继续工作前建议用同一 Standards 子代理对 `dcf6ec2` 做一次最终 delta recheck，并把正式 PASS 报告写入 `devpilot-agent/docs/reviews/`。

## 6. 明天的下一步顺序

1. 拉取本分支，确认 `git status` 干净，阅读本文档和 `docs/plans/CURRENT_PLAN.md`。
2. 对 `dcf6ec2` 做 Standards 最终 delta review；Spec 已通过，不要重复实现 SI-05/SI-06。
3. 写入 SI-05/SI-06 evidence 和两个 review 报告，形成最终验收记录。
4. 按冻结计划进入 SI-07：隔离 worktree 的 baseline/candidate Replay、三个受信 grader、ReplayTrial 和 EvaluationReport。
5. SI-07 完成后必须继续执行双轴 review；在用户明确确认前，候选仍不能自动激活。

## 7. 工作边界

本次没有上传本机配置、数据库、integrity key、API key、会话、日志、临时目录或真实项目数据。
不要使用 `git reset --hard` 覆盖另一台电脑上的本地改动；如果新电脑不是干净工作区，先保存并报告
本地改动，再决定合并方式。
