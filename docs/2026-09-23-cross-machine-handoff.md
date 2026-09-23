# DevFlow Agent 跨电脑交接文档

更新时间：2026-09-24
当前分支：`codex/h11-complete-handoff`  
实现基线提交：`48e7647`（`fix: gate full access changesets behind confirmation`）
持久化环境边界修复提交：`eac3973`（`fix: fail closed on stale FC prepared environments`）。
本轮故障恢复提交：`5c186c1`（`fix: recover bounded FC provider faults`）。
本轮本地优先实现提交：`b20c01c`（`feat: default project verification to local execution`）。

## 一、项目定位

仓库地址：<https://github.com/g3182479125-hub/devflow-agent.git>

- `nanobot/`：主 Agent、Runtime、工具注册和 WebUI。
- `devpilot-agent/`：DevPilot Control Plane、项目分析、测试执行、ChangeSet、Apply、Verify、Rollback 和 FC 沙箱适配。
- `docs/superpowers/specs/2026-09-22-codex-like-execution-integration-design.md`：设计规格。
- `docs/superpowers/plans/2026-09-22-codex-like-execution-integration-plan.md`：冻结实施计划和当前进度。

本仓库不包含 API key、模型配置、数据库、虚拟环境、运行日志或临时工作区。

## 二、当前已经完成

1. Full Access 下，用户当前 Run 指定的项目可以自动接入，但仍受 `AllowedRoots`、FC 项目白名单和路径边界约束。
2. 项目测试使用服务器固定 Profile，不接受模型传入任意安装命令或执行后端。
3. 支持 Python pytest、嵌套测试目录、requirements/pyproject 依赖识别和 Node lockfile 识别。
4. 测试回执包含 Run ID、Sandbox ID、退出码、执行阶段、错误类别、工作目录、依赖 Profile 和依赖指纹。
5. Full Access 项目绑定只存在于单次测试 Run，Run 结束后清理内存授权。
6. 代码修改必须经过：ChangeSet/Diff → 用户确认 → Apply → Verify；已移除无确认的 Full Access 直通 Apply/Verify 接口。
7. 真实隔离缺陷项目已完成三处业务修复：折扣、数量计算、退款金额。
8. 原始缺陷 clone 保持不变，修复只在隔离副本中进行。
9. 项目测试 Run 现在记录请求、准备开始、准备成功/失败和依赖指纹；依赖准备失败不会创建代码 Failure Run。
10. 对 `SANDBOX_UNAVAILABLE`、`SANDBOX_TIMEOUT`、`CONTROL_PLANE_TRANSIENT`、`ORPHAN_RUN_RECOVERABLE` 提供服务器受控恢复，最多两次，并把恢复 Run 链接回原 Run；测试断言失败、未知回执和依赖失败不自动重放。
11. nanobot 项目测试适配器会自动消费受控恢复结果；P7 评测忽略测试生成的 `.pyc`，临时工具确认允许网关时间字段的微秒级序列化误差。
12. FC prepare 已独立于测试 Run：按项目/Profile/依赖指纹生成环境 ID，状态落盘到 `DEVPILOT_FC_STATE_DIR\prepared`，跨 Run 和进程重连复用；TTL、指纹变化、不可查询状态和显式清理均有边界处理。
13. 最终增量修复了三个安全边界：TTL 过期不会静默换新环境；旧环境清理失败会阻止新环境创建；复用前会校验项目、Profile、工作目录和依赖指纹。另修复了 Python 3.11 下 `verification.py` 的 Ruff 语法兼容问题。
14. FC 超时和 provider 断连现在映射为稳定、可恢复的错误；项目测试恢复只对这两类瞬时故障开放有限重试，断言失败、依赖失败和未知回执不会重放。
15. 2026-09-23 本地优先改造已完成本机实现、验证与双审：项目测试默认选择本机快照执行；FC Sandbox 默认关闭，只有 `-EnableFCSandbox` / `DEVPILOT_ENABLE_FC_SANDBOX=1` 显式开启才会创建远端 Sandbox。没有本地失败后自动切回 FC 的逻辑。ChangeSet、Apply、Verify、Rollback 与用户确认逻辑未放宽。

## 三、验证证据

本地组合回归命令：

```powershell
$env:PYTHONPATH='E:\codex-local\nanobot-devpilot-agent\devpilot-agent\src;E:\codex-local\nanobot-devpilot-agent\nanobot'
& E:\codex-local\nanobot-devpilot-agent\nanobot\.venv\Scripts\python.exe -m pytest `
  devpilot-agent/tests/test_p2b_control_plane.py `
  devpilot-agent/tests/test_agent_test_runs.py `
  devpilot-agent/tests/test_p3_execution_boundary.py `
  devpilot-agent/tests/test_p4_bug_fix.py `
  devpilot-agent/tests/test_p4_feature_delivery.py `
  nanobot/tests/channels/test_websocket_devpilot_confirmation.py -q
```

结果：退出码 `0`，全部通过。

本地优先改造后的 DevPilot 全量测试（使用仓库配套 nanobot 环境，避免误用另一套全局 Python）：

- 命令：`$env:PYTHONPATH="$PWD\devpilot-agent\src;$PWD\nanobot"; & .\nanobot\.venv\Scripts\python.exe -m pytest -q devpilot-agent/tests`
- 最新结果：退出码 `0`，全量通过；5 个 skip：4 个 Windows 符号链接用例因当前进程无创建 symlink 权限，1 个真实 FC 用例因要求显式开启 live-test 和 sandbox opt-in。
- Ruff：`devpilot-agent` 全量 `All checks passed!`
- Pyright：新增/核心本地执行模块定向检查通过；不代表整个历史仓库通过全量 Pyright。
- 集成启动脚本 Pester：4/4 通过，覆盖默认关闭与显式启用开关。
- `git diff --check`：通过
- 最终实现差异的 Standards 与 Spec subagent review：均 PASS。
- 本地优先实现与当前交接记录已提交在 `b20c01c`；切换电脑后先确认 `git status --short`，不要覆盖或整体回退任何后续本机改动。
- ShopSphere 本地执行器集成试跑：Run `e896cf25-6fb1-46a9-b018-4419c1011726`；本地依赖准备完成，`execution_location=local` 且无 Sandbox ID，但最终 Run 是 `unknown / TEST_RECEIPT_INCOMPLETE`，没有可验收退出码。最可能原因是 `python -m unittest discover -v` 输出超过 16 KiB 回执限制并被截断；须修复/验证后再宣称项目级 E2E 通过。
- 当前已运行的 `127.0.0.1:18800` Control Plane 仍报告只有 `fc-agent-sandbox` 且状态 degraded，说明服务进程尚未加载本轮 local-first 改动。挑战试跑通过当前源码的隔离本地 `ProjectTestRunner` 执行，没有调用 FC。完整 WebUI/Personal Agent 测试前需按新电脑本地路径重启/启动集成服务并确认后端目录列出 `local`。

最新真实 WebUI + Full Access + FC 沙箱测试：

- 命令：`python -m pytest -q`
- 结果：`10 passed, 0 failed`
- 退出码：`0`
- Run ID：`1eb09704-f477-4e08-9621-6d0fd3a43705`
- Sandbox ID：`sbx-8a9c5456-ebbc-49c1-bbfe-d3b630ab372b`
- 后端：`fc-agent-sandbox`

## 四、另一台 Windows 电脑的恢复步骤

### 1. 拉取代码

```powershell
git clone -b codex/h11-complete-handoff https://github.com/g3182479125-hub/devflow-agent.git
Set-Location .\devflow-agent
git log -1 --oneline
```

应看到交接提交对应的最新 SHA；不要假设还是 `48e7647`，先执行 `git log -1 --oneline`。

### 2. 安装运行环境

要求：Windows、PowerShell、Python、Node/npm、`uv`，以及可用的模型 API 配置。

```powershell
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
Set-Location .\nanobot\webui
npm install
npm run build
Set-Location ..\..
```

### 3. 配置新电脑自己的密钥和路径

不要复制旧电脑的数据库、会话、临时工作区或密钥。新电脑需要自己配置：

- nanobot 的模型 API key、base URL、模型名；
- `DEVPILOT_HARNESS_SELECTION_INTEGRITY_KEY`；
- `DEVPILOT_CANDIDATE_GRANT_SECRET`（仅在启用模型候选 / FC 功能时）；
- 新电脑自己的 `DEVPILOT_ALLOWED_ROOTS`。

默认本地执行不需要 `E2B_API_KEY`、`E2B_API_URL`、`E2B_DOMAIN` 或
`DEVPILOT_FC_ALLOWED_PROJECTS`。如果将来明确启用 FC，再按本机账号配置这些值，
并在启动时加 `-EnableFCSandbox`。

这些值只放在本机环境变量或本机配置文件，不提交 GitHub。

### 4. 启动集成 Harness

将 `StateRoot` 和 `AllowedRoots` 换成新电脑的实际目录：

```powershell
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\start-integrated-harness.ps1 `
  -StateRoot C:\devflow-state `
  -AllowedRoots C:\devflow-projects
```

启动后：

- WebUI：`http://127.0.0.1:8765/`
- Gateway health：`http://127.0.0.1:18790/health`
- Control Plane health：`http://127.0.0.1:18800/healthz`

首次启动应执行数据库迁移；只有确认数据库已经是最新版本时才使用 `-SkipMigration`。

## 五、换机后的第一轮检查

1. 先检查三个 health endpoint。
2. 在 WebUI 新建对话，确认权限显示为 Full Access。
3. 用一个位于 `AllowedRoots` 内的项目执行只读检查和项目测试。
4. 默认应看到回执标记 `execution_location=local`、无 `sandbox_id`、真实退出码和通过/失败数量。
5. 做代码修改时，确认 Agent 先展示 ChangeSet/Diff，并等待用户明确确认；没有确认不能写文件。
6. 通过确认后再检查 Apply、Verify 和必要时 Rollback 的真实结果。

## 六、本次交接后的下一步

本轮“本地优先、FC 显式 opt-in”代码、全量 DevPilot 测试及双审均已完成；但 ShopSphere 真实项目通过 ProjectTestRunner 的端到端回执仍未验收。重要边界：本地项目测试不是 OS 沙箱，测试/依赖代码会以当前 Windows 用户权限运行并可使用主机默认网络；不要对不可信项目执行本地测试。

此前这批代码已经完成“准备/测试结果分离”和“瞬时错误有限恢复”的最小闭环。下一台电脑建议按下面顺序继续：

1. 先修复/确认 `project-unittest` 的输出回执问题。首选检查不带 `-v` 的固定 unittest 命令并同步更新 LocalExecutionBackend profile 验证和回归测试；仍须保持输出上限与“不接受截断回执”的契约。再用 ShopSphere 挑战副本重跑，要求 Run 有真实退出码、完整回执、`execution_location=local`、无 `sandbox_id` 且清理成功。
2. 在另一台 Windows 电脑按第四节安装后，先运行第三节 DevPilot 全量测试，确认使用 `nanobot\\.venv`，不要误用其他全局 Python。
3. 启动/重启集成服务后先检查 `http://127.0.0.1:18800/v1/security/backends`：FC 默认关闭时应显示 local 执行后端，不应只显示 `fc-agent-sandbox`。然后从 WebUI/Personal Agent 对 ShopSphere 运行只读分析和 unittest，确认沿用目标项目作用域。
4. 默认启动应完全本地运行项目测试：确认回执 `execution_location=local` 且没有 `sandbox_id`。不需要 E2B 凭据。
5. 若以后明确要验 FC Sandbox，在 `start-integrated-harness.ps1` 使用 `-EnableFCSandbox`，按本机 FC 账号配置凭据和白名单，再验证 FC 的真实回执、TTL 和清理行为。FC 真实 provider 故障演练仍需目标账号提供可控条件。
6. 模型候选试验和临时工具候选评测若依赖 FC，FC 关闭时会保持不可用；不会偷偷在本机或云端自动启用。

## 七、已知未完成项

以下是独立的云端验收/运行事项，不影响本轮“默认本地执行、FC 可显式开启”的实现完成状态：

- 目标 FC 账号上的真实 provider 断线/超时注入仍未执行；本地适配器和控制面的可重复故障注入、重启恢复及不重放测试已完成，真实云端演练需要可控的 provider 故障条件；
- FC provider 对长期 Sandbox 保活的实际成本与配额监控仍需在目标账号确认；代码有 TTL 和显式 cleanup，但不会替代 provider 侧账单/配额告警；
- 如果目标账号不允许可控故障演练，应记录限制及替代证据边界，不将模拟测试描述成云端验收。

开发顺序建议：先在目标账号上补 provider 断线/超时注入验收，并检查真实 FC prepare 环境 TTL/清理；如目标账号不支持可控故障演练，应记录限制及替代证据边界，不将模拟测试描述成云端验收。如目标账号不允许长时间保活，则把 TTL/显式 cleanup 作为运行边界，不绕过安全门禁。

## 七、重要路径

- 集成启动脚本：`devpilot-agent/scripts/start-integrated-harness.ps1`
- 安装脚本：`devpilot-agent/scripts/install-integrated.ps1`
- 控制平面：`devpilot-agent/src/devpilot/control_plane.py`
- 项目测试：`devpilot-agent/src/devpilot/test_runs.py`
- Agent 工具：`devpilot-agent/src/devpilot/nanobot_tools.py`
- 项目测试恢复接口：`POST /v1/project-tests/{run_id}/recovery`
- 冻结计划：`docs/superpowers/plans/2026-09-22-codex-like-execution-integration-plan.md`
