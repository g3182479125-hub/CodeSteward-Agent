# ShopSphere 故障录制项目交接说明

这个目录是给 Personal Agent 录制 Bug-fix 闭环使用的故障副本。
它与同级的 `shopsphere-bugfix` 分开：

- `shopsphere-bugfix`：修复后的基线，223 个测试全部通过；
- `shopsphere-bugfix-challenge`：故意保留并发取消故障，供 Agent 先复现再修复。

## 故障是什么

管理员同时取消同一个订单时，两个请求都可能读到订单仍处于可取消状态，随后都把订单标记为已取消并恢复库存。结果是同一批商品库存可能被返还两次。

项目中保留了确定性并发回归测试：

```powershell
python -m pytest -q
```

还原后的预期结果是 `223` 个测试中只有并发取消回归失败；失败现象是两个请求都被判定为成功，测试报告 `AssertionError: 2 != 1`。这是录制开始前应看到的故障状态。

## 录制时使用的项目路径

如果仓库位于 `D:\devflow-agent`，项目路径是：

```text
D:\devflow-agent\training-projects\shopsphere-bugfix-challenge
```

当前电脑的实际路径是：

```text
D:\Users\tianmin.li\.codex\worktrees\6401\remote-integrate\training-projects\shopsphere-bugfix-challenge
```

## 给 Personal Agent 的第一句话

在 WebUI 中选择 `bug-fix` Profile，然后发送：

> 我发现这个商城里，管理员同时取消同一个订单时，库存有时候会被加两次。项目路径是 `D:\devflow-agent\training-projects\shopsphere-bugfix-challenge`。请先检查并复现问题，不要直接修改代码。

之后按正常流程执行：复现失败 → 说明根因 → 生成 ChangeSet → 人工确认 → Apply → Verify → 再跑完整测试。

## 不要上传的内容

不要提交或上传 `.venv/`、`instance/`、`.nanobot/`、SQLite 数据库、缓存、日志、上传文件、模型配置和任何密钥。这个目录只包含源码、测试和文档。
