# FC pytest profile 变更 Spec 复核

## 复核对象

- 固定点：`92884b9`
- 初始候选：`65cf72c`
- 增量候选：`dd7a253`
- 规格：`CURRENT_PLAN.md` 第 0/6/7 节、ADR-0004、ADR-0008。

## 初始复核与修复

初始复核发现 P6 真实证据清单仍只记录旧的三个 FC profile，且没有控制面到
`python-regression` 的集成测试。随后在 `dd7a253` 中：

- 更新 P6 profile 集合、北京 FC 配置指纹和当前候选身份；
- 增加 `p6-05-pytest-regression-evidence-2026-09-17.md`，记录基线失败、Apply、
  FC `5 passed`、Sandbox、回滚和恢复哈希；
- 增加控制面→FC→pytest bootstrap 的集成测试；
- 修正 `DEVPILOT_FC_INSTALL_PYTEST=0` 的文档边界。

## 增量结论

在上述修复后，Spec reviewer 确认：

- `python-regression` 已列入 FC 能力声明，并与固定 pytest 命令和 bootstrap 一致；
- Apply/Verify/Rollback 的正式闭环证据可追溯，且验证结果明确为 `provider-default`；
- 集成测试和 P6-05 记录覆盖本次能力变化；
- 未发现 scope creep 或实现与规格不一致的问题。

**PASS（记录文件补齐后）。** 网络隔离仍按规格标记为未证明，不改变本结论。
