# H6 模型候选插件：实现与离线证据

日期：2026-09-12
节点：H6（模型创建、试用、更新和卸载工具）
状态：代码修复批次完成；本地测试通过；真实 DeepSeek + nanobot WebUI + 杭州 FC 的 H6-A 主链路已通过；双轴增量 Review 尚未形成可用 PASS 结论。

## 目标和实际落点

H6 要把“模型发现缺能力→定义候选→受控试用→激活→调用→生成新版本→热切换→停用/移除”接到 nanobot 原生聊天，同时保留 H5 的 FC 执行边界。实际实现如下：

| 能力 | 实际落点 | 关键约束 |
|---|---|---|
| 模型管理入口 | `devpilot-agent/src/devpilot/model_plugins.py` 的 `ModelPluginManagementTool` | 一个 nanobot Tool，动作固定为 `inspect/define/trial/activate/stop/remove/status/revoke` |
| 候选工件 | `ModelCandidateController.define` | 按 session、插件、版本、源码哈希分目录；源码、manifest、输入规则不可覆盖 |
| 试用 | `ModelCandidateController.trial` | 复用 H5 `CandidateTrialRunner`；Host 固定 fixture，成功后绑定 trial/source/input/执行契约 hash |
| 代理工具 | `ModelCandidateProxyTool` | 宿主不 import 候选源码；只把结构化输入交给 FC proxy |
| 首次激活 | `PluginManager.activate_candidate` | 只接受同一候选的 successful trial，检查 scope、授权、策略、sidecar 和执行契约 |
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

结果：H6 行为测试 `8 passed`；H5 runner、H3/H4 生命周期/更新/依赖回归通过；DevPilot 全量测试跑到 `100%` 无失败；Ruff 和 Python 语法检查通过。全量测试中的既有跳过项仍是 Windows 创建 symlink 需要额外权限，不是 H6 失败。

覆盖的事实：

1. 未授权试用返回 `PLUGIN_AUTHORIZATION_REQUIRED`，不能靠模型参数直接激活。
2. 定义和成功试用后，下一步同一 manager 的动态工具视图立即出现候选工具，无需进程或 AgentLoop 重启。
3. v1 已激活时，v2 需重新试用并绑定；激活 v2 走原子更新，下一次调用记录的 manifest 版本为 v2，旧路由进入清理。
4. 同一候选的激活范围为 session；另一 session 的 inspect 不可见，跨会话激活被拒绝。
5. 修改已登记的 `candidate.py` 或输入契约后，源码/契约绑定拒绝试用或激活；持久化 manager 重建后，已激活路由可恢复为动态工具。
6. 试用失败会把候选标记为 `rejected`；正式调用使用独立的任务预算，不消耗试用预算。
7. 已生成旧 schema 的动态视图不会误调用新版本，旧版本会返回 `TOOL_VERSION_RETIRED`。

## 真实 WebUI + DeepSeek + 杭州 FC 验收

2026-09-12 在 nanobot 原生 WebUI、DeepSeek `deepseek-v4-flash` 和杭州 FC
`provider-default` 配置下完成同一会话的 H6-A：

1. 通过聊天定义 `demo_greeting_v1@1.0.0`，候选保持 `inactive`；源码只做字典取值和字符串拼接。
2. 真实 FC trial 返回 `passed`，`backend=fc-agent-sandbox`、`exit_code=0`、耗时约 3578ms、`cleanup_status=cleaned`，候选变为 `tested` 并绑定 source/input/contract hash。
3. 聊天确认后 activate v1，返回 `state=active`、`generation=1`；下一轮直接调用通过 FC 返回 `{"message":"你好，小明"}`，无 nanobot 重启。
4. 在 v1 运行期间定义并 trial `demo_greeting_v1@2.0.0`，真实 FC 返回 `{"message":"你好，再见，fixture"}`，耗时约 1875ms。
5. activate v2 返回 `generation=2`、`route_commit=committed`、`old_cleanup=complete`；随后真实调用返回 `{"message":"你好，再见，小明"}`、`version=2.0.0`，证明热替换进入实际调用路径。
6. stop v2 成功变为 `inactive`，随后 remove v2 和 v1；最终 inspect 返回 `candidates=[]`，无 active 版本，artifact 按设计保留。
7. 更新工具 schema 后，在新 nanobot WebUI 会话中由 DeepSeek 按
   `[{"field":"name","kind":"string","max_length":64}]` 一次 define
   `h6_schema_probe@1.0.0` 成功；未发生规则字段探测，也未执行 trial/activate/call。

### 真实 H6-F 授权撤销验收

在同一 nanobot WebUI 会话中完成了授权撤销边界验证：先执行 `revoke` 返回
`authorization_revoked`，随后对已定义但未激活的候选发起一次 trial；系统返回
`PLUGIN_AUTHORIZATION_REQUIRED`，`requires_user_confirmation=true`，没有向 FC
派发执行，也没有重试。测试产生的三个临时候选随后在明确确认后全部 remove 成功，
其 artifact 按设计保留。该过程没有重启 nanobot，未产生 ChangeSet 或项目文件修改。

上述热替换和调用期间 nanobot 监听进程 PID 保持为 `29580`，控制面 PID 为 `29524`；候选版本切换没有重启 AgentLoop。FC 试用采用 Host 固定 fixture，模型传入的 trial `input_data` 不作为试用数据执行；正式调用才使用用户提供的结构化参数。全程未改动项目文件，也未把 API Key 写入仓库。

## 尚未证明的边界

- 代码级部分边界仍以受控 Fake runner 验证管理器、路由和状态语义；真实 H6-A 已补充 FC/WebUI 证据，但不等同于所有异常现场均已云端复现。
- 尚未在真实 FC 现场复现授权过期、预算耗尽和云端中断；显式 revoke 后的新 trial 拒绝已在 WebUI 现场复现，这些其余路径已有 H5/H6 自动化契约测试。
- 当前 stop 返回管理操作结果；若 provider 资源仍在排空，最终状态以 operation 查询为准，不应把中间 `draining` 当作已清理。
- H7 的 Loop Driver 替换和跨 Driver checkpoint 尚未开始，H6 只保证动态工具在同一 nanobot Runtime 内热切换。

## 下一步

本批次已经按 ADR-0004 派发同一对 Standards/Spec reviewer；其中 Standards 重试因远程连接失败，Spec 完成但当前接口未返回最终文本，因此不能记为 PASS。真实 H6-A 已通过，但 H6 关闭前仍须取得最终双轴 PASS；在此之前不进入 H7。
