# H5：模型候选执行边界交接证据

## 当前状态

- 代码候选：`c115aef`（基线：`0303533`）。
- 状态：已完成一批边界修复和自动化验证；双轴增量 Review 仍为 `NEEDS_FIX`，因此 H5 尚未验收完成，H6 不得开始。
- H1–H4：沿用既有已通过双轴 Review 的候选；不在本记录中重复认证。
- 本节点未新增 WebUI、第二套 Runtime、第二个 Agent Loop 或新数据库。

## 已实现的边界

- 模型候选源码只作为 FC 文件包上传，Host 不 import 候选 Python；固定入口为
  `candidate.py` 的 `transform(payload)`，固定 runner 返回 JSON。
- 单次试运行固定 30 秒，候选输入/输出上限均为 64 KiB，单任务最多 3 次、累计预留
  120 秒；输出超限、异常、超时、非零退出码和清理未确认均返回明确失败。
- 候选请求必须带 Host 作用域的 `CandidateTrialGrant`，校验 session/task/project 和
  输入字段投影；输入会拒绝敏感字段、敏感值和本机路径引用。结果只保存哈希、状态、
  运行标识和脱敏输出，不保存源码或原始输入。
- `trial` 与 `execute` 共用 FC 代理路径；非 FC 后端不能触发本机回退。可选的
  `CandidateTrialStore` 复用现有 `ProjectStore` 的幂等表保存结果和预算，重复 trial
  可重放，不重复调用后端。

## 实际验证

### 自动化检查

在 `devpilot-agent` 目录执行：

```powershell
uv run ruff check src tests
uv run pytest tests/test_h5_candidate_runner.py tests/test_fc_sandbox_contract.py tests/test_p3_execution_boundary.py -q
```

结果：Ruff 通过；定向测试 `62 passed`。

本批次另有持久化重放、重启后预算继承、输入授权、敏感值/本机路径、非 FC 后端、
清理未确认和输出上限测试。集成全量回归应在换机后按交接文档重新执行，不把旧环境结果
替代新环境证据。

### FC 现场记录

此前在杭州 FC Agent Sandbox 的 `provider-default` 模式完成过一次合成候选试运行：

| 字段 | 记录 |
|---|---|
| candidate | `model-greeting` `1.0.0` |
| input | `{name: Alice}` |
| backend | `fc-agent-sandbox` |
| sandbox_id | `sbx-ced52161-61f0-4908-9d52-5a33fffb6201` |
| result | `passed`，返回 `Hello, Alice` |
| exit_code | `0` |
| duration | `2044 ms` |
| cleanup_status | `cleaned` |
| network policy | `provider-default` |

该记录证明 FC 真实生命周期曾跑通，但不是对 `c115aef` 全部验收项的替代证据；
`provider-default` 不代表网络隔离，本项目不能按该记录宣称断网保证。现场测试未上传
API Key、数据库或本机工作区文件。

## Review 结论与未关闭项

本次沿用 H4 的两名 reviewer，对 `0303533...c115aef` 做了增量复核：

- Standards：`NEEDS_FIX`。报告见 `docs/reviews/h5-standards-review-c115aef.md`。
- Spec：`NEEDS_FIX`。报告见 `docs/reviews/h5-spec-review-c115aef.md`。

需要下一批修复的实际问题：

1. 生产路径必须强制注入真实 `ProjectStore`/Host issuer，不能保留内存状态作为生产回退；
   Grant 需要可验证的签发/撤销语义。
2. FC 候选能力需要强校验固定 profile/template，而不是仅依赖可伪造的对象属性。
3. 将 operation 的 project/session、版本、输入哈希、run/sandbox 标识和未知状态恢复
   统一持久化；补充查询/取消/恢复语义，未知结果不自动重试。
4. 预算计数改为跨进程原子预留或 CAS；损坏记录必须 fail-closed，不能静默按零预算恢复。
5. 补充候选源码本机路径检测和对应回归；类型检查问题需单独确定项目可用的 checker 基线。

以上问题关闭并由同一对 reviewer 增量复核通过后，才能更新本文件为 H5 complete，
然后再进入 H6 的 `inspect/define/trial/activate/stop/remove` 模型动作。

## 代码入口

- `src/devpilot/candidate_runner.py`：候选 manifest、授权输入、固定 runner、预算、幂等和结果语义。
- `src/devpilot/fc_sandbox.py`：FC 代理和候选执行可用性标记。
- `tests/test_h5_candidate_runner.py`：H5 候选边界与持久化端口测试。
- `tests/test_fc_sandbox_contract.py`、`tests/test_p3_execution_boundary.py`：FC/上传边界回归。
