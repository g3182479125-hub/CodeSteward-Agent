# P8-03 定时 Skill 改进批处理验收记录

状态：实现候选 `87e61ed` 已通过 Standards / Spec 双轴 review；`e12eeb5` 为仅收纳证据文档的 docs-only 提交。
范围：只为已有 `bug-fix` Skill 读取已验证反馈并生成私有候选；不自动评测、不启用、不修改用户项目。

## 用户入口

P8-03 复用 nanobot 原生 Cron 和原生聊天，不增加管理页面或第二套调度器。功能默认关闭，
只有用户在原生聊天中明确执行 `schedule enable` 才会创建定时任务。

```text
/skill-improvement schedule status
/skill-improvement schedule enable every 86400 Asia/Shanghai calls=2 tokens=12000 timeout=600
/skill-improvement schedule enable cron "0 9 * * *" Asia/Shanghai calls=2 tokens=12000 timeout=600
/skill-improvement schedule run
/skill-improvement schedule disable
```

`every` 是从任务触发时开始计算的间隔，时区只作为提示；需要固定当地时间时使用带时区的
5 字段 `cron` 表达式。可配置项被限制在：每批 1～2 次候选生成尝试、512～20,000 个
估算 token、30～600 秒。调度变更、手动运行和停用都要求直接用户聊天回合。

## 执行边界

1. 批次绑定工作区、项目、用户、`bug-fix` Skill 和原生 Cron job，批次 ID 稳定生成，
   不能通过重新配置改变作用域。
2. 每次触发先用 SQLite/Alembic 持久记录的租约领取最多 20 条、且 Skill hash 匹配的
   `verified` 反馈；没有新证据时只写 `no_new_evidence`，不调用模型，也不发通知。
3. 有证据时只向 Agent 暴露 `devpilot_skill_candidate`，并注入批次、租约所有者和完整
   证据 ID。候选提示明确禁止编辑文件、执行命令、评测、启用、激活、Apply 和恢复。
   候选仅写入工作区外的私有存储，状态为 `pending_evaluation`。
4. 只有候选持久化成功，才推进证据游标；失败、取消、进程重启或租约失效都保留证据以供
   下一次重试。正在执行的批次由租约防止重复运行，已处理 evidence ID 防止重启重复处理。
5. 定时任务只通知新候选或需要处理的失败；同一失败且没有新证据时不重复通知。停用会清除
   活动租约并阻止新批次，取消不会激活候选。进程关闭、休眠或离线期间不保证执行。
6. P8-02 的比较、用户确认、启用和恢复仍是独立入口；定时任务不持有用户确认令牌，不能
   代替 Apply，也不能直接修改 `skills/bug-fix/SKILL.md` 或项目源代码。

## 本批次验收矩阵

| 检查项 | 证据 | 结果 |
|---|---|---|
| 默认关闭、显式启用、停用 | `tests/command/test_skill_command.py` | 通过；显式启用后复用 1 个原生 Cron job，停用会取消活动任务 |
| `every` / 时区 / `cron` 复用原生 Cron | `tests/command/test_skill_command.py`、`tests/cron/test_cron_service.py` | 通过；覆盖 30 秒最小间隔、Asia/Shanghai 和实际定时器回调 |
| 无新证据零模型调用 | `nanobot/tests/cli/test_commands.py` | 通过；真实 Gateway batch preflight 返回 `no_new_evidence`，spy agent 未被调用 |
| 租约、重复触发、重启去重 | `tests/test_p8_03_skill_improvement.py` | 通过；覆盖 busy、租约过期、候选恢复、孤儿候选恢复和进程重启后的 evidence 去重 |
| 调用与 token 上限、失败重试、取消 | `tests/test_p8_03_skill_improvement.py`、`nanobot/tests/cli/test_commands.py` | 通过；覆盖 2 次调用、12,000 token 分摊、累计重试预算、50 ms 验收硬超时和 AgentLoop 原生取消 |
| 受信证据/租约绑定候选工具 | `nanobot/tests/command/test_skill_command.py`、`nanobot/tests/cli/test_commands.py` | 通过；校验 job、workspace、scope、batch owner 和 evidence IDs 绑定 |
| 实际短间隔原生 Cron 触发 | `nanobot/tests/cli/test_commands.py`、`tests/cron/test_cron_service.py` | 通过；every 25 ms 任务实际触发 Gateway，走 WebSocketChannel 序列化发送路径 |
| 候选不自动激活、Dream 旁路拒绝 | P8-01/P8-02 原有回归测试 | 通过；P8-01/P8-02 受影响集合已重跑 |
| 手动 `schedule run` | `tests/command/test_skill_command.py` | 通过；命令实际调用原生 Cron `run_job(force=True)`，未引入第二套调度器 |

## 自动化结果

- DevPilot P5/P8 受影响集合：`50 passed`。
- nanobot 受影响集合：`207 passed, 1 skipped`。
- nanobot 全量回归：`5884 passed, 45 skipped`。
- DevPilot 与 nanobot Ruff：均通过。
- 本批次新增的 WebSocket 集成测试使用真实 `WebSocketChannel.send` 序列化路径；没有调用真实模型、FC 沙箱或公网服务。

## 换机与清理

定时任务保存在 nanobot 原生 Cron 数据中，换机不迁移旧的确认上下文、数据库或密钥。
新机器先安装 nanobot 与 DevPilot 集成依赖，再重新配置本地 `DEVPILOT_DATA_DIR` 和模型；
用户需要在新机器原生聊天中重新执行 `schedule enable`。验收用短间隔自有测试任务，确认
原生 Cron 已触发后立即删除该测试任务；不得把测试任务或真实私有数据提交 Git。
