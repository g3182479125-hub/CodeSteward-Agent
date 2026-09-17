# P6 真实执行证据清单

本清单保留早期候选 `1161722eb369799c44ac8befc30f708806dc5343`、
`b939d018dbe085a9982f890644e86f14be4ae062` 与 `570208b1e4f5dbd656bb80dc8a4a8bc1224949cd`
的历史证据。本轮实现候选为 `65cf72c`，其固定点为 `92884b9`；本清单同时记录本轮新增的
`python-regression` FC 证据。它记录真实 FC 与本地集成测试的可审计边界。API
Key、SQLite、项目源码和原始日志不进入仓库。

## 固定配置

| 字段 | 值 |
|---|---|
| region | `cn-beijing` |
| endpoint/domain | `https://api.cn-beijing.e2b.fc.aliyuncs.com` / `cn-beijing.e2b.fc.aliyuncs.com` |
| template | `code-interpreter-v1` |
| SDK | `e2b-code-interpreter 2.8.1` |
| policy mode | `provider-default` |
| request network flag | `network_allowed=False` |
| isolation conclusion | 未证明；CLI 明确输出 `network_isolation_proven=false` |
| non-secret configuration fingerprint | `sha256:671e8d5c5df6f6b0dee49ef78b3451d0f9985b3a9ee97821b081ca48c937272e` |

配置指纹由下列规范化、非敏感字段按键排序后计算：`region`、`api_url`、`domain`、`template`、
`network_policy_mode`、`network_allowed=false`、已启用 profile 集合
`[node-syntax, project-npm-test, project-pytest, python-regression, python-run, python-syntax]` 与
`allowlist_mode=explicit-project-paths`。本轮规范化 JSON 为：
`{"allowlist_mode":"explicit-project-paths","api_url":"https://api.cn-beijing.e2b.fc.aliyuncs.com","domain":"cn-beijing.e2b.fc.aliyuncs.com","network_allowed":false,"network_policy_mode":"provider-default","profiles":["node-syntax","project-npm-test","project-pytest","python-regression","python-run","python-syntax"],"region":"cn-beijing","template":"code-interpreter-v1"}`。它不包含 API Key、allowlist 的实际路径或任何项目内容。

## 验证版本与最终候选复核

历史候选 `570208b1e4f5dbd656bb80dc8a4a8bc1224949cd` 曾在 Python `3.13.14`、`e2b 2.31.0`、
`e2b-code-interpreter 2.8.1`、`pytest 8.4.2`、`ruff 0.16.5`、`pyright 1.1.411` 环境中完成
本地复核。以下命令是该历史候选的记录，不代表本轮工作区的最终审查结论：

```powershell
uv run --no-sync pytest tests/test_fc_sandbox_contract.py tests/test_p3_execution_boundary.py tests/test_p2a_migration.py -q
# 31 passed
uv run --no-sync ruff check src/devpilot/action_broker.py src/devpilot/control_plane.py src/devpilot/fc_sandbox.py
uv run --no-sync pyright
git diff --check b939d018dbe085a9982f890644e86f14be4ae062...570208b1e4f5dbd656bb80dc8a4a8bc1224949cd
```

历史复核曾确认 `python-regression` 未接入 FC；该结论已因 `65cf72c` 的能力配置变化而失效。
当前 `python-regression` 的新增真实证据见下文，`react-static` 仍失败关闭。真实 FC 证据仍仅
证明本清单下的降级执行，不新增网络隔离或 React 执行结论。

## 真实 FC 场景

每条记录使用一次短生命周期 Sandbox，上传显式非敏感 fixture；命令由服务器 profile
固定生成，返回后执行 `kill()`。`sandbox_id` 只作为供应商运行关联标识，不代表隔离证明。

| 场景 | profile/命令 | 结果 | sandbox 关联 |
|---|---|---|---|
| CLI lifecycle | 固定 `python3 /workspace/devpilot/sandbox_check.py` | `exit_code=0`，`status=degraded`，清理完成 | `sbx-dc40e18d-87b0-4c57-9847-2621053c04ef` |
| Node syntax | `node --check /workspace/devpilot/main.js` | `exit_code=0`，`network_policy=provider-default`，清理完成 | `sbx-246efbbd-dae8-4559-9eb4-ebc3e2f9a104` |
| Python regression after approved Apply | `python-regression` → `python -m pytest -q`，前置固定 pytest bootstrap | `exit_code=0`，stdout 为 `5 passed in 0.02s`，清理完成 | `sbx-9d504d1e-39f3-4698-a25a-e32da91576e2`；ChangeSet `eb6f1dbb-7d50-4ff3-b6db-84928c2086e8` |
| Python syntax failure | `python-syntax` 非敏感语法错误 fixture | `exit_code=1`，stderr 保留，失败与成功分离 | 见 `p6-02-fc-hangzhou-diagnosis.md` 现场记录 |
| timeout | `python-run` 无限循环 fixture | `FC_TIMEOUT`，运行失败并清理 | 见同上 |
| cancel | 运行中调用 `cancel()` | `FC_CANCELLED`，重复取消不创建新运行 | 见同上 |
| recovery | 供应商状态不可查询/进程重启模拟 | `FC_RECOVERY_UNKNOWN`，不创建替代任务 | 见同上 |

## 自动化复核

- DevPilot 集成环境全套 `pytest`：100% 通过。
- nanobot 请求上下文、WebSocket 确认和通道集成：28 项通过。
- `ruff`、`secret_scan.py`、`check_markdown.py`：通过。
- Fake 合同测试与真实 FC 记录分列；Fake 不用于证明网络隔离。
- `python-regression` 控制面→FC 路径集成测试覆盖固定命令和 bootstrap 顺序；真实现场结果见
  [2026-09-17 pytest 回归证据](p6-05-pytest-regression-evidence-2026-09-17.md)。

## 当前范围与未交付增强

本项目接受 `provider-default` 作为 FC 验证执行模式，不把它描述为断网沙箱，也不以网络
隔离证明作为当前退出条件。真实 React 静态执行属于后续增强；跨进程供应商状态查询和清理
已支持显式恢复动作；普通项目只有在服务端显式 `DEVPILOT_FC_ALLOWED_PROJECTS` allowlist 命中后才能
发往 FC。
