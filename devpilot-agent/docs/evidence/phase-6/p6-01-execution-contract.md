# P6-01：真实入口与执行契约

状态：实现候选，等待本批次双轴 review。

基线：`2cfdc86`。

## 本批次范围

- nanobot 原生 WebUI 的 `执行` 被映射为受信 `apply_verify`；`仅应用` 保留单独
  `apply` 路径，`验证` 与 `撤销` 保持单动作确认。
- Gateway 在本机控制面重新解析浏览器只读的 ChangeSet tuple，并在签名中绑定
  profile、profile version、完整预期验证快照哈希和 check-set 哈希。
- ActionBroker 将组合确认拆成两个内部能力：先 Apply，重新核对同一绑定后再 Verify。
  Apply 已成功而 Verify 未运行/失败时，结果明确保留 `application` 与 `verification`
  两部分；不会伪装为完全成功或自动回滚。
- 控制面只生成固定 allowlist profile 的快照和哈希。`verification_available` 在
  P6-01 固定为 false：FC 尚未经过 P6-02 的认证与证据验证，组合执行会在 Apply 前
  拒绝，不能用 Fake 后端冒充。

## 验证记录

在 nanobot 集成虚拟环境（`E:/codex-local/devflow-agent/nanobot.venv`）运行：

```powershell
pytest ../devpilot-agent/tests/test_p3_execution_boundary.py `
  ../devpilot-agent/tests/test_p5_action_broker.py `
  tests/channels/test_websocket_devpilot_confirmation.py -q
```

结果：`29 passed`。

已运行 Ruff：受影响 DevPilot 与 WebSocket Python 文件均通过。

WebUI 全量 TypeScript build 当前未作为通过证据：工作区既有 `node_modules` 缺少
React 及多个依赖的类型/可执行链接，`npm run build` 在调用 `tsc` 前即失败。这不是
本次一行 confirmation 分支引入的编译错误；P6-03 的真实网页验收前必须在 E 盘用
完整锁定依赖重建该环境，并记录实际 build 结果。

## 未完成与下一步

P6-01 尚需本批次 Standards/Spec 双轴 review 和结论记录。真实 FC 生命周期、取消、
清理、云端通过/失败证据属于 P6-02，未在本节点声明完成。
