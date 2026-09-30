# 2026-09-30 一键本地录制交接

## 结论

分支 `codex/h11-complete-handoff` 新增了可跨电脑复用的一键本地录制启动器。首次在新电脑填写自己的模型配置后，运行一条 PowerShell 命令即可准备故障商城、Skill、授信项目和本地服务。

仓库不会包含模型 Key、Control Plane token、数据库、会话、虚拟环境或测试日志。

## 换电脑后的操作

```powershell
git clone -b codex/h11-complete-handoff https://github.com/g3182479125-hub/devflow-agent.git
Set-Location .\devflow-agent
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\start-local-recording-demo.ps1
```

首次运行前，必须在该 Windows 用户的本机创建并填写模型配置：

```text
%USERPROFILE%\.nanobot\config.json
```

它是唯一不能从 GitHub 获取的录制前置项。模型配置可用后，启动器会按需安装本地依赖。

若之前的录制副本已被修复，需要重新制造故障：

```powershell
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\start-local-recording-demo.ps1 -ResetDemo
```

若默认端口已被占用，可运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\start-local-recording-demo.ps1 `
  -ShopPort 5001 -AgentPort 8766 -GatewayPort 18791 -ControlPlanePort 18801
```

## 启动器做什么

`devpilot-agent/scripts/start-local-recording-demo.ps1`：

1. 建立仓库旁的可丢弃 `shopsphere-recording-demo` Git worktree；
2. 从 `c001238cfbd54e90a01ee7a129bf7b199f084873` 恢复 ShopSphere 基线；
3. 注入两个录制故障：库存商品在页面显示为缺货，以及并发取消订单会重复回补库存；
4. 注入库存页面故障的回归测试，使 Agent 能先复现再修复；
5. 发布 `bug-fix`、`repo-analysis`、`feature-delivery` 三个本地 Skill；
6. 创建 Windows 用户级的本地随机 token（缺失时才创建），启动并授信 DevPilot 项目；
7. 启动 ShopSphere、DevPilot control plane 和 nanobot WebUI。

默认页面：

- 商城：`http://127.0.0.1:5000/c/books-media`
- Personal Agent：`http://127.0.0.1:8765/`

## 真实验证记录

2026-09-30 在本机用隔离端口 `5001 / 8766 / 18791 / 18801` 完整执行成功：

- 商城 HTTP 200，页面包含 `Out of stock`；
- Personal Agent HTTP 200；
- DevPilot 项目状态为 `trusted`；
- 三个本地 Skill 均已发布；
- 库存页面回归测试按预期失败；
- 并发取消订单回归测试按预期失败，断言为 `2 != 1`；
- Pester 启动脚本测试为 `8 passed`；两个 PowerShell 脚本通过语法解析，`git diff --check` 通过。

测试期间发现并修复：工作树检出参数冲突、Skill 传入目录而非 `SKILL.md`、已有控制面复用旧 allowed roots、以及固定端口误把旧服务当新服务的问题。启动器现支持控制面、Agent、Gateway 和商城端口隔离。

## 边界与后续

- FC/cloud 默认关闭；本地临时 Tool 继承当前 Windows 用户权限，不是 OS 沙箱。
- 本次录制展示 Skill 热刷新，不展示 Skill 自进化。
- 启动器只准备和恢复演示基线；修复、ChangeSet、确认、验证与回滚仍在 Personal Agent 中按录制稿完成。
- 完整录制说明：`devpilot-agent/docs/runbooks/one-click-local-recording.md`。
- 原有完整讲稿：`devpilot-agent/docs/runbooks/full-recording-script.md`；其中历史 FC/Skill 自进化段落不属于当前本地录制范围，应以一键运行说明为准。

## Review 状态

本次变更已完成自动化和真实本机启动验证。当前执行环境没有可调用的 Standards/Spec 子代理接口，因此双轴独立审查未执行，不能标记为双审通过。
