# 2026-09-28 跨电脑交接

## 交接结论

- 仓库：`https://github.com/g3182479125-hub/devflow-agent.git`
- 分支：`codex/h11-complete-handoff`
- 本次基线：`3bd5187 fix: guide temporary tool contract and record webui closure`
- 本次新增：强制回复风格指南、完整录制稿和本文档
- P0–P8、H1–H11 及后续已记录扩展的状态仍以 `CURRENT_PLAN.md` 为准；本次没有重新打开已完成节点。

## 本次新增内容

### 回复风格

根目录 `AGENTS.md` 已把
`devpilot-agent/docs/agents/assistant-style-guide.md` 加入每个任务的强制必读清单。
该规则覆盖工具执行期间的进度回复和最终回复，要求结论先行、默认简短、证据可核对。
对应候选提交 `d100a83` 已通过 Standards 和 Spec 两轴独立检查。

### 临时工具热插拔现场闭环

`3bd5187` 已补充临时 Tool 的精确源码契约提示和错误可见性，并记录 2026-09-28
本地 WebUI 实测：

- `text-uppercase` 1.0.0 因 schema 不匹配被拒绝；
- 模型修订为 1.0.1 后，固定本地契约验证通过并在原会话热加载；
- 真实调用返回大写文本，`restart_required=false`；
- 1.0.2 增加首尾空格清理，更新后真实调用通过；
- 停用和移除返回 `state=disposed`、`removed=1`；
- 移除后再次调用返回 `TOOL_VERSION_RETIRED`；
- 工具失败和移除没有锁死后续普通对话。

证据见 `devpilot-agent/docs/evidence/live-harness/2026-09-19-temporary-tool-chain.md`。
本地复核记录为 `39 passed, 1 skipped`、本地执行测试 `25 passed`，Ruff 和
`git diff --check` 通过。

### 录制文档

完整操作与旁白稿位于：

```text
devpilot-agent/docs/runbooks/full-recording-script.md
```

覆盖普通对话、Bug-fix Profile、受控修复、安全边界、Loop Driver 切换、Skill、
临时工具创建/修订/热加载/更新/移除，以及 ShopSphere 页面展示。

## 当前实现边界

- 当前本地临时工具验证使用固定 `LocalExecutionBackend` profile，不是 OS 沙箱；
  候选代码继承当前 Windows 用户权限。
- `TemporaryToolEnsureTool` 每次只执行一次“生成→验证→激活”；验证失败后由模型根据
  错误创建新版本，没有独立的临时工具固定重试次数。
- 主 Agent Loop 是 `for iteration in range(max_iterations)`，默认上限为 200。
- Loop 热切换只替换 `LoopDriver` 的下一步决策规则，不替换 Host 的工具执行、权限、
  持久化和循环主体。
- `PlanThenActDriver` 目前按 `understand → act → verify` 阶段向模型提供 Host 指令；
  它约束阶段顺序，但不是验证每个业务阶段必然完成的严格图工作流。

## 换电脑后的操作

```powershell
git clone -b codex/h11-complete-handoff https://github.com/g3182479125-hub/devflow-agent.git
Set-Location .\devflow-agent
git pull --ff-only origin codex/h11-complete-handoff
git log -3 --oneline
```

然后执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
```

新电脑必须使用自己的模型配置、API Key、控制面 token 和数据目录。启动和录制前依次阅读：

1. `AGENTS.md`
2. `devpilot-agent/docs/plans/CURRENT_PLAN.md`
3. `devpilot-agent/docs/handoffs/2026-09-28-cross-machine-handoff.md`
4. `devpilot-agent/docs/runbooks/full-recording-script.md`
5. `training-projects/shopsphere-bugfix/HANDOFF.md`

## 下一步

1. 在新电脑安装并使用本机模型配置启动 Harness/WebUI。
2. 使用隔离的 ShopSphere 故障副本，按录制稿重新走完整流程。
3. 录制前确认 `http://127.0.0.1:8765` 和 `http://127.0.0.1:5000` 均可访问。
4. 若继续开发，先以当前代码和 `CURRENT_PLAN.md` 为准，不根据旧截图恢复已经废弃的
   “工具失败后锁死整个会话”行为。

## 未上传内容

本机模型配置、API Key、控制面 token、SQLite 数据库、日志、会话、缓存、运行时目录、
临时 pytest 目录，以及根目录现有的个人未跟踪文件均不上传。ShopSphere 的受控训练快照
已经是仓库跟踪内容，不需要从本机运行目录另行复制。
