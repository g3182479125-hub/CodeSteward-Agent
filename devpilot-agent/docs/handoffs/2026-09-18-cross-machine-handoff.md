# 2026-09-18：跨电脑继续开发交接

## 结论

当前冻结计划 `devpilot-agent/docs/plans/CURRENT_PLAN.md` 中的 P0–P8、H1–H11 均已有完成记录，没有待实现的冻结功能节点。

本次交接重点：

- WebUI 普通对话的 LLM 总等待时间已从 75 秒改为默认 6 小时，正值上限也是 6 小时；
- 录制训练项目保持故意失败基线，便于在另一台电脑重新录制完整 Bug-fix 流程；
- 录制稿覆盖项目理解、Bug-fix、Feature delivery、动态 Harness 工具、工具热更新、Loop 热切换、Skill 自进化、停止恢复和安全边界；
- 当前浏览器现场的 Harness 演示被 DeepSeek API key 配额不足阻断，不能把这次错误当作产品功能失败，也不能在录制中伪装成功。

## 仓库和分支

- GitHub：<https://github.com/g3182479125-hub/devflow-agent>
- 分支：`codex/h11-complete-handoff`
- 远端：`origin`

换机时以该分支推送后的最新提交为准：

```powershell
git clone -b codex/h11-complete-handoff https://github.com/g3182479125-hub/devflow-agent.git
Set-Location .\devflow-agent
```

已有副本使用：

```powershell
git fetch origin
git switch codex/h11-complete-handoff
git pull --ff-only origin codex/h11-complete-handoff
git status --short
git log -1 --oneline
```

## 本次已完成内容

### 六小时 WebUI LLM 超时

实现位置：`nanobot/nanobot/session/goal_state.py` 和 `nanobot/nanobot/agent/loop.py`。

- 默认值：`21600` 秒；
- 正值最大值：`21600` 秒；
- 超过上限会钳制到 6 小时；
- 显式设置 `0` 仍表示关闭该总超时；
- 非法值、NaN、无穷值回退到 6 小时；
- 只影响普通 WebUI WebSocket 回合；Cron、持续 Goal、工具执行、流式空闲超时和 Apply/Verify/Rollback 未改变。

相关超时与 WebSocket 合同测试使用仓库内独立 `--basetemp` 执行，结果为 `23 passed`。

### Harness 与 Skill 能力

冻结计划中的 H1–H11、P8-01/P8-02/P8-03 已有代码、证据和专项测试：

- 动态工具定义、固定示例试用、激活、停用、移除；
- 工具版本热更新和原子切换；
- 候选工具通过 FC proxy 执行，不在宿主机直接 import；
- 默认权限与完全访问权限下的候选自主策略；
- `default-react` 与 `plan-then-act` 在安全边界切换；
- owner、checkpoint、预算和旧副作用保护；
- 反馈形成私有 Skill 候选，固定评测后才能人工确认启用；
- Skill 候选默认不加载、不自动激活，可恢复旧版本；
- 原生 Cron/Dream 的 Skill 改进批处理默认关闭。

本机已执行并通过 H5、H6、H8、H9、H11 和 P8 专项测试集合。

## 录制训练项目

位置：`training-projects/python-project-template-bugfix`

这是专门用于录制的故意故障项目，不要提前手动修复。基线应为：

```text
3 failed, 2 passed
```

关键基线 hash：

- `main.py`: `3e8e9010af20b0e441635588959017ecc986eedf3ba49a2725a23f2406973cee`
- `tests/main_test.py`: `ed6c072b0d42acd2aa83eeb62befd0d045e7a077eb959e821b32bbee07f03755`

录制主线：只读分析 → FC 失败证据 → 正式 ChangeSet → 单独发送 `执行` → Apply/Verify → `5 passed` → 单独发送 `撤销` → 回到 `3 failed, 2 passed`。

录制文档：

- `devpilot-agent/docs/runbooks/unknown-bug-full-capability-video.md`
- `devpilot-agent/docs/runbooks/natural-language-full-flow-test.md`
- `devpilot-agent/docs/runbooks/h10-video-demo.md`

## 换机安装和启动

不要复制旧电脑的 API Key、Token、`config.json` 私密字段、SQLite、会话、日志、确认令牌或虚拟环境。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\install-integrated.ps1
```

准备三个本机目录：模型配置、录制工作区、DevPilot 数据目录。启动后检查：

- WebUI：`http://127.0.0.1:8765`
- Gateway：`http://127.0.0.1:18790`
- DevPilot 控制面：`http://127.0.0.1:18800`

参考 `devpilot-agent/docs/handoffs/2026-09-17-final-cross-machine-handoff.md` 的“换电脑操作”第 1–7 步。项目需要在新电脑重新登记并 trust，不能复制旧电脑数据库。

## 继续工作的第一步

模型配置完成后，先在 WebUI 新会话发送：

```text
请只回复：模型连通正常。
```

如果返回 API key 配额不足/欠费，先处理模型账户或更换有效配置；不要反复重放会产生副作用的 Harness 或 Apply 请求。

模型恢复后，按以下顺序继续：

1. `/new`，选择 `feature-delivery`；
2. 运行中 Harness：`inspect → define → trial → activate → call`；
3. 定义 v2，试用后停用并移除 v1；
4. `/plugins switch-loop plan-then-act`，继续一条不调用工具的总结，再切回 `default-react`；
5. `/skill-improvement` 查看候选和固定评测状态；
6. `/skill-improvement schedule status` 确认定时改进仍为默认关闭；
7. 最后再录制项目 Bug-fix 主线。

确认词必须单独发送：`执行`、`撤销`、`确认试用`、`确认启用`。其它话可以使用自然语言。

## 已知边界

- 当前 FC 是 `provider-default`，不要表述成已经证明严格断网；
- 动态 Harness 工具、Skill 版本和 Loop 切换不要求重启；传统 Python entry-point 插件的安装/卸载仍可能需要重启；
- Skill 自进化不会自动修改项目，也不会自动绕过 Apply/Verify/Rollback 确认；
- React 静态执行不是当前真实 FC 录制主线，不要把旧 fixture 证据说成已交付 React 产品页面；
- 录制或测试中如果模型没有最终答复，继续询问状态或要求总结，不要重复发送旧的副作用请求；
- Windows pytest 临时目录可能有权限噪声，验证时使用仓库 `work/` 下独立的 `--basetemp`。

## 工作区纪律

本机当前存在未提交的 `nanobot/webui/package-lock.json` 修改，以及大量历史 pytest 临时目录。这些不是本次交接内容，未放入交接提交；换机后不要把它们当成产品变更，也不要执行 `git add .` 盲目提交。

新增功能必须先更新冻结计划，再按 `AGENTS.md` 的范围、测试和 Standards/Spec 双轴 review 流程继续。
