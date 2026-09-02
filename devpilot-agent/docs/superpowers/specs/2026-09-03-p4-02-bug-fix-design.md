# P4-02 bug-fix Skill/Workflow 设计

日期：2026-09-03  
前置节点：P4-01，已通过独立 Review  
目标节点：P4-02，`bug-fix` Skill/Workflow 与 G-02  
冻结依据：`E:/codex-local/plans/DEVPILOT_FROZEN_PLAN.md` v2.2.0

## 目标

在现有 DevPilot 控制面上增加一条“已保存失败证据 → 最小修复提案 → 用户批准 →
受控验证 → 前后 hash 与交付报告”的 bug-fix 工作流。G-02 使用最小 React 表单 fixture，
验证只修改批准范围内的文件，失败现象、修复前后内容 hash 和验证结果可追溯。

本节点不是通用 Coding Agent，不增加任意 Shell、依赖安装、自动 commit/push、AG-UI、
React 控制台或 P6 真实 Sandbox。P6 通过前，自动化验证仍只允许 Fake 后端。

## G-02 复现案例

fixture：`tests/fixtures/react-form-project`。表单提交处理器缺少
`event.preventDefault()`，导致提交时浏览器默认刷新页面；期望行为是阻止默认提交并保留
表单状态。fixture 只作为静态、可审查的最小项目，不在本机通过任意 subprocess 执行。

## 设计

### 证据

bug-fix 必须同时拥有：

- repo-analysis 的 inspect/search/read 事实证据；
- 已保存的失败检查 Run，包含 `bug_check.failed` 事实、失败摘要、目标文件和失败前 hash；
- 明确的 symptom、expected behavior、非目标和允许修改文件。

失败证据只作为事实输入，不能由模型临时编造，也不能用成功验证结果替代。

### Workflow

新增 `BugFixWorkflow`，复用现有 `FeatureDeliveryWorkflow` 的 ChangeSet、审批、hash、
Fake Verify、Artifact 和 Rollback 能力，但在 proposal 阶段额外绑定：

- `workflow=bug-fix`；
- `failure_evidence_ids` 及其 hash；
- symptom、expected behavior 和 regression profile；
- G-02 的允许文件范围与停止条件。

Apply、Verify、Rollback 仍由现有控制面完成，nanobot 只得到 proposal、Diff、状态和审批
意图接口，不接触 capability token。

### 结果与失败

成功报告必须包含失败证据引用、patch hash、before/after hash、验证 profile、退出码和
Artifact id。验证失败、前置 hash 变化、超出文件范围、审批缺失或 Artifact 写入失败时，
工作流停止并生成失败 Artifact；不得宣称修复成功，不自动扩大范围或自动回滚。

## 测试与退出条件

第一小步先完成：

1. React fixture 与失败证据格式；
2. bug-fix Skill 草案和静态验证；
3. `BugFixWorkflow.propose` 的证据/范围/契约测试；
4. G-02 Fake 成功、失败、越界和用户拒绝测试。

P4-02 完成前必须再通过全量 pytest、Ruff、Pyright、迁移、Secret、Markdown/link、lock、
manifest 和新的独立只读 Review。Skill 在 Review 通过前保持 `draft`/`reviewed: false`，
不能发布为 active。

## 当前实现顺序

1. 本文与 React fixture；
2. `src/devpilot/workflows.py` 中的 bug-fix proposal/结果模型；
3. 控制面请求和 nanobot proposal adapter；
4. G-02 Golden 与失败 Artifact 证据；
5. 独立 Review 后再更新 Skill 生命周期和 P4-02 manifest。
