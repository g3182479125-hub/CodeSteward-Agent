# DevFlow Agent 跨电脑交接文档

更新时间：2026-09-23  
当前分支：`codex/h11-complete-handoff`  
实现基线提交：`48e7647`（`fix: gate full access changesets behind confirmation`）
本次继续开发最终提交：`eac3973`（`fix: fail closed on stale FC prepared environments`）。
本轮故障恢复提交：`5c186c1`（`fix: recover bounded FC provider faults`）。

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

本次继续开发后的 DevPilot 全量测试（使用仓库配套 nanobot 环境，避免误用另一套全局 Python）：

- 命令：`$env:PYTHONPATH="$PWD\devpilot-agent\src;$PWD\nanobot"; & .\nanobot\.venv\Scripts\python.exe -m pytest -q devpilot-agent/tests`
- 结果：退出码 `0`（包含 3 个 skip）
- Ruff：受影响源码和测试文件 `All checks passed!`
- `git diff --check`：通过
- Standards 增量 review：PASS，报告见 `devpilot-agent/docs/reviews/2026-09-23-project-test-recovery-standards-review.md`
- Standards 最终故障恢复 review：PASS，报告见 `devpilot-agent/docs/reviews/2026-09-23-project-test-recovery-fault-recovery-standards-review.md`
- Spec 最终故障恢复 review：reviewer 再次返回空结果，按规则记录为 BLOCKED，见 `devpilot-agent/docs/reviews/2026-09-23-project-test-recovery-fault-recovery-spec-review-blocked.md`
- 故障注入定向测试：`61 passed`；全量测试：退出码 `0`，包含 3 个 skip。

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
- `DEVPILOT_CANDIDATE_GRANT_SECRET`；
- FC 沙箱所需的 `E2B_API_KEY`、`E2B_API_URL`、`E2B_DOMAIN`；
- 新电脑自己的 `DEVPILOT_ALLOWED_ROOTS` 和 `DEVPILOT_FC_ALLOWED_PROJECTS`。

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
3. 用一个位于 `AllowedRoots` 内的项目执行只读检查和 pytest。
4. 确认回执包含真实 `run_id`、`sandbox_id`、退出码和通过/失败数量。
5. 做代码修改时，确认 Agent 先展示 ChangeSet/Diff，并等待用户明确确认；没有确认不能写文件。
6. 通过确认后再检查 Apply、Verify 和必要时 Rollback 的真实结果。

## 六、本次交接后的下一步

当前这批代码已经完成“准备/测试结果分离”和“瞬时错误有限恢复”的最小闭环。下一台电脑建议按下面顺序继续：

1. 先按本文件第三节的命令运行 DevPilot 全量测试和组合回归，确认使用的是 `nanobot\.venv`，不要直接用可能指向旧项目的全局 `python`。
2. 使用真实 FC 项目跑一次：观察同一个 Run 的 `test.prepare.started` → `test.prepare.completed` → `test.execution.completed` 事件，以及真实 `sandbox_id`、退出码和依赖指纹。
3. 本地可重复的 FC 超时/断连注入已经完成，确认恢复结果、原 Run 关联和一次有限重试；换机后如有条件，再用目标 FC 账号做一次真实 provider 断线/超时演练。
4. 补齐 Spec reviewer 的有效 PASS/FAIL 报告；Standards 已 PASS，但 Spec 接口仍返回空消息，不能把它当作通过。
5. 检查真实 FC prepare 环境的 TTL 过期、依赖指纹变化和显式清理结果。

## 七、已知未完成项

以下内容仍按冻结计划继续开发，不能把当前提交当作整个计划全部完成：

- 目标 FC 账号上的真实 provider 断线/超时注入仍未执行；本地适配器和控制面的可重复故障注入已经完成，真实云端演练需要可控的 provider 故障条件；
- FC provider 对长期 Sandbox 保活的实际成本与配额监控仍需在目标账号确认；代码有 TTL 和显式 cleanup，但不会替代 provider 侧账单/配额告警；
- 本次候选的 Standards review 已 PASS；Spec review 仍未形成有效 PASS/FAIL 报告，当前仅记录空输出阻塞，不得视为通过；

开发顺序建议：先完成本轮候选的双轴 review 有效报告，再补 provider 断线/超时注入验收；如目标账号不允许长时间保活，则把 TTL/显式 cleanup 作为运行边界，不绕过安全门禁。

## 七、重要路径

- 集成启动脚本：`devpilot-agent/scripts/start-integrated-harness.ps1`
- 安装脚本：`devpilot-agent/scripts/install-integrated.ps1`
- 控制平面：`devpilot-agent/src/devpilot/control_plane.py`
- 项目测试：`devpilot-agent/src/devpilot/test_runs.py`
- Agent 工具：`devpilot-agent/src/devpilot/nanobot_tools.py`
- 项目测试恢复接口：`POST /v1/project-tests/{run_id}/recovery`
- 冻结计划：`docs/superpowers/plans/2026-09-22-codex-like-execution-integration-plan.md`
