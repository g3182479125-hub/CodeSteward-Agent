# H9 跨电脑开发交接

日期：2026-09-14
仓库：`g3182479125-hub/devflow-agent`
远端：`origin` = `https://github.com/g3182479125-hub/devflow-agent.git`
当前分支：`handoff-2026-09-01`

## 交接结论

本次工作已把 H9 Live Harness Extension 的主要运行链路接入现有 H6/H8/H8.1 代码，但 H9 还没有通过最终产品验收，下一台电脑应继续开发和验证，不要把当前状态当作“已完成/可宣称全局最优”。

本次提交包含当前工作树中已有的 H8 交接相关改动，以及本轮 H9 实现和测试。工作树中两个临时目录未提交：`nanobot/.pytest-h8-current/`、`nanobot/MagicMock/`。

## 已完成的主要内容

### 持久化选择门

- 新增 `migrations/versions/0016_h9_harness_selection_records.py`，复用 DevPilot 数据库保存 candidate、evaluation、confirmation、evidence、task reference。
- `harness_selection.py` 提供插件/Loop 两类候选的 scope、baseline/candidate version、内容哈希、证据引用、round 和预算绑定。
- 每轮最多 2 个候选，只允许一个 plugin 或一个 Loop Driver target；评估预算上限 48 次，固定套件要求 24 次。
- 固定套件包含 target、holdout、regression 各 2 个 fixture，每个 baseline/candidate 各重复 2 次。
- 评估记录保留固定 case identity、contract/safety 结果、失败状态、调用次数和真实可得的 token 用量；不把原始工具输出写入 H9 持久记录，成本未知时保持 `null`。
- malformed runner result、fixture/contract/safety 结果不完整和基础设施异常会 fail-closed，并留下 `infrastructure_failure` receipt，不伪装成业务失败。
- confirmation 绑定 candidate/evaluation/scope/hash，默认 15 分钟过期；candidate retirement 会使未消费 confirmation 失效。
- confirmation 消费采用 SQLite writer lock；native promotion 失败可以回滚未产生 task reference 的 durable claim，避免“数据库 active 但项目文件未落地”。
- 已收紧持久化 JSON 的类型与 HMAC 校验，损坏或篡改记录不会被静默转换。

### Plugin H9 评估入口

- `ModelPluginController` 增加 `evaluate` action。
- 入口要求授权、显式 `max_candidates`/`max_evaluations`/`max_tokens`、baseline candidate 与固定输入规则一致，并执行 24 次 live runner evaluation。
- 固定 case 的业务成功/安全判定由 host 注入的 `harness_case_oracle` 提供；没有 oracle 时明确返回 `HARNESS_EVALUATOR_UNAVAILABLE`，不自行编造通过结果。
- 合格结果会写入 H9 selection store，并标记现有 model-candidate manager candidate 为 tested，绑定 evaluation/result/input/contract hash。

### Remote Loop Driver H9 评估入口

- `define_loop_candidate` 通过已有 FC-only catalog 创建不可变远端策略候选；`trial_loop_candidate` 复用 H8 trial gate。
- `evaluate_loop` 使用相同 H9 fixed projection 和 24 次 runner evaluation，要求 baseline/candidate 均已通过 session trial。
- 合格的远端策略可以经 native confirmation 以 metadata-only 方式复制到 project scope；新 session 可通过 project artifact 恢复，不依赖旧 session 目录。
- AgentLoop 会读取 exact project/workspace scope 的 active Loop candidate，默认候选与自定义 remote 策略均走 H9 selection binding。

### AgentLoop / evidence / native command

- `runner.py` 为每次运行累积实际 model call、input token、output token 计数。
- `AgentLoop` 在 task boundary 记录一条有界 task-result evidence，工具 evidence 只保存摘要哈希和字节数，不持久化原始工具输出。
- native `confirm` 先消费 durable confirmation，再执行 plugin/project artifact promotion；promotion 失败会尝试恢复 confirmation claim。

## 已验证的命令

在仓库根目录下，使用 PowerShell：

```powershell
cd devpilot-agent
$env:PYTHONPATH='src;../nanobot'
python -m pytest tests/test_h8_remote_strategy_catalog.py tests/test_h6_model_plugins.py tests/test_h9_harness_selection.py -q
python -m ruff check src/devpilot/harness_selection.py src/devpilot/model_plugins.py src/devpilot/remote_loop_strategy.py tests/test_h9_harness_selection.py tests/test_h6_model_plugins.py tests/test_h8_remote_strategy_catalog.py

cd ../nanobot
$env:PYTHONPATH='../devpilot-agent/src;.'
python -m pytest tests/agent/test_loop_handoff.py tests/command/test_plugin_commands.py -q
```

最近一次结果：DevPilot 相关测试通过；nanobot 相关回归 `40 passed`；上述 Ruff 通过。

Pyright 曾执行过诊断，但当前配置下 DevPilot 侧缺少 nanobot import path，且 nanobot 侧存在既有 protocol/dynamic typing 报告；不能把它描述为全仓库类型检查通过。后续应在正确的多包配置下区分新增问题和既有问题。

## 未完成项与风险

1. **最终 H9 验收尚未完成。** 初始 Standards/Spec 审查指出：生产侧仍缺少默认的固定 fixture oracle、最终 candidate → fixed eval → compare → retain/reject 的端到端验收证据；需要在本轮增量完成后让同一批审查员复核，并单独保存 H9 review report。
2. **Host oracle 是刻意留出的边界。** 当前 AgentLoop 只绑定 selection store，没有默认业务 oracle，因此生产调用 `evaluate`/`evaluate_loop` 会安全地报告 evaluator unavailable。下一步需要在明确 fixture 语义和授权边界后接入真实 host oracle，不能通过通用启发式替代。
3. **仍需跑更宽的全量回归。** 目前只跑了 H8/H9 直接相关的跨包测试；提交后应在另一台电脑跑完整 DevPilot 与 nanobot 测试集，并执行 migration/fixture 相关测试。
4. **Pyright 需要重新配置后处理。** 先为两个包提供可解析的 import roots，再针对本轮文件修复新增类型错误；不要用当前跨包缺失依赖的输出直接判定实现失败。
5. **当前工作树有历史批量改动。** `git diff --stat` 较大，包含此前 H8 ownership/recovery 的实现和测试。不要使用 `git reset --hard` 或按文件回滚；如需拆分提交，先以本次提交为安全锚点再做可恢复的整理。

## 下一台电脑的建议顺序

```powershell
git fetch origin
git switch handoff-2026-09-01
git pull --ff-only origin handoff-2026-09-01

cd devpilot-agent
python -m alembic upgrade head
```

然后按下面顺序继续：

1. 阅读本文件、`docs/plans/LIVE_HARNESS_EXTENSION.md`、`docs/evidence/live-harness/h8-loop-handoff.md` 和最新 H8 review。
2. 检查 `harness_selection.py` 的严格解码、confirmation 原子性和 project scope 恢复测试。
3. 设计并注入真实的 6 fixture host oracle，补 plugin 与 remote Loop 的端到端测试；测试必须验证 24 次运行、失败保留、真实 usage、无副作用和权限违规即拒绝。
4. 补一轮同审查员 Standards/Spec review，修复后保存 `docs/reviews/h9-standards-review-2026-09-14.md` 和 `docs/reviews/h9-spec-review-2026-09-14.md`，记录 baseline、candidate、发现、处理和残余风险。
5. 跑完整测试集，确认 migration、native confirm、旧 H8 session 兼容路径均未回归。
6. 只有在验收证据齐全、holdout/regression 无下降、没有副作用/权限违规且跨 session confirmation 经过一次 native chat confirmation 后，才将 H9 标为完成。
## 关键文件

- `devpilot-agent/src/devpilot/harness_selection.py`
- `devpilot-agent/src/devpilot/model_plugins.py`
- `devpilot-agent/src/devpilot/remote_loop_strategy.py`
- `devpilot-agent/migrations/versions/0016_h9_harness_selection_records.py`
- `nanobot/nanobot/agent/loop.py`
- `nanobot/nanobot/agent/runner.py`
- `nanobot/nanobot/command/plugin_commands.py`
- `devpilot-agent/tests/test_h9_harness_selection.py`
- `devpilot-agent/tests/test_h6_model_plugins.py`
- `devpilot-agent/tests/test_h8_remote_strategy_catalog.py`
- `nanobot/tests/agent/test_loop_handoff.py`
- `nanobot/tests/command/test_plugin_commands.py`
