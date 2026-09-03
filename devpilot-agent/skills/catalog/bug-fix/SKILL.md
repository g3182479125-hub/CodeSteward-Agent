---
name: bug-fix
description: Turn saved failure evidence into a bounded, reviewable bug-fix ChangeSet proposal.
metadata:
  devpilot:
    version: 0.1.0
    status: draft
    reviewed: false
    allowed_tools:
      - devpilot_inspect_project
      - devpilot_search_code
      - devpilot_read_code
      - devpilot_bug_fix_propose
      - devpilot_propose_changeset
      - devpilot_request_approval
      - devpilot_get_changeset
      - devpilot_get_run_state
    risk_level: R1
    source: catalog
---

# Bug fix

## 适用场景

用户提供了可复现的失败检查或错误现象，希望定位一个最小缺陷、查看 Diff、确认回归验证
方式后再决定是否应用。适用于单个已登记、已信任项目内的有限修复。

## 禁用场景

没有保存失败证据、需求只是新功能、需要批量重构、需要依赖安装、需要任意 Shell、涉及
密钥/凭据/生产仓库、需要扩大批准文件范围，或 P6 Sandbox Gate 尚未通过时的真实自动修复。

## 前置条件

- 项目真实路径位于服务端配置的 allowed roots 且已显式 trust；
- 已读取冻结计划和当前 P4-02 设计；
- 已有 repo-analysis inspect/search/read 证据；
- 已有服务端保存的失败检查 Run，包含目标文件与失败前 hash；
- symptom、expected behavior、非目标、允许文件和回归 profile 已明确。

## 允许 Tool

- `devpilot_inspect_project`、`devpilot_search_code`、`devpilot_read_code`：只读定位和取得事实；
- `devpilot_bug_fix_propose`：绑定失败证据并创建 bug-fix proposal，只读提案；
- `devpilot_propose_changeset`：创建不可变 Diff，只读提案；
- `devpilot_request_approval`：登记用户确认的审批意图，不接收或返回 capability token；
- `devpilot_get_changeset`、`devpilot_get_run_state`：读取服务端事实。

Skill 只能建议上述 Tool。Apply、Verify、Rollback 必须由控制面用户动作完成。

## 执行步骤

1. 读取冻结计划和 P4-02 设计，确认当前节点；
2. 读取失败 Run，核对失败现象、目标文件和 before hash；
3. 用 inspect/search/read 取得相关源码事实，不能把模型猜测当作失败证据；
4. 生成最小修复 proposal，绑定 failure evidence、symptom、expected behavior 和回归 profile；
5. 创建 ChangeSet/Diff，展示允许文件、风险、前后 hash、验证和回滚方式；
6. 等待用户对精确 ChangeSet 回复“执行”，再调用 `devpilot_request_approval`；
7. 控制面执行 Apply 和服务端 allowlisted Verification；P6 前只允许 Fake 自动化验证；
8. 输出成功或失败 Artifact，验证失败不得宣称修复完成。

## 输出契约

proposal 必须包含 `workflow=bug-fix`、failure evidence ids、symptom、expected behavior、
change_set_id、patch_hash、scope_hash、允许文件、回归 profile、停止条件、回滚方式和
`requires_user_confirmation=true`。交付结果必须包含文件级 before/after hash、before/after
manifest、验证 profile、backend、退出码和 Artifact ID。P4-02 的 `react-static` 只代表
Fake 合同验证，不代表真实 React/npm 测试或 Sandbox 隔离已经执行。

## 失败处理

失败证据缺失或不属于目标项目、前置 hash 变化、路径越界、范围漂移、审批缺失、验证失败、
Artifact 写入失败或后端不可用时立即停止。不得改用 nanobot Shell、自动扩大范围、伪造通过
或自动回滚未获用户确认的修改。
