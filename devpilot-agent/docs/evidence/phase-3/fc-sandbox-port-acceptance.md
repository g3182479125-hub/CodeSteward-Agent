# FC Agent Sandbox 接入端口验收

日期：2026-09-02
范围：ExecutionBackend、Fake 后端、FC E2B 兼容适配器、配置诊断和安全边界

## 已实现

- `ExecutionBackend` 统一执行端口，调用方必须提供 `run_id`、`project_id`、显式文件包、
  显式命令和 1–900 秒超时。
- 输入路径只接受相对路径，拒绝路径穿越、重复路径和空输入。
- `FakeExecutionBackend` 在本地提供确定性契约测试，不执行任何命令。
- `FCSandboxBackend` 懒加载 `e2b-code-interpreter`，创建短生命周期 Sandbox，写入显式文件，
  执行命令，返回 stdout/stderr/exit code，最后无论成功失败都调用 `kill()`。
- 创建 Sandbox 时将请求级网络策略显式传递为 `allow_internet_access`；当前验证 Profile
  默认传递 `False`，避免只在本地策略层检查而未约束云端实例。
- FC 适配器不注册为 nanobot Tool，不接收模型上下文，不读取本机项目目录，不写回本地仓库。
- `devpilot sandbox-check` 提供固定的无害连通性检查，只写入一个测试文件并执行固定 print 命令。
- API Key、API URL、Domain 只从环境变量读取，诊断和错误输出不打印密钥。
- Control Plane 新增独立的 `r2.verify` 授权和 `/verify` 路由；只允许 Python/Node 语法
  Profile，验证结果写入 ChangeSet 状态与 Run 事件，Apply 和 Rollback 授权仍然独立。
- P3 已完成完整项目快照：验证时收集项目目录内受限的 UTF-8 文本文件，并叠加 ChangeSet
  内容；排除 `.git`、依赖/构建目录、虚拟环境、缓存和密钥文件，不把本机目录直接挂载到云端。
- P3 已完成服务端命令白名单：只接受 `python3 -m py_compile`、单文件 `python3` 自测和
  `node --check`，命令路径必须属于显式快照；Shell 操作符和模型自带命令会 fail closed。
- P3 已完成资源与审计边界：最多 200 个文件、单文件 512 KiB、总快照 8 MiB、输出 16 KiB；
  FC 默认执行上限 900 秒，并在结果中记录命令 Profile、快照大小、地域、模板、退出码和耗时。

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

沙箱专项契约测试覆盖 Fake 后端、输入 fail-closed、命令白名单、完整快照、输出/资源上限、
环境变量缺失、网络策略拒绝、
FC 创建/文件写入/命令执行/销毁生命周期模拟。Windows 的 3 个符号链接测试因当前
用户权限跳过，未被伪称为通过。

## 真实账号复验

2026-09-02 使用华北 2（北京）地域的脱敏 fixture 和当前用户级环境变量完成真实 FC
复验，未记录或提交 API Key：

```text
uv sync --extra fc-sandbox
uv run devpilot sandbox-check
status=ok
backend=fc-agent-sandbox
exit_code=0
stdout=devpilot-fc-sandbox-ok
```

随后通过本地 Control Plane 完成一条完整的小型交付链路：

```text
preview -> r1.apply approval -> Apply -> r2.verify approval
-> FC python-run -> exit_code=0 -> r1.rollback approval -> Rollback
```

复验结果为 Apply `applied`、FC 验证 `verified`、验证后端为
`fc-agent-sandbox`、退出码 `0`、输出为 `devpilot-live-fc-ok`，临时探针最终为
Rollback `reverted`，本机项目未留下该探针文件。

## 仍未宣称

本证据证明当前账号和北京地域的最小端到端链路可用，但不宣称生产配额、长期成本、
执行成功率或绝对网络隔离。沙箱仍不是执行授权本身，真实项目仍必须经过人工审批。
后续增强包括验证取消、更细粒度的外部存储治理和可观测性治理。

复验所需环境变量仍必须由运行者在本机提供，不得写入代码仓库：

```powershell
$env:E2B_API_KEY = "<FC Agent Sandbox API Key>"
$env:E2B_API_URL = "https://api.<region>.e2b.fc.aliyuncs.com"
$env:E2B_DOMAIN = "<region>.e2b.fc.aliyuncs.com"
```

FC Agent Sandbox 的当前主路径是 E2B 兼容 SDK；Snapshots、Volume、Team 管理和 Sandbox
Logs 不作为本阶段依赖。P2B Apply 不会自动调用该执行端口；当前验证 Gate 仍使用内部的
`r2.verify` 授权和内置命令 Profile，但用户只需在 nanobot 对话中确认“执行”，不再打开独立
审批页面。验证取消和更细粒度的外部存储/网络治理仍属于后续增强项。
