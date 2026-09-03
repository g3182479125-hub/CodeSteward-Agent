# P4-03 test-repair 实施计划

## 纵向切片

1. 读取现有 `BugCheckFailureFact`、Run Event 和 Verification 接口，定义可复用的失败
   检查事实与失败指纹 seam，不改变 P4-02 的 bug-fix contract。
2. 先写 G-03 的失败测试，覆盖 Python/Node fixture、失败证据归属、目标文件和 before
   hash，再实现 `TestRepairWorkflow.propose`。
3. 写一轮成功、验证失败和用户拒绝测试，实现 TestRepairWorkflow 的 Apply/Verify
   适配，复用现有控制面能力。
4. 写父子 ChangeSet、三轮预算、重复失败和第四轮拒绝测试，实现 `next_attempt` 与持久
   化恢复。
5. 写 S-11 提示注入、未知 profile、越界文件、非 Fake backend 和 Artifact 失败测试，
   补齐 nanobot Tool、catalog Skill 和必要的控制面入口。
6. 更新 P4-03 acceptance、manifest 和验证记录，运行全量质量门禁，进行只读 Review，
   修复 Review 发现的问题后提交。

## 验收命令

```text
uv run pytest tests/test_p4_test_repair.py
uv run pytest
uv run ruff check src scripts tests migrations
uv run pyright
uv run python scripts/secret_scan.py
uv run python scripts/check_markdown.py
uv lock --check
```

所有命令必须在 E 盘项目环境中运行，测试临时目录指向 E 盘，不能使用真实 API key、真实
远程 backend 或外部网络。
