# P4-03 test-repair Skill/Workflow 设计

日期：2026-09-03  
前置节点：P4-02，已通过独立 Review  
目标节点：P4-03，`test-repair`、三轮预算、S-11/S-12 与 G-03

## 目标

在 P4-02 的保存失败证据、ChangeSet、审批、Fake Verify 和 Artifact 能力之上，增加一条
处理失败检查的受控修复流程。流程支持 Python 和 Node fixture，能把每次尝试记录为独立的
子 ChangeSet，并在失败重复、范围漂移、后端不合格或预算耗尽时停止。

本节点不运行真实 Python、Node、npm、Shell 或网络命令，不安装依赖，不自动批准、不自动
回滚，不扩大允许文件范围。P6 通过前，验证仍然只能由已登记的 Fake backend 完成。

## G-03 场景

- Python fixture：`tests/fixtures/python-project`，使用服务端 allowlisted 的
  `python-run` 验证合同。
- Node fixture：`tests/fixtures/node-project`，使用服务端 allowlisted 的
  `node-syntax` 验证合同。
- 失败检查必须是服务端保存的 `test_check.failed` 事实，包含项目归属、目标文件和失败前
  hash，不能由模型临时编造。

## 设计

### TestRepairWorkflow 模块

新增 `TestRepairWorkflow` 作为 P4-03 的深模块，外部接口只暴露：

- `propose`：根据已保存失败事实创建首个测试修复提案；
- `apply`：沿用现有控制面审批和 Apply 能力；
- `verify`：沿用现有 allowlisted Verification 和 Artifact 能力；
- `next_attempt`：在上一轮失败且仍满足预算时创建一个独立的子 ChangeSet。

它复用 P4-02 的 `ChangeSet`、`PatchPlan`、审批、哈希、Run Event、Artifact 和 Fake
ExecutionBackend，不复制已有状态机。轮次和失败指纹只属于该模块的内部契约，并持久化在
ChangeSet contract 与 Run events 中，避免引入不必要的数据库迁移。

### 修复尝试契约

每个尝试绑定：

- `workflow=test-repair`；
- `parent_change_set_id`、`attempt`、`max_attempts=3`；
- `failure_evidence_ids`、失败事实 hash 和失败指纹；
- 分析证据、症状、期望行为、允许文件和验证 profile；
- patch hash、scope hash、before hash、停止条件和回滚方式。

每个尝试都必须重新审批。`next_attempt` 只能生成提案，不得 Apply 或 Verify，也不能
自动替用户选择下一轮。

### 预算与停止规则

1. 第 1、2、3 轮可以创建子 ChangeSet；
2. 第 4 轮请求稳定拒绝，并生成真实失败报告；
3. 同一个失败指纹再次出现时立即停止，不再生成下一轮；
4. 失败证据变化、before hash 变化、范围漂移、审批缺失、用户拒绝或 Fake backend
   不可用时立即停止；
5. 停止时保留最后一个已验证状态，不自动回滚已批准的修改。

### 安全边界

提示注入只是失败事实或模型输出中的不可信数据。它不能改变 workflow、允许工具、允许
文件、验证 profile、轮次预算或后端。Tool 和 Policy 在确定性代码中拒绝第四轮、未知
profile、越界文件、任意命令和非 Fake 执行。

## 测试与退出条件

先在公共接口的 seam 上用测试锁定以下行为，再逐个实现：

- G-03 Python/Node 失败检查到提案的证据、文件和 hash 绑定；
- 成功修复、验证失败、用户拒绝、Artifact 失败和后端不可用；
- 每轮独立 ChangeSet，父子关系和尝试次数持久化；
- 重复失败停止，第四轮和累计预算超限在写入前拒绝；
- S-11 提示注入不能扩大权限或触发下载、执行；
- nanobot Tool、Skill 和控制面 contract 一致；
- P4-01/P4-02 全量回归保持通过。

通过全量 pytest、Ruff、Pyright、Secret、Markdown/link、迁移、lock、manifest 和独立
只读 Review 后，P4-03 才能标记为完成。真实代码执行、真实 Sandbox、自动修复循环和
自动回滚继续留给 P6 或后续节点。
