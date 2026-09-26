# ShopSphere 录制与修复交接（2026-09-26）

## 当前状态与路径

主仓库中的 `training-projects/shopsphere-bugfix-challenge` **已由 Personal Agent 修复**，不是故障起点。不要为了重新录制把它回退。当前电脑的修复版：

```text
E:\codex-local\nanobot-devpilot-agent\training-projects\shopsphere-bugfix-challenge
```

可复现故障的原始副本保存在独立 Git worktree，固定于提交 `c001238cfbd54e90a01ee7a129bf7b199f084873`：

```text
E:\codex-local\shopsphere-recording-baseline-20260926\training-projects\shopsphere-bugfix-challenge
```

这个 worktree 只存在于当前电脑。换电脑后可从仓库建立独立故障副本，目标目录需事先不存在：

```powershell
git worktree add --detach --no-checkout <独立目录> c001238cfbd54e90a01ee7a129bf7b199f084873
git -C <独立目录> sparse-checkout set training-projects/shopsphere-bugfix-challenge
git -C <独立目录> reset --mixed HEAD
git -C <独立目录> restore --worktree -- training-projects/shopsphere-bugfix-challenge
```

查看当前修复效果用主仓库里的项目；从故障开始录制，则让 Agent 操作独立故障副本。新副本需在 WebUI 重新选择和 trust。

## 故障与 Personal Agent 实测证据

管理员并发取消同一订单时，旧实现只按订单 ID 更新状态，两个请求都可能成功并重复恢复库存。确定性用例：

```powershell
python -m pytest -q tests/test_shop.py::TestConcurrentOrderCancellation::test_two_cancellations_restore_variant_stock_only_once
```

- 故障 worktree 上运行该用例：退出码 1，`AssertionError: 2 != 1`（2026-09-26 本机复现）。
- Personal Agent 生成 ChangeSet `539d899d-8807-41f9-9ce4-4dd6a664d0e8`，只修改 `shop/orders.py`；经单独的“执行”确认，通过 `devpilot_confirmed_action(action=apply_verify)` 应用。ChangeSet 与 verification 状态均为 `verified`。
- 修复方式：带原状态条件的原子 `UPDATE`，以受影响行数判定竞争胜者；失败的一方不返还库存。
- 修复后 `shop/orders.py` 的 SHA-256 为 `8424b5f5150951e958158d501fa18dec0d5abf6d59cb4d45ea8b491a08006d32`，与 ChangeSet `after_manifest` 一致。故障前清单哈希为 `8a901478486e0e483442b79ce11e3f40749a8237171c0f1d1679646fadc701a3`。
- 修复版上同一确定性用例：`1 passed`。
- Personal Agent 在修复版上通过 `devpilot_test_run` 执行 `python -m pytest -q`：Run `4bf802e2-b084-40b1-bb0c-9c9e54429d19`，`execution_location=local`，`exit_code=0`，`223 passed in 567.06s`，0 failed / 0 skipped。这是 Agent 的正式项目测试，不是手工 pytest 冒充 Agent 测试。

第一次应用后的全量 Run `bb411d6f-aa9d-4aee-ab00-8a90ad3f59a1` 曾是 `1 failed, 222 passed`。剩余失败并非并发修复失效，而是 DevPilot 测试快照把 `forgot_password.html` / `reset_password.html` 误判为敏感文件并排除，导致密码重置节流用例的 429 页面渲染变成 500。控制层随后仅对白名单内两个公开模板放行，相关拒绝敏感文件的规则仍保留。平台修复见提交 `7146b0e`，收紧规则见 `6457b41`；以新全量 Run 的 223/223 为准。平台修复由 Codex 完成，不能算作 Personal Agent 自动修复。

## 录制顺序

1. 启动集成 Harness，确认项目允许根目录与故障副本路径匹配；在 WebUI 选择 `bug-fix` Profile 并信任该项目。
2. 对 Personal Agent 说：

   > 我发现这个商城有个问题：管理员同时取消同一个订单，库存有时候会被加两次。项目就在 `<故障副本的绝对路径>`。你先帮我检查并复现，先别改代码。

3. 看 Agent 的项目分析、失败用例和 Run ID；让它解释根因，并提出只针对 `shop/orders.py` 的 ChangeSet。检查 Diff、文件范围后再明确回复“执行”。
4. 看 Apply / Verify 回执，检查 ChangeSet 状态和文件哈希；让 Agent 再运行 `python -m pytest -q`，等到终态并核对 223/223、退出码 0。完整测试约需 9–10 分钟，不要因等待时间提前当成超时。

本次 ChangeSet 与 Run ID 保存在当前电脑的 Control Plane 状态中；换电脑后无法直接复用，必须在新故障副本上重新走流程。项目测试在本地以当前 Windows 用户权限运行，FC Sandbox 保持关闭；只对可信副本运行。

不要上传 `.venv/`、`instance/`、`.nanobot/`、数据库、缓存、日志、会话、模型配置和任何密钥。
