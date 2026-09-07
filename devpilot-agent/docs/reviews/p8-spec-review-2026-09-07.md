# P8 Spec Review

日期：2026-09-07
审查轴：Spec；受审 reviewer：Planck（同一 reviewer 增量复核）
原始范围：`bd909f2..3083f60`；最终候选：`c23a896`

## 结论

PASS。最终实现符合 `CURRENT_PLAN`、`SKILL_IMPROVEMENT` 和 ADR-0004 的 P8-01/P8-02 规格；P8-03 明确为可选未选。

## Findings 关闭记录

- SPEC-01：关闭。受控开关默认关闭时不注册、不注入，直接实例化工具也拒绝执行。
- SPEC-02：关闭。固定三类场景 × 两个版本 × 三次 repeat 共 18 次真实调用；每次使用独立临时 fixture，独立环境标识唯一，所有固定断言在 fixture 存活期间执行。证据明确标为 offline/protocol-level。
- SPEC-03：关闭。激活/恢复均有 prepared/committed journal；文件交换窗口崩溃后可在重启时恢复或 fail-closed，且有回归测试。
- 私有根写入旁路：关闭。Dream `WriteFileTool` 与其他写入工具一致阻断私有目录和密钥文件。

## 实际验证

- DevPilot P7/P8/P4/P5 目标集合：退出码 0。
- nanobot context/Dream/P8 guard/task profile/skill command 集合：`117 passed`，退出码 0。
- Ruff、secret scan、`git diff --check`：均通过。

## 限制与下一步

未执行 live LLM/provider E2E，未重审历史 P6/P7。P8-03 定时批处理没有实现、没有验收、没有启用；后续只有在用户明确选择该扩展后才能立项。
