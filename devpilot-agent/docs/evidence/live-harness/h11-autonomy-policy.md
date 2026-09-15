# H11 权限模式驱动候选工具自主策略：实现预检

状态：实现候选，ADR-0004 双轴审查因两个完成但空输出的 reviewer turn 而阻断；不得据此写 H11 完成。详见 `docs/reviews/h11-review-blocked-654f340.md`。

基线：`3b37fd0`（H11 冻结计划）。本批次只修改候选工具授权决策；不修改
WebUI 控件、项目 Apply/Verify/Rollback、P8 Skill、FC 执行契约或 nanobot Shell 权限。

## 实现范围

- 新增 `devpilot.candidate_autonomy.CandidateAutonomyPolicy`，以可信工作区模式、
  候选动作、数据类别、试用状态、Profile 与预算返回 `deny`、`confirm`、`auto` 或
  `notice`。
- `model_plugins.py` 仅在 `current_workspace_scope()` 属于 Gateway 绑定的 WebUI
  turn 时采用该策略；缺少可信 WebUI scope 的调用保留 H6 原有明确聊天确认。
- 默认权限自动 define、fixture trial 和 activate；首次真实输入调用保持一次确认。
  该确认绑定 session/task、候选 ID/版本/内容哈希、契约哈希、输入规则哈希及 live
  调用预算。新版本因此不能复用旧版本确认。
- 完全访问权限只会让已试用、Profile 已启用、预算可用且输入未敏感的候选真实调用
  走 `notice` 自动路径。输入 schema 和现有 FC 预校验仍会拒绝敏感数据，且没有本机
  fallback。

## 已运行检查

在集成 nanobot Python 环境运行：

```powershell
nanobot\.venv\Scripts\python.exe -m pytest devpilot-agent\tests\test_h11_candidate_autonomy.py devpilot-agent\tests\test_h6_model_plugins.py -q
# 20 passed

nanobot\.venv\Scripts\python.exe -m pytest nanobot\tests\agent\test_workspace_scope.py -q
# 14 passed

nanobot\.venv\Scripts\python.exe -m ruff check devpilot-agent\src\devpilot devpilot-agent\tests\test_h6_model_plugins.py devpilot-agent\tests\test_h11_candidate_autonomy.py
# All checks passed
```

`devpilot-agent/tests` 全量套件在本执行环境的单次 30 秒上限内已输出至 95%，但
没有取得 pytest 最终汇总；这不是通过证据，留待后续环境/审查处理。全量 Pyright
同样有既存的跨项目导入和历史 strict 错误，不能作为本批次通过证据。

## 待完成

- 在可输出报告的环境中复用同一对 Standards / Spec 审查员，取得两份明确报告。
- 对有效问题批量修复、运行受影响检查、由原审查员增量复核。
- 完成真实 WebUI + 既有 FC 的默认/完全访问各一条演示，保存脱敏证据；没有真实
  证据时不能声称 H11-E 通过。
