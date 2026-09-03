# P4-04 交付决策实施计划

1. 先写统一报告和决策的公共接口测试，锁定成功、失败、停止、接受和回滚边界。
2. 实现 `DeliveryDecisionWorkflow`，复用控制面状态机和持久 RunEvent，不新增迁移。
3. 增加只读报告 API、用户决策 API 与 nanobot 只读 Tool，并同步注册/Skill contract。
4. 增加脱敏、重复决策、非法状态、回滚 capability 和已有 P4 回归测试。
5. 在 C 盘可写副本运行 E 盘临时目录的全量质量门禁，生成 P4-04 acceptance、manifest、
   verification log 和 review 记录。
6. 提交本地 Git 变更，并给出把提交迁移回 E 盘原仓库的命令；不推送远端。

验收命令：

```text
uv run pytest
uv run ruff check src scripts tests migrations
uv run pyright
uv run python scripts/secret_scan.py
uv run python scripts/check_markdown.py
uv lock --check
```
