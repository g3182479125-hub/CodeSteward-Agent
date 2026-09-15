# H11 权限模式驱动候选工具自主策略：实现与现场证据

状态：实现与 H11-E 真实 WebUI/FC 证据已完成；ADR-0004 双轴审查仍因两个完成但空输出的
reviewer turn 而阻断，**不得据此写 H11 完成**。最新阻断记录为
`docs/reviews/h11-review-blocked-62540b5.md`。

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
- FC operation receipt 只保留固定、非敏感的 `access_mode`、策略 `decision` 与
  `input_class`；这些字段同时进入 operation hash，因此不能在重试/恢复时被静默替换。
- 默认权限真实输入被拦截时返回
  `CANDIDATE_REAL_INPUT_CONFIRMATION_REQUIRED`，文案是一个合并问题：
  “已验证临时工具；是否允许它在本次任务处理当前允许的数据？”。这不再把 fixture
  trial、activate 和真实调用拆成多个确认问题。

## 已运行检查

在集成 nanobot Python 环境运行：

```powershell
nanobot\.venv\Scripts\python.exe -m pytest devpilot-agent\tests\test_h5_candidate_runner.py devpilot-agent\tests\test_h6_model_plugins.py devpilot-agent\tests\test_h11_candidate_autonomy.py -q
# 63 passed

nanobot\.venv\Scripts\python.exe -m pytest nanobot\tests\agent\test_workspace_scope.py nanobot\nanobot\channels\websocket\tests\test_websocket_channel.py -q
# 186 passed in 11.60s

nanobot\.venv\Scripts\python.exe -m ruff check devpilot-agent\src\devpilot\candidate_autonomy.py devpilot-agent\src\devpilot\model_plugins.py devpilot-agent\src\devpilot\candidate_runner.py devpilot-agent\tests\test_h5_candidate_runner.py devpilot-agent\tests\test_h6_model_plugins.py devpilot-agent\tests\test_h11_candidate_autonomy.py
# All checks passed

git diff --check
# no output
```

`devpilot-agent/tests` 全量套件在本执行环境的单次 30 秒上限内再次输出至 95%，但
没有取得 pytest 最终汇总；这不是通过证据，留待后续环境/审查处理。全量 Pyright
同样有既存的跨项目导入和历史 strict 错误，不能作为本批次通过证据。

## H11-E：真实 WebUI + FC 验收（2026-09-15）

隔离的本地 nanobot 数据副本启动后，使用既有 WebUI 的两个权限菜单和配置好的 FC
候选执行器。会话 `websocket:c92b3f31-8146-4de8-822a-34689e9b07d3` 先选择可信的
`feature-delivery` Profile；现场只使用 `devpilot_model_plugin`，没有读写项目文件。
测试候选均为 `candidate.py::transform(payload)` 的纯 JSON 大写转换，输入规则仅
`value:string`。FC 回执均为 `backend=fc-agent-sandbox`、
`side_effect_count=0`、`permission_violation=false`、`cleanup_status=cleaned`。

| WebUI 模式 | 现场事实 | 结果 |
|---|---|---|
| 完全访问权限 (`full`) | `h11-uppercase@1.0.0` 完成 define → fixture trial → activate → `value=hello` 真实调用 → stop → remove；真实调用输出 `HELLO`，耗时 1747 ms。 | 未输入确认口令；结束 inspect 为 candidates `[]`。 |
| 默认权限 (`restricted`) | `h11-default@1.0.0` 自动 define → fixture trial → activate；首次 `value=default` 被 `PLUGIN_AUTHORIZATION_REQUIRED` 拦截，未派发 FC。随后用户精确输入一次 `确认试用`，同一候选调用输出 `DEFAULT`，耗时 1517 ms，随后 stop/remove。 | 一次确认才允许真实输入；结束 inspect 为 candidates `[]`。 |

fixture trial 使用 Host 固定夹具，输出 `FIXTURE`，并不声称它处理了聊天真实输入。现场
provider network policy 为 `provider-default`：这是 FC 后端报告的策略名称，**不等于**
网络隔离或绝对安全保证。敏感字段、预算、schema、版本绑定和 FC 回执仍是每次调用的
Host 校验红线。

现场还发现确认词必须作为完整用户回合才能由已有 H6 授权绑定识别；带附加说明的自然
语言不是授权。代码现在将“缺一次确认”的返回改成 H11 的单一、可理解提示；UI 不新增
第三个权限控件。

服务重启并加载 `62540b5` 后，在新的隔离 WebUI 会话又复测了这项提示变更：默认权限下
`h11-message@1.0.0` 的 define → fixture trial → activate 自动完成；`value=check` 被
`CANDIDATE_REAL_INPUT_CONFIRMATION_REQUIRED` 拦截，FC 未收到这次真实输入。精确输入一次
`确认试用` 后，FC 输出 `CHECK`（1781 ms、无副作用/越权、清理已完成）；随后 stop →
remove → inspect，候选列表为 `[]`。这同时证明修正后的提示在真实 Gateway/FC 路径生效，
且测试候选没有留下活动路由。

## 待完成

- 在可输出报告的环境中复用同一对 Standards / Spec 审查员，取得两份明确报告。
- 对有效问题批量修复、运行受影响检查、由原审查员增量复核。
