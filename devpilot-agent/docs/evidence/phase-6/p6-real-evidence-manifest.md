# P6 真实执行证据清单

本清单绑定代码候选提交 `1161722eb369799c44ac8befc30f708806dc5343`；本文件随后由
证据文档提交带入仓库。代码提交与证据提交分开记录，避免文档自引用 SHA。它记录真实
杭州 FC 与本地集成测试的可审计边界。API Key、SQLite、项目源码和原始日志不进入仓库。

## 固定配置

| 字段 | 值 |
|---|---|
| region | `cn-hangzhou` |
| endpoint/domain | `https://api.cn-hangzhou.e2b.fc.aliyuncs.com` / `cn-hangzhou.e2b.fc.aliyuncs.com` |
| template | `code-interpreter-v1` |
| SDK | `e2b-code-interpreter 2.8.1` |
| policy mode | `provider-default` |
| request network flag | `network_allowed=False` |
| isolation conclusion | 未证明；CLI 明确输出 `network_isolation_proven=false` |

## 真实 FC 场景

每条记录使用一次短生命周期 Sandbox，上传显式非敏感 fixture；命令由服务器 profile
固定生成，返回后执行 `kill()`。`sandbox_id` 只作为供应商运行关联标识，不代表隔离证明。

| 场景 | profile/命令 | 结果 | sandbox 关联 |
|---|---|---|---|
| CLI lifecycle | 固定 `python3 /workspace/devpilot/sandbox_check.py` | `exit_code=0`，`status=degraded`，清理完成 | `sbx-dc40e18d-87b0-4c57-9847-2621053c04ef` |
| Node syntax | `node --check /workspace/devpilot/main.js` | `exit_code=0`，`network_policy=provider-default`，清理完成 | `sbx-246efbbd-dae8-4559-9eb4-ebc3e2f9a104` |
| Python syntax failure | `python-syntax` 非敏感语法错误 fixture | `exit_code=1`，stderr 保留，失败与成功分离 | 见 `p6-02-fc-hangzhou-diagnosis.md` 现场记录 |
| timeout | `python-run` 无限循环 fixture | `FC_TIMEOUT`，运行失败并清理 | 见同上 |
| cancel | 运行中调用 `cancel()` | `FC_CANCELLED`，重复取消不创建新运行 | 见同上 |
| recovery | 供应商状态不可查询/进程重启模拟 | `FC_RECOVERY_UNKNOWN`，不创建替代任务 | 见同上 |

## 自动化复核

- DevPilot 集成环境全套 `pytest`：100% 通过。
- nanobot 请求上下文、WebSocket 确认和通道集成：28 项通过。
- `ruff`、`secret_scan.py`、`check_markdown.py`：通过。
- Fake 合同测试与真实 FC 记录分列；Fake 不用于证明网络隔离。

## 不能从本清单推出的结论

`provider-default` 只证明适配器没有发送杭州不支持的 `SessionNetworkPolicy`，不证明 FC
供应商侧断网。真实 React 静态执行、跨进程供应商状态查询/清理仍关闭或未证明；普通项目
只有在服务端显式 `DEVPILOT_FC_ALLOWED_PROJECTS` allowlist 命中后才能发往 FC。
