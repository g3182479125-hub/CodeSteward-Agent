# DevFlow Agent 换机交接与录制准备（2026-09-09）

## 1. 当前仓库

- GitHub：`https://github.com/g3182479125-hub/devflow-agent`
- 分支：`main`
- 本次交接目标：把今天的响应契约修复、可插拔工具演示和录制说明一起提交。
- 不提交：API Key、`.env`、SQLite、日志、虚拟环境、`node_modules` 和 pytest 临时目录。

## 2. 今天完成的代码

### Feature Delivery 响应契约修复

`devpilot-agent/src/devpilot/workflows.py` 的 `FeatureDeliveryProposal.to_dict()` 现在会完整返回
`plan_hash`，并且工作流构造对象时使用同一个值。这修复了真实 WebUI 链路中“提案已经生成，但 nanobot
无法注册完整确认 tuple”的问题。

新增回归断言位于：

`devpilot-agent/tests/test_p4_feature_delivery.py`

### DeepSeek Harness 风格的可插拔工具演示

示例位于：

`devpilot-agent/examples/pluggable-tool/`

它使用 `pyproject.toml` 的 `nanobot.tools` entry point 注册一个只读工具 `demo_greeting`。
这个示例体现的是 Provider / Tool Registry / Entry Point 的插拔设计：新增工具只改插件目录，不修改
nanobot Agent Loop，也不绕过 DevPilot 的 Apply、Verify、Rollback 和确认边界。

说明文档位于：

`devpilot-agent/docs/demo/pluggable-tool.md`

本机真实演示曾通过受控 Apply 写入 `formatFarewell` 和 `formatWelcome`；这些演示副作用不纳入仓库，
换机后 fixture 保持干净，录制流程可以重新生成 Diff。它们不是用户 API Key 或真实业务代码。

## 3. 已完成的真实链路验证

在 nanobot 原生 WebUI 中已经跑通一条干净链路：

1. 用户提出新增 `formatWelcome(name)` 的自然语言需求。
2. nanobot 先调用 inspect / search / read，再调用 `devpilot_feature_delivery_propose`。
3. 页面展示 Changeset、Diff、验证 profile 和“仅提案未执行”。
4. 用户在同一聊天中发送“执行”。
5. nanobot 调用 `devpilot_confirmed_action(action=apply_verify)`。
6. Apply 成功后自动进入验证。
7. FC Agent Sandbox（杭州，`code-interpreter-v1`）返回 `exit_code=0`，最终状态为 `verified`。

本次真实结果记录：

- ChangeSet：`a0d92b88-d36d-4ae6-8376-73f32a1206e8`
- Run：`57cbde45-ccb7-456a-a524-8c4555216f90`
- 后端：`fc-agent-sandbox`
- 模板：`code-interpreter-v1`
- 网络策略：`provider-default`
- 退出码：`0`

`provider-default` 表示适配 FC 杭州接口，不再发送 FC 不支持的强制断网参数；结果不能宣称“已证明网络隔离”。
代码上传前仍必须设置 `DEVPILOT_FC_ALLOWED_PROJECTS` 精确 allowlist，避免任意本地项目被上传。

## 4. 本次验证结果

在交接前实际执行：

- `uv run ruff check src scripts tests examples/pluggable-tool/src`：通过
- `uv run pytest -q tests/test_p4_feature_delivery.py`：20 passed
- nanobot WebSocket 回归：172 passed
- `git diff --check`：应在提交前再次执行

`uv run pyright` 仍报告 105 个类型错误，主要集中在既有的 `skill_improvement.py`、`fc_sandbox.py`
和 `scripts/p7_02_benchmark.py`，不是本次 `plan_hash` 修复引入；后续若要恢复全量类型门禁，需单独开任务处理。

## 5. 另一台电脑拉取与安装

首次拉取：

```powershell
cd E:\workspace
git clone https://github.com/g3182479125-hub/devflow-agent.git
cd .\devflow-agent
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
```

已有副本：

```powershell
cd E:\workspace\devflow-agent
git status
git pull --ff-only origin main
```

如果 `git status` 有本地修改，先备份或提交本地修改，再决定是否拉取；不要使用 `reset --hard` 覆盖它们。

然后阅读：

- `AGENTS.md`
- `devpilot-agent/docs/plans/CURRENT_PLAN.md`
- `devpilot-agent/docs/runbooks/local-nanobot-devpilot.md`
- `devpilot-agent/docs/demo/pluggable-tool.md`

## 6. 本地启动要点

在启动控制面板的同一个 PowerShell 进程中设置 FC 配置，尤其是项目 allowlist：

```powershell
$env:DEVPILOT_ENABLE_FC_SANDBOX = "1"
$env:DEVPILOT_FC_NETWORK_POLICY_MODE = "provider-default"
$env:DEVPILOT_FC_TEMPLATE = "code-interpreter-v1"
$env:DEVPILOT_FC_ALLOWED_PROJECTS = "E:\workspace\devflow-agent\devpilot-agent\tests\fixtures\node-project"
```

`E2B_API_KEY`、`E2B_API_URL` 和 `E2B_DOMAIN` 只在本机环境变量或未提交的本地配置中填写，不能写进交接文档。
控制面板默认：`http://127.0.0.1:18800`；nanobot WebUI 默认：`http://127.0.0.1:8765`。

## 7. 录视频的最短流程

在 nanobot 新建普通话题，使用以下口语化但可稳定复现的请求：

```text
帮我给这个 Node 小项目加一个小功能：在 src/index.js 里加 formatThanks(name)，返回 Thanks, name!。
先看看项目结构和目标文件，给我简短方案和 Diff，先不要执行。
```

展示提案后，在同一个话题发送：

```text
执行
```

录制时展示四个关键画面：

1. 提案和折叠后的 Diff；
2. nanobot 聊天中的明确确认；
3. Apply 成功与 FC Sandbox 验证通过；
4. `verified`、`exit_code=0`、`fc-agent-sandbox` 和结果摘要。

不要使用旧 Changeset 重试；确认必须针对当前聊天刚生成的 Changeset。

## 8. 可插拔功能的录制顺序

先安装示例插件并重启 nanobot：

```powershell
cd E:\workspace\devflow-agent\devpilot-agent\examples\pluggable-tool
uv pip install --python E:\workspace\devflow-agent\nanobot\.venv\Scripts\python.exe -e .
```

在普通 nanobot 对话输入：

```text
请调用 demo_greeting，名字叫小明，并告诉我这个工具来自哪里。
```

展示返回 `插件工具已加载：你好，小明！`，然后解释：工具由 entry point 被 ToolLoader 发现，
没有改 Agent Loop，也没有获得文件写入、网络或审批权限。

若要录制“拔出”：

```powershell
uv pip uninstall --python E:\workspace\devflow-agent\nanobot\.venv\Scripts\python.exe devflow-greeting-tool
```

重启 nanobot 后 `demo_greeting` 消失，而原生聊天和 DevPilot 交付工具继续可用。

## 9. 下一步

- 先在新机器按本文件完成拉取、安装和真实 WebUI/FC 复现。
- 录制完成后，再回到冻结计划检查尚未实现的 P8-03 定时批处理；它不能被当前已完成项替代或提前标记完成。
- 后续任何行为改动仍按 ADR-0004 批量修改、一次性双轴 review、只对 review 触及文件增量复审的规则执行。
