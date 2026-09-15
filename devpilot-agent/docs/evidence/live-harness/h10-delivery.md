# H10：真实链路、恢复与交付证据

日期：2026-09-14
验证仓库：`E:\codex-local\nanobot-devpilot-agent`（历史现场路径；复跑时以当前仓库根目录为准）
验证原则：现场结果、历史证据和暂未具备的条件分别记录；不会把演示目标或旧测试计数当成
本轮生产结果。

## 本轮现场运行事实

- DevPilot 控制面健康检查：`http://127.0.0.1:18800/healthz` 返回 HTTP 200。
- nanobot Gateway 健康检查：`http://127.0.0.1:18790/health` 返回 HTTP 200；WebUI 和
  Gateway 在本机端口 `8765`、`18790` 运行。PID 是启动实例属性，不作为跨次复现的身份断言。
- 已以 nanobot 原生 WebUI 的 bootstrap token 建立真实 WebSocket 会话；不是测试替身。
  新会话执行 `/task-profile feature-delivery` 后，服务端返回的可见工具中包含
  `devpilot_model_plugin`，连同 `devpilot_*` 提案、状态、确认工具一起受 Profile 约束。
- 同时建立两个独立 WebUI 会话并发选择 `feature-delivery` 与 `bug-fix` Profile，两个
  `turn_end` 均成功返回（2/2）；端到端命令回合分别为 179.4ms、269.3ms。期间 Gateway PID
  未改变，证明这类会话级配置不需要重启整个 Agent。
- H8 命令在没有 Host owner 的新会话中拒绝切换；建立普通任务 owner 后，
  `/plugins switch-loop plan-then-act` 返回 `pending at the next safe Host boundary` 及操作 ID。
  这证明它不会在无记录或未结算任务中强行替换 Loop。
- Codex 内置浏览器已打开 `http://127.0.0.1:8765/`，用于录屏前的原生 WebUI 入口复核。

这次运行还发现并修复了真实交付缺口：已有数据库若未升级到 `0016_h9_harness_selection_records`
，运行时会按 fail-closed 原则禁用 H9。现在启动脚本会在启动前执行 `alembic upgrade head`；
只有迁移成功后才启动带 H6/H8/H9 开关的 nanobot。

### H10 收尾网页回归（本轮新增）

- 先前的真实网页问题已精确复现：先发送 `/task-profile feature-delivery`，再请求模型调用
  `devpilot_model_plugin`，旧实现会在约一秒内结束并返回通用 fallback，且没有模型或工具事件。
- 根因是 H8 checkpoint 使用原始会话消息计数，而 `AgentRunner` 使用过滤掉命令历史后的 LLM
  消息计数；命令回合会让首个模型 Step 被误判为 stale。修复后 Host 显式将同一原始会话位置
  传给 Runner 的 Driver 输入，checkpoint 的 CAS/旧执行器拒绝语义不变。
- 重启到当前源码后，真实 WebSocket chat `b607e77e-b1d9-492d-a208-5d84da7c7696` 完成
  `/task-profile feature-delivery` → `devpilot_model_plugin(action=inspect)`；日志记录实际 Tool
  call，客户端收到了 `reasoning_delta`、`delta`、`stream_end` 与 `turn_end`，未出现 fallback。
- 独立新 chat 对 `/plugins switch-loop plan-then-act` 在尚未建立 Host owner 时返回
  `loop handoff record does not exist`。这是 H8 的预期 fail-closed 行为，不会在无 checkpoint
  的会话中强行换 Loop；建立 owner 后才可在安全边界排队切换。

### H10 本轮本地确定性协议闭环（不计作真实 FC 证据）

为先验证 WebUI、Gateway、确认上下文、控制面和回滚协议，本轮在独立临时目录运行了一个
确定性的本地 fake provider 与 `FakeExecutionBackend`。它没有调用真实 FC，也没有使用用户项目、
用户数据库或用户配置；以下事实只证明本地协议闭环，不替代 H10-A 的真实 FC 验收。

- WebUI chat：`4d763777-79b2-43de-b179-95feb8762ad8`。
- 受控演示文件：隔离 workspace 下的 `h10-live-demo/main.py`。
- ChangeSet：`134ca8fc-0211-482c-8e84-f2cd6c25cc0c`；Run：
  `69a42b14-01fe-4ecd-b8df-9e6f9e45569e`。
- 同一 WebUI 会话依次完成：`inspect → search/read → feature-delivery proposal`，单独输入
  `执行` 后 `devpilot_confirmed_action(action=apply_verify)` 返回 `application=applied`、
  `verification=verified`，随后取得 completed delivery report；单独输入 `撤销` 后只调用
  `devpilot_confirmed_action(action=rollback)`，返回 `reverted`，第二次 `read_code` 读回基线。
- 变更前哈希为 `85917786…3a93ade54e`，Apply 后为 `4de3fd96…b02bcc05`，Rollback 后
  恢复为 `85917786…3a93ade54e`；刷新 WebUI 后仍可看到提案、执行、回滚三组工具轨迹和最终
  恢复消息。
- 本地后端回执明确为 `backend=fake`、`sandbox_id=fake-sandbox`，没有真实地域、模板或
  provider-default FC 策略，因此不得将本记录写成云端验证成功。

### H10-A 本轮真实 FC 全链路（2026-09-15）

本节是本轮使用既有本机 FC 配置得到的真实证据；配置中的密钥、bootstrap token、完整
模型响应和完整日志均不进入仓库。运行态使用独立的控制面数据库、WebUI 工作区和隔离
演示项目，不触碰用户真实项目、数据库或聊天记录。

- 原生 nanobot WebUI chat：`be3cfa0e-039f-4966-b5c8-c2715542162a`；Profile：
  `feature-delivery`；实际模型：`deepseek-chat`。12:35 完成 `inspect → search/read →
  proposal`，提案阶段项目文件哈希仍为 `85917786c8f56a32aba9c5b005cecdee4ddab184d0995f16b0315f3a93ade54e`，未发生写入。
- 隔离项目：`h10-live-demo`，项目 ID：`dc2629d1-e9ff-466c-ba68-efa96fc1e12b`；
  ChangeSet：`bf33c510-de9f-47ac-93cf-194669c42382`；Run：
  `9b9b6e19-78a5-49f9-b3f2-24fcd28bb053`。变更仅为 `main.py` 一行：
  `Decimal("10") → Decimal("100")`，`verification_profile=python-syntax`。
- 12:36 单独输入“执行”后，真实 `devpilot_confirmed_action(action=apply_verify)` 返回
  `application=applied`、`verification=verified`、`run_status=completed`。FC 回执：
  `backend=fc-agent-sandbox`、sandbox `sbx-f84fefbd-c3df-4912-b6c2-9a740e0402c3`、
  `template=code-interpreter-v1`、`region=cn-beijing`、`exit_code=0`、`duration_ms=1417`、
  `bundle_bytes=229`、`network_policy=provider-default`。
- 12:36 单独输入“撤销”后，模型只登记 `r1.rollback` 审批意图，返回
  `approval_pending` 且没有 token；这证明模型/Skill 不能签发或传播控制面 capability token。
  12:39:10（Asia/Shanghai）由控制面用户动作签发 `r1.rollback` 审批并调用 rollback，返回
  `status=reverted`。没有重新 Apply，也没有本机 Shell fallback。
- 回滚后刷新 WebUI，并在 12:41 通过同一真实会话只读调用 `devpilot_read_code`：
  `main.py` 当前内容恢复为 `Decimal("10")`，当前 SHA-256 为
  `85917786c8f56a32aba9c5b005cecdee4ddab184d0995f16b0315f3a93ade54e`，与
  `rollback_manifest` 和提案 `before_manifest` 完全一致。独立 `Get-FileHash` 复核得到同一值。
- 控制面 `delivery-report` 的 `status=reverted`，但其 `after_manifest` 持久字段仍描述
  “应用后版本”；它不是回滚后磁盘哈希。本轮将回滚后的实际文件哈希作为独立只读证据记录，
  且明确本次回滚没有重新运行验证。`provider-default` 不被表述为网络隔离保证。

## 可审计的网页事件引用

以下均是本机原生 WebUI 追加式事件文件；它们不进入 Git，换机时可按本文件的命令重新生成。

| 范围 | 记录类型 | 位置与事实 |
| --- | --- | --- |
| H2/H6 历史完整生命周期 | 历史记录，不冒充本轮 | `E:\codex-local\nanobot-state\webui\websocket_364ab9f0-5dc7-486a-80ef-f78efcec8f38.jsonl`。`demo_greeting_v1@2.0.0` 在 2026-09-12 的事件中已激活 generation 2，并以 operation `077d3587-b355-4b14-b114-a57501b094a3` stop；随后 operation `520c12e8-c830-4f71-9313-4935446df4f4` remove，结果均为 `ok`。该日志还保存 define/trial/activate 的对应 tool events。 |
| H6 迁移后装载 | 本轮现场 | chat `5b56a67e-6370-4055-a7fb-1b56755b98a1` 的 Profile 回复列出 `devpilot_model_plugin`。这表示最新 `0016` schema 与 H6 运行时已被同一 nanobot 进程加载，不等同于本轮重新试用候选。 |
| H8 安全切换 | 本轮现场 | `E:\codex-local\nanobot-state\webui\websocket_4310ca9d-6a9a-4869-a878-e9cadc891009.jsonl`。先拒绝无 owner 的切换；建立 owner 后 operation `5c498e70-90db-4b92-a084-6a015620126d` 于事件时间 `1789396614785` 进入 `pending`，旧任务没有被重启。 |
| 并发会话 | 本轮现场 | chat `5b56a67e-6370-4055-a7fb-1b56755b98a1`（feature-delivery）及另一独立 bootstrap 会话（bug-fix）均收到 `turn_end`；同一时刻 Gateway PID 为 `17288`。 |

查看某个本机会话的脱敏事件（不要复制其中的模型内容或任何凭据到公开仓库）：

```powershell
Get-Content E:\codex-local\nanobot-state\webui\websocket_<chat-id>.jsonl
```

## 管理操作与回归测量

| 项目 | 本轮实测 | 解释 |
| --- | --- | --- |
| 50 次装载→停止 | P50 1.465ms；P95 2.059ms；范围 1.144–2.153ms | 同一 nanobot 进程、`devflow-greeting` 只读示例、无运行中 lease；计时包含实际 route 装载、停止和等待终态。 |
| 装卸残留 | active routes 0；retired routes 0；drain tasks 0 | 完成第 50 次停止并等待 `inactive` 后读取管理器状态。 |
| 并发会话完成率 | 2/2（100%） | 仅为两会话现场样本，不外推为生产 SLA。 |
| H9 固定对照 | 24 个宿主拥有的比较用例 | 目标、输入、预期输出和通过条件均由宿主固定；候选无法改写。对应 H9 测试本轮通过。 |
| H6/H8/H9/H10 聚焦回归 | 94 passed，16.4s | 包含候选、选择 oracle、Loop handoff、插件命令、迁移及命令历史回归。 |
| 前端质量 | `npm run lint`、`npm run build` 通过 | build 耗时 10.99s；本轮针对 nanobot 原生 WebUI，不会启动已移除的独立控制台。 |

这些数值是本机开发机测量，不能视为云端吞吐量、模型响应延迟或生产可靠性承诺。

可复跑 50 次测量（不读写用户数据库、聊天或项目文件）：

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
$env:PYTHONPATH="$repoRoot\nanobot;$repoRoot\devpilot-agent\src"
& "$repoRoot\nanobot\.venv\Scripts\python.exe" `
  "$repoRoot\devpilot-agent\scripts\h10_lifecycle_benchmark.py"
```

H9 的 24 个固定用例来自 `test_h9_harness_selection.py` 中的宿主固定 runner 场景；本轮运行了
预植错误修复、基础设施失败及畸形宿主结果三个测试。H6/H8/H9 聚焦回归的完整命令见本文件末尾。

## 隔离恢复演练

正式数据与聊天未被复制或改写。历史现场新建的证据目录为
`E:\codex-local\nanobot-state\h10-recovery-20260914`；复跑时应在当前电脑的临时状态根目录
新建隔离目录，独立数据库从空库迁移到
`0016_h9_harness_selection_records (head)`。随后在独立 pytest 基目录运行下列恢复分支：

- 不兼容持久 Driver 恢复为 `default-react`，保留 checkpoint；
- 会话快照缺失时暂停，而不是猜测重放；
- 结果已持久化但 handoff checkpoint 未提交时暂停，而不是重复执行。

结果：3 passed，30 deselected。该演练验证了缺工件/不兼容状态的 fail-closed 恢复语义；
它不是对现有用户数据库做破坏性故障注入。

可在其他电脑或本机再次演练（只会新建传入的 E 盘目录，绝不覆盖正式状态）：

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
$recoveryRoot = Join-Path (Split-Path $repoRoot -Parent) 'nanobot-state\h10-recovery-<date>'
New-Item -ItemType Directory -Path $recoveryRoot -ErrorAction Stop
$env:DEVPILOT_DATABASE_URL = "sqlite:///" + (($recoveryRoot -replace '\\','/') + '/devpilot.db')
$env:DEVPILOT_DATA_DIR = $recoveryRoot
Set-Location "$repoRoot\devpilot-agent"
& .\.venv\Scripts\alembic.exe upgrade head
$env:PYTHONPATH="$repoRoot\nanobot;$repoRoot\devpilot-agent\src"
& "$repoRoot\nanobot\.venv\Scripts\python.exe" -m pytest `
  "$repoRoot\nanobot\tests\agent\test_loop_handoff.py" -q `
  -k 'restores_default_when_persisted_driver_is_incompatible or durable_unsettled_checkpoint_pauses_when_session_snapshot_is_missing or result_saved_before_handoff_checkpoint_pauses_instead_of_replaying'
```

预期迁移版本为 `0016_h9_harness_selection_records (head)`，三个恢复断言均通过。证据目录可在
确认不再需要后人工删除；不要删除 `E:\codex-local\nanobot-state\devpilot-data`。

## H10 用户操作指南

### 启动与最短自检

在当前仓库根目录执行（不要假设固定盘符）：

> 注意：`start-integrated-harness.ps1` 的 `StateRoot`/`AllowedRoots` 仍保留 E: 盘安全门禁；
> 新电脑没有 E: 时不要绕过该门禁，应先按计划调整并重新做双轴复核。下列仓库内测试命令
> 可以使用任意仓库盘符。

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
Set-Location $repoRoot
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\start-integrated-harness.ps1
```

浏览器打开 `http://127.0.0.1:8765`，新建话题，先输入：

```text
/task-profile feature-delivery
```

回复的 `Visible tools` 中出现 `devpilot_model_plugin` 才表示 H6/H8/H9 处于运行状态。
若出现数据库 schema 提示，停止 nanobot 后重新运行上述脚本，不要删除现有数据库。

已有仓库时原地同步，先检查本地改动；有改动则先自行提交或备份，绝不执行 reset/覆盖：

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
Set-Location $repoRoot
git status --short
git pull --ff-only origin handoff-2026-09-01
```

### 候选工具和 Loop 的生命周期

在已选中的 `feature-delivery` 或 `bug-fix` 话题中：

1. 让模型通过 `devpilot_model_plugin` **define** 候选；它只能是受限的 FC proxy，不能取得
   Shell、网络、审批或文件写入权限。
2. 模型提出试用时，用户单独发送“确认试用”；Host 使用固定 fixture 试用，未确认不会执行。
3. 确认通过后，模型提出激活时，用户单独发送“确认启用”。激活/更新只改变当前作用域的
   动态路由；下一轮使用新版本，不重启 nanobot。
4. `stop` 停用当前作用域版本；`remove` 删除候选注册但保留工件以便审计；`revoke` 取消
   当前会话的候选授权。操作状态可用 `/plugins status <operation-id>` 查询。
5. `/plugins switch-loop default-react` 或 `plan-then-act` 只在 Host 安全边界提交；若工具调用
   尚未结算，系统显示等待而不会中断或重放。

等待不是卡死：它表示已有工具调用、FC 清理或所有权 checkpoint 尚未安全结算。先查询操作
状态和 Run 状态；不能以刷新网页代替恢复。WebUI 断线后可重新连接，Gateway 从持久快照恢复
状态，而长任务和动态路由仍在原进程内继续。

### 仍需发布重启的边界

传统 Python `nanobot.tools` entry point 的安装/卸载依赖 Python 包元数据发现，因此需要重启
nanobot；这不是 H2–H9 管理的候选工具。通过 `/plugins` 和 `devpilot_model_plugin` 管理的候选
工具、版本更新、stop/remove 与已注册 Loop 手动切换不需要重启。更新 nanobot/DevPilot 自身
代码、模型 Provider 配置或数据库 schema 时仍需受控重启。

## H10 验收状态

- **H10-A：完成。** 本轮真实 DeepSeek + 原生 WebSocket WebUI + 既有 FC 配置完成
  `项目提案 → 聊天确认 → apply_verify → FC python-syntax 验证 → 结果查询 →
  受保护撤销 → 刷新重连 → 回滚后只读哈希复核`；H2 的完整 lifecycle、H6 的
  `define→trial→activate→stop→remove` 现场记录和 H8 安全边界切换记录仍分别保留在
  本文前述历史现场引用中。本节没有把 fake 回执计入真实 FC 证据。
- **H10-B：完成。** 本轮聚焦回归、前端 lint 与生产 build 均已通过。
- **H10-C：完成。** 隔离数据库迁移及三类缺失/不兼容恢复分支已可复现。
- **H10-D：完成。** 本文已分别标注本轮真实实测、历史现场记录、自动化证据、本地
  fake 限制和 provider-default 边界；独立只读产品交付评审已核对真实控制面/WebUI/测试
  证据并通过，结论见 `docs/reviews/h10-product-review-2026-09-15.md`。原 Standards/Spec
  reviewer 线程虽显示完成，但本轮没有返回正文，因此未把空响应当作 PASS。

当前 FC `provider-default` 模式不提供“禁网且无副作用”的可信回执，因此 H9 会拒绝将这类
试用结果提升为持久选择。这是故意的 fail-closed 限制，不是本机回退执行；FC 控制台或服务端
日后提供受支持的安全回执后，才可进行 H9 真实晋升演示。

## 本轮回归命令

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
$env:PYTHONPATH="$repoRoot\nanobot;$repoRoot\devpilot-agent\src"
& "$repoRoot\nanobot\.venv\Scripts\python.exe" -m pytest `
  "$repoRoot\devpilot-agent\tests\test_p2a_migration.py" `
  "$repoRoot\devpilot-agent\tests\test_h6_model_plugins.py" `
  "$repoRoot\devpilot-agent\tests\test_h8_remote_strategy_catalog.py" `
  "$repoRoot\devpilot-agent\tests\test_h9_harness_selection.py" `
  "$repoRoot\devpilot-agent\tests\test_h9_harness_oracle.py" `
  "$repoRoot\nanobot\tests\agent\test_loop_handoff.py" `
  "$repoRoot\nanobot\tests\command\test_plugin_commands.py" -q
Set-Location "$repoRoot\nanobot\webui"
npm run lint
npm run build
```
