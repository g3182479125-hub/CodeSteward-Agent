# ShopSphere Personal-Agent Bug-Fix Challenge 实施计划

设计依据：`docs/superpowers/specs/2026-09-23-shopsphere-agent-challenge-design.md`

## 目标与边界

在 `E:\codex-local\agent-demo-projects\shopsphere` 建立 ShopSphere 固定版本的隔离副本，验证上游基线；仅在 `agent-challenge` 分支加入一个可重复触发的“并发取消同一订单导致库存重复返还”缺陷和回归用例；之后把该副本交给用户的 DevPilot Personal Agent，通过正常工具调用、ChangeSet、Apply、Verify 路径修复。上游、DevPilot 源码、真实数据和凭据均不改动，不提交或推送挑战项目。

## 阶段 1：隔离获取与来源校验

1. 检查目标父目录和目标路径；若目标路径已有内容，停止并报告，不覆盖。
2. 克隆公开仓库到目标路径，检出并固定到设计记录的上游 SHA `1bdb758791b2b28a8a2548a3a18f9e034330e25f`。
3. 验证 HEAD、远端 URL、工作区状态及 MIT LICENSE；建立本地 `upstream-baseline` 保护引用，再从其创建 `agent-challenge` 分支。
4. 记录项目结构、测试入口、Python 版本需求、运行依赖和数据/上传/日志目录；确认没有启动真实邮件或支付集成。

**验收门槛：** 来源 SHA 精确匹配；基线引用不可变；挑战分支与基线起点相同；任何路径边界或目录状态不明确即停止。

## 阶段 2：上游基线测试

1. 阅读项目 README、`AGENTS.md`（若存在）、订单/库存实现和关联测试。
2. 按仓库说明安装最小依赖，使用隔离 Python 环境；SQLite、上传、日志和临时文件只落在挑战目录下的 disposable state。
3. 执行上游完整测试命令 `python -m unittest discover -s tests -t .`，保存命令、退出码、通过/失败数、耗时及失败摘要。
4. 若基线不通过，先诊断环境与上游既有失败；不得把基线失败归因于后续注入的缺陷，也不得继续注入。

**验收门槛：** 基线完整测试通过；否则停在此阶段，报告具体阻塞并请求决定。

## 阶段 3：设计并证明单一缺陷

1. 追踪管理员取消请求到订单状态、库存及 variant 汇总更新的完整路径，确认涉及的真实文件、事务边界和 SQLite 连接配置。
2. 先在挑战分支编写一个同步屏障型并发回归测试：两个请求都到达状态读取边界后再继续；断言最终订单只取消一次、库存只恢复一次、variant 汇总一致、取消订单不计入相关报表。
3. 以测试结果指导最小注入：保留既有路径，仅移除/破坏足以导致重复库存返还的必要保护；不改无关业务逻辑。
4. 连续运行新回归多轮并执行既有完整测试。新回归必须稳定失败，既有测试必须保持全绿。
5. 若 SQLite 事务语义使同步测试不能可靠复现，不得加入 timing-sleep 或 flaky 测试，也不得擅自替换成另一类缺陷；停止并向用户报告证据与替代方案。

**验收门槛：** 回归测试可重复红、原测试全绿，缺陷现象仅限设计范围；否则不把副本交给 Agent。

## 阶段 4：以 DevPilot Personal Agent 修复

1. 确认个人 Agent 服务运行状态与实际项目绑定，工作目录精确为挑战副本；本次允许路径仅包含挑战副本，不改永久 AllowedRoots。
2. 确认本次运行只使用公开 ShopSphere 代码和合成测试数据；不允许挂载真实业务数据库、密钥、用户资料或其他仓库。
3. 通过聊天自然语言请求 Agent 调查“管理员并发取消同一订单后库存偶尔增加两次”，先复现和解释，再给最小 ChangeSet；不告诉它具体修复算法或直接写补丁。
4. 检查 Agent 的诊断、工具轨迹、ChangeSet diff 和目标文件范围；只允许订单取消/库存一致性所必需的改动。若越界、测试命令不可信或改动无关，拒绝 Apply 并报告。
5. 按系统正常审批流程批准此 ChangeSet，由 DevPilot 完成 Apply；随后运行 Agent 的 Verify。不得手工代替 Agent 修复。

**验收门槛：** Agent 确实产出可审计 ChangeSet，Apply 和 Verify 记录对应同一目标项目与修订；否则不能宣称 Agent 修复成功。

## 阶段 5：独立复验和收尾

1. 对新增并发回归重复运行至少 20 次，确认全通过且无超时/挂起。
2. 运行完整上游测试集；记录准确的命令、退出码、测试总数和耗时。
3. 独立检查订单终态、商品库存、variant 汇总和取消订单报表排除情况；保存可复核的合成数据断言。
4. 审阅最终 diff、`git diff --check`、仓库状态和基线引用，确认仅含有意挑战与 Agent 修复内容；清理仅属于本次挑战的 disposable 数据，不删除源代码或用户文件。
5. 整理交付记录：仓库 URL、固定 SHA、项目本地路径、基线与故障注入测试结果、Agent Run/ChangeSet/Verify 标识、最终测试结果、未覆盖风险和复现命令。

**最终验收：** 新增回归连续通过；全量测试通过；业务状态一致；Agent 修改链路可审计；上游和 DevPilot 仓库没有被挑战工作污染；无未经请求的 push/发布。

## 失败处理与停止条件

- 目标目录非空、来源 SHA 不匹配、许可证缺失、基线测试不通过：停止，不覆盖、不注入。
- 并发缺陷无法稳定复现：停止，不交付不稳定挑战，不自行换题。
- Personal Agent 不可用或授权配置不能严格限定到挑战项目：先检查本地连接和作用域，不扩展全局授权；仍不可用则保留可复现挑战并清楚报告，不能声称完成 Agent 修复。
- Agent 改动越界、Apply/Verify 回执不完整或任何测试失败：不宣称通过；保留证据并分析下一步。
- 不执行 upstream push、challenge push、GitHub fork、部署或真实交易。

## 执行记录（2026-09-23）

- 仓库固定于 `1bdb758791b2b28a8a2548a3a18f9e034330e25f`；本地 `upstream-baseline` tag 与 `agent-challenge` branch 均从该 SHA 建立。
- 基线：`python -m unittest discover -s tests -t .` → `222 tests, OK`（166.066s）。
- 注入方式：新增 `TestConcurrentOrderCancellation.test_two_cancellations_restore_variant_stock_only_once`，在两个真实管理员 HTTP 请求读取订单状态后使用 `threading.Barrier` 同步。修复前 20/20 次稳定因 variant 库存多返还 2 件而失败；修复后 20/20 次通过。
- Personal Agent 调用 `devpilot_inspect_project` 和 `devpilot_test_run`。Run `a6a0b9bb-abff-4cab-88c2-3c2d8defde6d` 在 prepare 阶段以 `FC_DEPENDENCY_INSTALL_NETWORK_DENIED` 结束，未执行测试；当前测试工具仅接受 `python-pytest` / `node-test`，此仓库使用标准库 `unittest`。运行时 Full Access 会话也未注册本地 `exec` 工具。
- Agent 提案 ChangeSet `e5607f49-f908-4c6a-b352-0a5d44ef5c07` 为 planned 预览。正式 Apply 流程分别遇到 `PROJECT_TRUST_REQUIRED` 和 `APPROVAL_BROKER_UNAVAILABLE`；故未对该 ChangeSet 执行 Apply/Verify。
- 经用户本轮授权，Agent 使用 `apply_patch` 仅修改挑战副本的 `shop/orders.py`：先以 `UPDATE ... WHERE status = ?` 原子认领状态流转，未认领成功则不回补库存。原 planned ChangeSet 的 `before_sha256` 因此已过期，不得 Apply。
- 修复后本地独立验证：并发回归 `20/20 passed`；全量 `python -m unittest discover -s tests -t .` → `223 tests, OK`（179.937s）；`git diff --check` 通过。输出中有上游已有的 `ResourceWarning`（测试未关闭邮件/上传文件句柄），未影响退出码。
- 挑战副本源代码差异仅 `shop/orders.py` 与 `tests/test_shop.py`；未提交、未推送。Personal Agent 的项目 trust、ChangeSet approval broker、FC 网络依赖安装以及 unittest Profile 的缺口仍未解决，故“完整 DevPilot ChangeSet→Apply→Verify 闭环”尚未验收。
- 2026-09-24 使用新 local-first `ProjectTestRunner` 对 Git 基线 + 上述两处挑战改动的临时干净副本执行 `python-unittest`。Run `e896cf25-6fb1-46a9-b018-4419c1011726`：本地依赖准备成功，`execution_location=local`、无 Sandbox ID、清理状态 `cleaned`；但最终状态 `unknown`、错误 `TEST_RECEIPT_INCOMPLETE`，退出码未进入可验收回执。未修改 ShopSphere 源工作区，也未调用 FC。初步根因是 verbose unittest 输出超过 16 KiB 回执上限导致截断；下一步先验证该诊断并修正固定命令/回执交互，再通过当前集成服务由 Personal Agent 重测。
- 2026-09-24 DevPilot 已将 `python-unittest` 固定命令从 `python -m unittest discover -v` 收紧为 `python -m unittest discover`，并同步更新三处命令契约；输出截断仍然拒绝，不放宽安全边界。新增 500-case 大型通过套件回归通过，DevPilot 全量测试通过。当前电脑不存在约定的 ShopSphere 挑战副本，故尚未声称真实项目级 E2E 或 Personal Agent ChangeSet→Apply→Verify 已完成。
