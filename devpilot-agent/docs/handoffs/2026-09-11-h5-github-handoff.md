# 2026-09-11 H5 交接文档

## 当前远端检查点

- 仓库：`https://github.com/g3182479125-hub/devflow-agent.git`
- 当前开发分支：`main`
- H5 最新代码候选：`e08299e`
- 本轮增量提交：`9d216c7`、`e08299e`。
- 当前工作区基于远端 `main`，H5 已完成；本次文档提交后再形成新的交接提交。

## 当前完成情况

- H1–H4：已实现，并有既有 Standards / Spec 双轴通过记录。
- H5：模型候选执行边界已实现；生产路径强制使用真实 `ProjectStore`、具体 FC 后端和
  Host 签发的可验证 TrialGrant；operation、结果、run/sandbox、预算租约支持持久化、
  CAS/fencing、查询、取消和恢复。
- H5 Review：同一对 Standards / Spec reviewer 对 `9d216c7...e08299e` 均为 `PASS`。
- 验证：Ruff 通过；H5/FC/P2B 定向测试通过；全量 `317 passed, 3 skipped`（共收集 320 项）。
- H6–H10：尚未开始。H5 仍不是模型侧 `inspect/define/trial/activate/stop/remove` 产品动作。

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

## H5 可选本地配置

H5 的 runner 工厂默认关闭。仅在准备离线/受控试验时，在本机设置以下变量；密钥和
Grant secret 不写入仓库：

```powershell
$env:DEVPILOT_ENABLE_MODEL_CANDIDATES = "1"
$env:DEVPILOT_CANDIDATE_GRANT_SECRET = "<至少 32 字节的本地随机 secret>"
```

H5 尚未提供模型管理动作或独立管理页面，不能仅凭这两个变量在聊天中执行
`activate/remove`；这些属于 H6。

## H6 下一步（换机后继续）

按 `docs/plans/LIVE_HARNESS_EXTENSION.md` 实现最少模型动作：
`inspect → define → trial → activate → stop/remove`。必须复用 H5 runner 和现有
nanobot 原生聊天，不能增加第二个 Runtime、管理页面或宿主 import 候选代码；激活前
核对候选内容哈希、成功 trial、当前契约、授权和预算。按 ADR-0004 对 H6 做一次批量
双轴 Review，集中修复后由同一对 reviewer 只复核增量，禁止逐个问题循环 Review。

## 本机未上传内容

本次刻意没有提交以下工作区内容：

- `devpilot-agent/tests/fixtures/node-project/src/index.js`：用户本地 fixture 修改。
- `nanobot/MagicMock/`：测试生成目录。
- API Key、`.env`、SQLite、日志、虚拟环境、缓存和 `node_modules`。

若另一台电脑需要 fixture 变更，请由用户单独确认并提交；不要把这些本地内容混入 H5
交接提交。
