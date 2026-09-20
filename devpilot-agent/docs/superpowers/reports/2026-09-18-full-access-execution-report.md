# Full Access 高信任执行模式：本轮执行报告

日期：2026-09-18  
依据：`docs/superpowers/specs/2026-09-18-full-access-high-trust-execution-design.md`

## 本轮已完成

本轮没有放开 Apply、Verify、Rollback，也没有删除现有工作区、项目锁、敏感文件和 Shell 安全边界。完成的是普通开发操作的统一结果契约：

1. 新增 `nanobot/nanobot/security/execution_policy.py`。
   - 决策：`AUTO_EXECUTE`、`REQUIRE_CONFIRMATION`、`BLOCKED_BY_POLICY`、`CAPABILITY_UNAVAILABLE`、`PRECONDITION_FAILED`。
   - 错误码：策略拒绝、操作系统权限、参数错误、超时、命令失败、工具不可用、取消和未知错误。
   - `WorkspaceBoundaryError` 与真正的 OS `PermissionError` 分开分类，避免把普通权限/环境故障误报成策略拒绝。
2. 扩展 `ToolResult`，保留原有字符串协议，同时携带 `code`、`decision`、`retryable`。
3. Tool Registry 在追加诊断提示时保留错误元数据，并为工具不存在、参数错误和执行异常提供分类。
4. Shell 接入分类：
   - 安全 deny pattern、工作区越界、内部地址和路径穿越 → `BLOCKED_BY_POLICY`；
   - 超时 → `TIMEOUT` / `PRECONDITION_FAILED`；
   - 命令自身非零退出 → `COMMAND_FAILED` / `AUTO_EXECUTE`，不再伪装成权限问题；
   - Shell 不存在或不可执行 → `TOOL_UNAVAILABLE` / `CAPABILITY_UNAVAILABLE`。
5. 文件工具和 CLI App 工具的异常结果接入同一分类层。
6. 结构化 WebSocket 工具事件同步 `code`、`decision`、`retryable`，前端类型已同步，旧事件不增加空字段。
7. 新增契约测试，覆盖策略拒绝、OS 权限、命令失败、文件越界、Registry 元数据保留和 WebSocket 事件传递。

## 验证结果

- nanobot 全量相关回归：`1957 passed, 1 skipped`。
- 新增策略与核心工具回归：`86 passed, 1 skipped`；文件/Registry/Shell 扩展回归：`121 passed, 1 skipped`。
- DevPilot 全量回归：`419 passed, 3 skipped`，使用 `nanobot/.venv`（包含 `pytest-asyncio`）运行；H6/H8/H9/H11 异步用例已实际执行。
- WebUI：本轮相关测试 `153 passed`；WebUI 历史全量基线 `182 passed`。
- WebUI 生产构建：通过。
- WebUI ESLint：通过。
- 本轮变更目标文件 Ruff：通过。

完整回归使用仓库外的 pytest 临时目录，避免测试临时目录被 GitStore 误识别为嵌套仓库。

## 保留的行为边界

- Full Access 只改变当前任务范围内普通开发操作的授权，不等于无限权限。
- Apply / Verify / Rollback 仍然走原有确认、基线、锁和 CAS/Revision 门禁。
- Shell 的破坏性 deny pattern、私有/内部地址、受保护 Skill 和私有数据目录仍然硬阻断。
- 当前分支已有的用户修改、训练项目缺陷和临时测试目录未清理、未覆盖。
- 本报告记录的是本轮代码落地与验证，不把新增分类层夸大为取消所有外部系统权限或凭据要求。

## 本轮新增验收证据

- Full Access 工作区与 Shell 矩阵：`74 passed, 1 skipped`。
- 取消、停止、取消后的上下文恢复：`28 passed`。
- 统一会话与进度事件：`30 passed`。
- DevPilot H6–H11 异步候选/远程循环/会话绑定回归：在 nanobot 虚拟环境中全量通过。
- WebUI 结构化错误中文展示：`POLICY_DENIED` → `策略阻断`、`TIMEOUT` → `等待超时`、`OS_PERMISSION_DENIED` → `系统权限不足`、命令非零退出 → `命令执行失败`，对应测试已通过。

## 测试环境注意

`devpilot-agent/.venv` 当前没有同步 `pytest-asyncio`，直接用该解释器运行异步测试会出现“async def functions are not natively supported”；这不是业务失败。项目的 `pyproject.toml` 已在 `dependency-groups.dev` 声明 `pytest-asyncio`。换机后应先用 `uv sync --group dev`（或直接使用已安装完整依赖的环境）再运行测试。当前机器有 nanobot 环境可用于本轮验证，因此 H6–H11 已按正确插件实际执行。

## 明日继续入口

先查看本报告和冻结规格，再运行：

```powershell
cd E:\codex-local\nanobot-devpilot-agent
git status --short
nanobot\.venv\Scripts\python.exe -m pytest -q nanobot\tests\security\test_execution_policy.py --basetemp E:\codex-local\pytest-next
```

若继续做下一批，优先补真实 FC/本地服务条件下的录制视频验收，以及审计日志查询；不要修改已冻结规格，不要用提示词替代服务端门禁。
