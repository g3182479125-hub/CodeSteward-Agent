# 2026-09-11 H5 交接文档

## 当前远端检查点

- 仓库：`https://github.com/g3182479125-hub/devflow-agent.git`
- 当前开发分支：`handoff-2026-09-01`
- H5 代码候选：`c115aef`
- GitHub `main` 已更新至交接提交：`36a30b6`。

## 当前完成情况

- H1–H4：已实现，并有既有 Standards / Spec 双轴通过记录。
- H5：候选执行边界代码已提交；定向测试和 Ruff 通过；真实 FC 曾在
  `provider-default` 模式完成过一例成功试运行和清理。
- H5 尚未完成：本次两名独立 reviewer 的增量结论均为 `NEEDS_FIX`。具体问题、路径和下一步
  见 `docs/evidence/live-harness/h5-candidate-execution.md` 及两份 H5 review 报告。
- H6–H10：尚未开始。不要把 H5 的 runner 当作已经具备模型 `activate/remove` 等产品动作。

## 另一台电脑首次拉取

在 Windows PowerShell 执行：

```powershell
cd E:\workspace
git clone https://github.com/g3182479125-hub/devflow-agent.git
cd .\devflow-agent
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
```

`install-integrated.ps1` 会按锁文件安装 nanobot，并把 DevPilot 以 editable 方式接入
nanobot 的虚拟环境。安装完成后，先阅读：

```text
AGENTS.md
devpilot-agent/docs/plans/CURRENT_PLAN.md
devpilot-agent/docs/plans/LIVE_HARNESS_EXTENSION.md
devpilot-agent/docs/adr/ADR-0004-batched-review-and-incremental-recheck.md
devpilot-agent/docs/evidence/live-harness/h5-candidate-execution.md
```

## 已克隆仓库的更新方式

如果这台电脑已经有仓库，先确认没有未提交修改：

```powershell
cd E:\workspace\devflow-agent
git status
git pull --ff-only origin main
```

如果 `git status` 显示你自己的修改，先单独备份或提交，再执行 `git pull --ff-only`；
不要用 `reset --hard` 覆盖本地工作。

## 换机后验证

DevPilot 定向验证：

```powershell
cd E:\workspace\devflow-agent\devpilot-agent
uv run ruff check src tests
uv run pytest tests/test_h5_candidate_runner.py tests/test_fc_sandbox_contract.py tests/test_p3_execution_boundary.py -q
```

nanobot 集成回归：

```powershell
cd E:\workspace\devflow-agent\nanobot
uv run pytest -q
```

如果使用集成安装脚本生成的 nanobot 虚拟环境，也可从 `devpilot-agent` 目录执行：

```powershell
& ..\nanobot\.venv\Scripts\python.exe -m pytest -q
```

## FC 配置

API Key 只能在本机环境变量或本机密钥管理器中配置，绝不写入仓库、交接文档或截图。
杭州 FC 的非敏感配置如下：

```powershell
$env:E2B_API_URL = "https://api.cn-beijing.e2b.fc.aliyuncs.com"
$env:E2B_DOMAIN = "cn-beijing.e2b.fc.aliyuncs.com"
$env:DEVPILOT_FC_TEMPLATE = "code-interpreter-v1"
$env:DEVPILOT_FC_NETWORK_POLICY_MODE = "provider-default"
$env:DEVPILOT_ENABLE_FC_SANDBOX = "1"
$env:DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE = "1"
$env:DEVPILOT_FC_ALLOW_NETWORK = "0"
```

还需由操作者在本机设置 `E2B_API_KEY`。`provider-default` 是当前杭州 FC 兼容模式，
不等于网络隔离；不要在简历或验收中写成“断网沙箱”。真实云调用会产生费用，正式测试只用
自有合成 fixture，并控制次数、超时和清理。

## H5 下一步（换机后继续）

1. 强制 H5 生产路径使用真实 `ProjectStore` 和可验证的 Host TrialGrant。
2. 将 session/project/版本/哈希/run/sandbox/未知状态持久化，补查询、取消、恢复和
   跨进程原子预算预留；损坏记录必须失败关闭。
3. 强化 FC 固定 profile/template 校验，补候选源码本机路径检测。
4. 运行受影响测试后，仍使用同一对 reviewer 做增量复核；两轴 PASS 前不要开始 H6。

## 本机未上传内容

本次刻意没有提交以下工作区内容：

- `devpilot-agent/tests/fixtures/node-project/src/index.js`：用户本地 fixture 修改。
- `nanobot/MagicMock/`：测试生成目录。
- API Key、`.env`、SQLite、日志、虚拟环境、缓存和 `node_modules`。

若另一台电脑需要 fixture 变更，请由用户单独确认并提交；不要把这些本地内容混入 H5
交接提交。
