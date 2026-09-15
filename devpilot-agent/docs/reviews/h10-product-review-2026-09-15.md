# H10 产品交付评审

日期：2026-09-15。评审方式：只读；依据
`h10-product-review-plan-2026-09-15.md`、实际源码/测试、控制面回执和原生 WebUI 轨迹。

## 决策：APPROVE

H10-A、H10-B、H10-C 和 H10-D 的退出条件均有可追溯证据；没有发现 blocker 或
must-fix-before-next-node。

## 证据核对

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| H10-A 真实网页与 FC | PASS | chat `be3cfa0e-039f-4966-b5c8-c2715542162a`；ChangeSet `bf33c510-de9f-47ac-93cf-194669c42382`；Run `9b9b6e19-78a5-49f9-b3f2-24fcd28bb053`；真实 `fc-agent-sandbox`、`code-interpreter-v1`、`cn-beijing`、`python-syntax`、exit code 0；控制面 `r1.rollback` 后 `reverted`；WebUI `read_code` 与独立哈希均恢复到 baseline |
| H10-B 回归 | PASS | 聚焦 pytest、Ruff、WebUI lint/build 和 `git diff --check` 均通过 |
| H10-C 恢复 | PASS | 隔离数据库迁移及三类缺失/不兼容/未结算恢复分支已有可复跑证据；交接包含原地 `git pull --ff-only` |
| H10-D 文档 | PASS | `h10-delivery.md` 分开记录真实 FC、历史 WebUI 和 fake；明确 provider-default、回滚报告字段语义、秘密保护和未实现边界 |

## 关键安全核对

- 未确认阶段没有项目文件写入；“执行”只走 `apply_verify`，完整 tuple 和固定
  `python-syntax` profile 由控制面绑定。
- “撤销”先由模型登记 `approval_pending`，未返回 token；随后才由控制面用户动作签发
  `r1.rollback` 并执行回滚。模型/Skill 没有签发、读取或传播 token 的能力。
- 回滚后的 `main.py` 由同一 WebUI 会话只读读取，内容为 `Decimal("10")`，SHA-256 与
  `before_manifest`/`rollback_manifest` 一致。`delivery-report` 中保留应用后
  `after_manifest` 的事实已在文档中明确，未被当作回滚后磁盘哈希。
- 真实 FC 使用的是既有用户配置，但本次证据、日志和提交均未包含任何密钥；
  `provider-default` 仅作为实际策略标签，不宣称网络隔离。

## 观察项（不阻断）

1. 真实 FC 运行时逐次创建短生命周期沙箱，耗时 1417ms；这属于已记录的 H10 成本事实，
   不外推为生产 SLA。
2. `nanobot/.pytest-h8-current/` 和 `nanobot/MagicMock/` 是本轮之前就存在的未跟踪目录；
   评审未修改或提交它们，换机前由操作者按需处理。
3. 本轮原 Standards/Spec reviewer 线程均显示完成，但 Codex 线程接口没有返回最新正文；
   本报告不把空响应当作 PASS，而是以实际控制面、WebUI、测试和文档证据完成独立只读
   产品门禁。既有 H10 增量 review 文档的可见结论仍为 PASS。
