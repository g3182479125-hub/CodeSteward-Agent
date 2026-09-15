# 2026-09-16：WebUI 流式中文与停止释放交接

## 本次目标

修复两个用户可见问题：

1. 流式过程区域会随 WebUI 英文 locale 混入英文状态、工具活动和模型英文推理摘要。
2. 点击右下角停止后，界面看似结束，但后端任务/会话可能仍被占用；同一会话或新建话题随即发送会长期停在“发送中”或“正在打开新话题”。

## 当前已完成的改动（未完成最终人工网页验收）

- 流式状态统一保留后端的中文规范文本，不再跟随浏览器/WebUI locale 翻译为英文。
- 思考行不再直接露出模型的英文推理片段：中文内容原样显示；英文内容显示为“正在分析下一步”或“已完成本步分析”。路径、工具名、代码和错误码保持原文，便于排查。
- 活动组、文件编辑占位和底部运行状态均固定为中文，例如“思考中”“处理中”“当前：正在等待模型开始处理”。
- 停止按钮不再把 `/stop` 当作普通聊天消息发送；改为 `sendSystemCommand(chatId, "/stop")`。该通道带独立的系统 `turn_id` 与后端回执等待机制，后端会把 `/stop` 作为 priority command 执行，并调用已有的 `_cancel_active_tasks()` 清理活跃任务和排队消息。
- 为上述行为补了前端回归测试：英文 locale 下仍为中文活动文本；停止必须走系统命令通道，且不能再发送普通 `/stop` 聊天消息。

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
# 3 files, 123 passed

npm run lint
# exit 0

npm run build
# TypeScript 校验完成，Vite 已进入 build；终端返回在 `transforming...` 后被会话输出截断，
# 未将其作为“build 完整通过”的证据。
```

`git diff --check` 已通过。

## 尚未完成、不可宣称已修复的部分

- 最终 WebUI 实测尚未完成，因此不能宣称“停止后同一会话和新会话都已经恢复可发送”。
- 尝试用 Codex 内置浏览器新开 `http://127.0.0.1:8765/#/new` 时，浏览器导航在 20 秒后超时；新 tab 停留在 `about:blank`。这与用户报告的“新话题一直打开中”现象一致，但尚未确认是当前 nanobot 进程仍被旧任务占用，还是前端/网关连接没有释放。
- 本批修改尚未进入要求的 Standards/Spec 双轴 review；因用户要求先跨电脑交接，下一次应先完成真实复现和修复闭环，再固定候选并并行审查。

## 下一台电脑的优先执行顺序

1. 拉取当前分支，查看本提交和本交接文档。
2. 用完整启动脚本重启 nanobot，确保加载最新 `webui/dist`，不要仅刷新页面。
3. 打开一个全新 WebUI 会话，发送：

   ```text
   帮我在 E:\\codex-local 里找一下 h10-live-demo 这个目录在哪里，先不要修改任何文件。
   ```

4. 在出现“正在搜索/正在分析”后点击停止；确认没有继续输出，也没有残留“运行中”。
5. 立刻在同一会话发送：`你好，请只回复：取消后可以继续发送。`；必须得到回复且不显示“发送中”。
6. 新建话题，发送：`你好，请只回复：新对话也可以发送。`；必须得到回复。
7. 若仍失败：抓取 WebSocket/Gateway 日志，重点核对 `/stop` 的系统 `turn_id` 是否收到 `message` 或 `turn_end` 回执、`_cancel_active_tasks()` 是否返回、以及 `new_chat` 是否被活动 turn 阻塞；按真实证据继续修复，而不是只改前端状态。
8. 网页验收通过后，执行完整 `npm run build` 并保留 exit 0，再按 `AGENTS.md` / `ADR-0004` 对固定候选做 Standards 与 Spec 并行 review，修复发现后由同一批 reviewer 增量复核。

## 提交边界

仅提交上述 9 个源码/测试文件和本交接文档。以下现有用户本地内容必须保持未提交、未删除：

- `nanobot/webui/package-lock.json`
- 所有 `.pytest-*`、`pytest-*` 临时目录

当前分支：`codex/h11-complete-handoff`。
