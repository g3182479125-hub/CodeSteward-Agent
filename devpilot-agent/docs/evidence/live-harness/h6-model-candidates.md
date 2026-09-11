# H6 模型候选插件：实现与离线证据

日期：2026-09-12
节点：H6（模型创建、试用、更新和卸载工具）
状态：代码批次完成；待同一对 Standards / Spec reviewer 的批量 Review、增量复核，以及真实 DeepSeek + nanobot WebUI + 杭州 FC 场景验收。

## 目标和实际落点

H6 要把“模型发现缺能力→定义候选→受控试用→激活→调用→生成新版本→热切换→停用/移除”接到 nanobot 原生聊天，同时保留 H5 的 FC 执行边界。实际实现如下：

| 能力 | 实际落点 | 关键约束 |
|---|---|---|
| 模型管理入口 | `devpilot-agent/src/devpilot/model_plugins.py` 的 `ModelPluginManagementTool` | 一个 nanobot Tool，动作固定为 `inspect/define/trial/activate/stop/remove` |
| 候选工件 | `ModelCandidateController.define` | 按 session、插件、版本、源码哈希分目录；源码、manifest、输入规则不可覆盖 |
| 试用 | `ModelCandidateController.trial` | 复用 H5 `CandidateTrialRunner`；成功后绑定 trial/source/input hash |
| 代理工具 | `ModelCandidateProxyTool` | 宿主不 import 候选源码；只把结构化输入交给 FC proxy |
| 首次激活 | `PluginManager.activate_candidate` | 只接受同一候选的 successful trial，检查 scope、授权、策略和预算 |
| 在线更新 | `PluginManager.update_candidate` + H3 `update` | v1 在线时激活 v2 走原子路由切换；旧代次撤销新派发后排空清理 |
| 状态恢复 | `PluginManager._save_state/_restore_state` | 持久候选状态、trial 绑定和激活路由；新会话仍需重新授权 |
| 停用/移除 | `ModelCandidateController.manage` | stop 要求版本与当前激活版本一致；remove 只移除登记，工件仍保留 |

## 已验证行为

测试均在 E 盘临时目录运行，未读取真实 key、数据库或项目源码：

```powershell
cd E:\codex-local\nanobot-devpilot-agent\devpilot-agent
uv run ruff check --fix src/devpilot/model_plugins.py tests/test_h6_model_plugins.py ..\nanobot\nanobot\agent\plugins\manager.py
uv run --with pytest-asyncio pytest tests/test_h6_model_plugins.py --basetemp E:\codex-local\nanobot-devpilot-agent\.pytest-basetemp-h6-revision2 -q
uv run --with pytest-asyncio pytest tests/test_h5_candidate_runner.py tests/test_h6_model_plugins.py ..\nanobot\tests\agent\test_plugin_manager.py ..\nanobot\tests\agent\test_plugin_h3.py ..\nanobot\tests\agent\test_plugin_h4.py --basetemp E:\codex-local\nanobot-devpilot-agent\.pytest-basetemp-h6-regression -q
```

结果：H6 行为测试 `5 passed`；H5 runner、H3/H4 生命周期/更新/依赖回归合计 `83 passed`；DevPilot 全量 `322 passed, 3 skipped`；Ruff 通过。3 个跳过项均为 Windows 创建 symlink 需要额外权限，不是 H6 失败。

覆盖的事实：

1. 未授权试用返回 `PLUGIN_AUTHORIZATION_REQUIRED`，不能靠模型参数直接激活。
2. 定义和成功试用后，下一步同一 manager 的动态工具视图立即出现候选工具，无需进程或 AgentLoop 重启。
3. v1 已激活时，v2 需重新试用并绑定；激活 v2 走原子更新，下一次调用记录的 manifest 版本为 v2，旧路由进入清理。
4. 同一候选的激活范围为 session；另一 session 的 inspect 不可见，跨会话激活返回 `plugin_not_found`。
5. 修改已登记的 `candidate.py` 后，源码哈希校验拒绝试用；持久化 manager 重建后，已激活路由可恢复为动态工具。

## 尚未证明的边界

- 当前证据使用受控 Fake runner 验证管理器、路由和状态语义，不能替代真实 FC 请求。
- 尚未完成真实网页长任务 H6-A：同一聊天中展示 define→trial→activate→call→revision→call→stop/remove，并记录 PID 不变。
- 尚未完成真实 FC 授权撤销/过期、预算耗尽和云端中断现场证据；代码路径仍需按 H5/H6 契约进行现场测试。
- 当前 stop 返回管理操作结果；若 provider 资源仍在排空，最终状态以 operation 查询为准，不应把中间 `draining` 当作已清理。
- H7 的 Loop Driver 替换和跨 Driver checkpoint 尚未开始，H6 只保证动态工具在同一 nanobot Runtime 内热切换。

## 下一步

固定本批次候选后，按 ADR-0004 一次并行派发 Standards/Spec review；集中修复有效缺陷，再让同一对 reviewer 只复核增量。双轴无待修复项后，使用用户本地配置完成真实 WebUI + DeepSeek + 杭州 FC H6-A/H6-F 验收；在这些条件满足前，不进入 H7。
