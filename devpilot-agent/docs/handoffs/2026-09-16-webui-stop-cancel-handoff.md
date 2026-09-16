# 2026-09-16：WebUI 流式中文与停止释放交接

## 本次目标

修复两个用户可见问题：

1. 流式过程区域会随 WebUI 英文 locale 混入英文状态、工具活动和模型英文推理摘要。
2. 点击右下角停止后，界面看似结束，但后端任务/会话可能仍被占用；同一会话或新建话题随即发送会长期停在“发送中”或“正在打开新话题”。

## 已完成的改动

- 流式状态统一保留后端的中文规范文本，不再跟随浏览器/WebUI locale 翻译为英文。
- 思考行不再直接露出模型的英文推理片段：中文内容原样显示；英文内容显示为“正在分析下一步”或“已完成本步分析”。路径、工具名、代码和错误码保持原文，便于排查。
- 活动组、文件编辑占位和底部运行状态均固定为中文，例如“思考中”“处理中”“当前：正在等待模型开始处理”。
- 停止按钮不再把 `/stop` 当作普通聊天消息发送；改为 `sendSystemCommand(chatId, "/stop")`。该通道带独立的系统 `turn_id` 与后端回执等待机制，后端会把 `/stop` 作为 priority command 执行，并调用已有的 `_cancel_active_tasks()` 清理活跃任务和排队消息。
- 为上述行为补了前端回归测试：英文 locale 下仍为中文活动文本；停止必须走系统命令通道，且不能再发送普通 `/stop` 聊天消息。
- 后续修复 `b87781c` 将本地流状态释放移到 `/stop` 系统命令收到同一 `turn_id` 的后端回执之后。回执未到、超时、socket 关闭或后端拒绝时，界面保持当前运行态，不能让下一条用户消息与仍在取消中的任务竞争。

涉及文件：

- `nanobot/webui/src/hooks/useNanobotStream.ts`
- `nanobot/webui/src/components/thread/AgentActivityCluster.tsx`
- `nanobot/webui/src/components/thread/ThreadComposer.tsx`
- `nanobot/webui/src/components/thread/activity/{activity-copy,ReasoningRow,FileEditRow}.tsx`
- 对应 3 个前端测试文件。

## 已完成验证

在 `nanobot/webui` 已通过：

```powershell
npm run test -- --run src/tests/activity-copy.test.ts src/tests/agent-activity-cluster.test.tsx src/tests/useNanobotStream.test.tsx
# 3 files, 124 passed

npm run lint
# exit 0

npm run build
# exit 0
```

```powershell
cd ..
.venv\Scripts\python.exe -m pytest tests/command/test_stop_pending_queue.py tests/agent/test_loop_direct_websocket_status.py -q
# 7 passed
```

`git diff --check` 已通过；本地 WebUI 已以最新生产 build 成功加载。

候选 `b87781c` 的 Standards 与 Spec 双轴复核均为 PASS：

- `docs/reviews/webui-stop-cancel-standards-review-b87781c.md`
- `docs/reviews/webui-stop-cancel-spec-review-b87781c.md`

## 已完成的真实模型现场验收（2026-09-16）

用户在本机配置 DeepSeek 后，通过本地 WebUI 与真实模型完成以下闭环：

1. 模型能成功流式输出；此前错误的 `anthropic/claude-opus-4-5` 模型名已改为该 Provider 支持的 DeepSeek 模型。
2. 发送“请写一篇至少五千字的科幻短篇小说，分成多个章节并持续展开情节。”；确认“停止响应”按钮出现后立即点击。
3. 停止后，同一会话立刻发送“停止后的新消息：只回复‘验收通过’。”
4. 页面显示该用户消息及模型回复“验收通过”，没有卡在“发送中”或被旧任务占用。

这证明真实 Provider 下的停止→后端回执→同会话立即重新发送链路已闭环。此前的自动化测试继续覆盖前端竞态和后端 priority `/stop`/会话锁契约。

本轮未重复新建话题验收；其竞态边界由已通过的前端回归与后端排队/会话锁测试覆盖。若未来现场出现新会话阻塞，应抓取 WebSocket/Gateway 日志，核对 `/stop` 的系统 `turn_id` 回执、`_cancel_active_tasks()` 返回及 `new_chat` 活动 turn 状态后再修复。

## 提交边界

本次增量提交 `b87781c` 仅包含停止回执门禁的 2 个源码/测试文件；其后的文档提交记录本交接和双轴结论。以下现有用户本地内容必须保持未提交、未删除：

- `nanobot/webui/package-lock.json`
- 所有 `.pytest-*`、`pytest-*` 临时目录

当前分支：`codex/h11-complete-handoff`。
