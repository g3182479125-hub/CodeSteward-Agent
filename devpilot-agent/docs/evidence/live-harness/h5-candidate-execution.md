# H5：模型候选执行边界交接证据

## 当前状态

- 代码候选：`e08299e`（本轮增量基线：`9d216c7`；远端 H5 起始检查点：`c115aef`）。
- 状态：H5 已完成验收；同一对 Standards / Spec reviewer 的增量 Review 均为 `PASS`。
  H1–H5 已完成，H6–H10 尚未开始。
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
- `trial` 与 `execute` 共用 FC 代理路径；非 FC 后端不能触发本机回退。生产工厂只在
  明确启用模型候选、注入真实 `ProjectStore`、具体 `FCSandboxBackend` 和 Host 签发器时
  创建 runner；缺失配置直接拒绝，不回退到内存状态或宿主执行。
- `ProjectStore` 的幂等表同时保存完整 operation envelope、结果、run/sandbox 标识和
  预算租约。操作哈希、任务/项目/session 作用域和结果 schema 不匹配时失败关闭；重复
  trial 可查询/重放确定结果，但不会再次调用 FC。
- 预算使用 SQLite `BEGIN IMMEDIATE` 原子预留与 fencing lease；取消、恢复和进程重启
  都沿用同一 operation，不自动重放未知调用。FC 取消无本地句柄时先用持久 sandbox ID
  重连确认，再返回确定的 cancelled/recovered/unknown。

## 实际验证

### 自动化检查

在 `devpilot-agent` 目录执行：

```powershell
uv run ruff check src tests
uv run pytest tests/test_h5_candidate_runner.py tests/test_fc_sandbox_contract.py tests/test_p3_execution_boundary.py -q
```

结果：Ruff 通过；H5/FC/P2B 定向测试全部通过；全量回归共收集 320 项，
`317 passed, 3 skipped`。

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

该记录证明 FC 真实生命周期曾跑通，但不是对 `e08299e` 全部验收项的替代证据；
`provider-default` 不代表网络隔离，本项目不能按该记录宣称断网保证。现场测试未上传
API Key、数据库或本机工作区文件。

## Review 结论与关闭项

同一对 H4 reviewer 对本轮增量 `9d216c7...e08299e` 做了复核：

- Standards：`PASS`，报告见 `docs/reviews/h5-standards-review-e08299e.md`。
- Spec：`PASS`，报告见 `docs/reviews/h5-spec-review-e08299e.md`。

本轮关闭了首轮发现的生产注入、可验证 Grant、固定 FC 契约、完整 operation 恢复、
原子预算租约、损坏记录 fail-closed 和候选源码本机路径检查问题。H5 的执行边界已
达到计划退出条件；H6 才开始开放模型侧 `inspect/define/trial/activate/stop/remove`，
当前没有 H6 产品动作或 WebUI 管理入口。

## 代码入口

- `src/devpilot/candidate_runner.py`：候选 manifest、授权输入、固定 runner、预算、幂等和结果语义。
- `src/devpilot/fc_sandbox.py`：FC 代理和候选执行可用性标记。
- `tests/test_h5_candidate_runner.py`：H5 候选边界与持久化端口测试。
- `tests/test_fc_sandbox_contract.py`、`tests/test_p3_execution_boundary.py`：FC/上传边界回归。
