# FC Agent Sandbox 接入端口验收

日期：2026-09-01  
范围：ExecutionBackend、Fake 后端、FC E2B 兼容适配器、配置诊断和安全边界

## 已实现

- `ExecutionBackend` 统一执行端口，调用方必须提供 `run_id`、`project_id`、显式文件包、
  显式命令和 1–900 秒超时。
- 输入路径只接受相对路径，拒绝路径穿越、重复路径和空输入。
- `FakeExecutionBackend` 在本地提供确定性契约测试，不执行任何命令。
- `FCSandboxBackend` 懒加载 `e2b-code-interpreter`，创建短生命周期 Sandbox，写入显式文件，
  执行命令，返回 stdout/stderr/exit code，最后无论成功失败都调用 `kill()`。
- FC 适配器不注册为 nanobot Tool，不接收模型上下文，不读取本机项目目录，不写回本地仓库。
- `devpilot sandbox-check` 提供固定的无害连通性检查，只写入一个测试文件并执行固定 print 命令。
- API Key、API URL、Domain 只从环境变量读取，诊断和错误输出不打印密钥。
- Control Plane 新增独立的 `r2.verify` 授权和 `/verify` 路由；只允许 Python/Node 语法
  Profile，验证结果写入 ChangeSet 状态与 Run 事件，Apply 和 Rollback 授权仍然独立。

## 本地证据

```text
python -m pytest -q -ra
79 passed, 3 skipped
python -m ruff check src scripts tests migrations
All checks passed
python -m pyright
0 errors, 0 warnings, 0 informations
python scripts/secret_scan.py
Secret scan passed
python scripts/check_markdown.py
Markdown link check passed
uv lock --check
Resolved 59 packages
```

沙箱专项契约测试覆盖 Fake 后端、输入 fail-closed、环境变量缺失、网络策略拒绝、
FC 创建/文件写入/命令执行/销毁生命周期模拟。Windows 的 3 个符号链接测试因当前
用户权限跳过，未被伪称为通过。

## 未宣称

本证据不宣称真实阿里云账号已经连通，也不宣称网络隔离、生产配额、成本或执行成功率。
真实验证需要用户在目标地域创建 FC Agent Sandbox API Key，配置与资源地域匹配的
`E2B_API_KEY`、`E2B_API_URL`、`E2B_DOMAIN`，再运行：

```powershell
uv sync --extra fc-sandbox
uv run devpilot sandbox-check
```

FC Agent Sandbox 的当前主路径是 E2B 兼容 SDK；Snapshots、Volume、Team 管理和 Sandbox
Logs 不作为本阶段依赖。P2B Apply 不会自动调用该执行端口；当前验证 Gate 已使用独立
`r2.verify` 授权和内置命令 Profile。下一阶段仍需完善完整项目快照、测试命令白名单、
取消、结果审计和成本上限。
