# nanobot WebUI integration

本目录保存 DevPilot 对独立 nanobot WebUI 的本地适配代码。nanobot 源码目录没有纳入本仓库，
因此另一台电脑需要先准备 nanobot checkout，再按下面的文件映射应用适配。

## 这次适配做了什么

- 监听 `devpilot_propose_changeset` Tool 的完成事件。
- 从 Tool 的 JSON 结果中提取安全格式的 `change_set_id`。
- 在 nanobot 活动区显示“打开审批页面”按钮。
- 按 `http://127.0.0.1:18801/?change_set_id=<id>` 打开 DevPilot WebUI。
- DevPilot WebUI 启动时自动读取该 Changeset，展示 Diff；批准、Apply、Sandbox Verify 和 Rollback
  仍然只在 DevPilot 审批页面执行。

按钮只是一个深链接，不携带 API Key、审批令牌，也不能绕过人工审批。只接受 loopback 的 HTTP
审批页面地址，避免把本地 Changeset 标识发送到外部域名。

## 应用方式

将本目录下两个源文件复制到 nanobot WebUI 的相同相对路径：

```text
src/components/thread/DevPilotProposalCard.tsx
src/tests/devpilot-proposal-card.test.tsx
```

然后在 nanobot 的
`webui/src/components/thread/AgentActivityCluster.tsx` 中：

1. 在其他 thread component import 附近加入 `DevPilotProposalCards` import。
2. 在 `</ThinkingReasoningShell>` 后加入
   `<DevPilotProposalCards messages={activityMessages} />`。

完整的两行修改见 `AgentActivityCluster.patch`。应用后，在 nanobot `webui` 目录执行：

```powershell
pnpm run test -- src/tests/devpilot-proposal-card.test.tsx
pnpm run build
```

## 联调启动顺序

1. 启动 DevPilot 控制面 `127.0.0.1:18800`，并完成 `alembic upgrade head`。
2. 在 DevPilot `web` 目录启动 WebUI `pnpm dev`，默认地址为 `127.0.0.1:18801`。
3. 重新构建并重启 nanobot Gateway，使其重新加载 `nanobot/web/dist`。
4. 在 nanobot 对话中生成 Changeset，点击“打开审批页面”，确认页面自动加载对应提案。

如果 DevPilot WebUI 使用其他 loopback 地址，可在构建 nanobot WebUI 时设置
`VITE_DEVPILOT_REVIEW_URL`；该值仍必须是 `http://localhost`、`127.0.0.1` 或 `[::1]`。
