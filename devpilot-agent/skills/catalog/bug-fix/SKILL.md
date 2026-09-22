---
name: bug-fix
description: Turn saved failure evidence into a bounded, reviewable bug-fix ChangeSet proposal.
metadata:
  devpilot:
    version: 0.1.0
    status: active
    reviewed: true
    allowed_tools:
      - devpilot_inspect_project
      - devpilot_search_code
      - devpilot_read_code
      - devpilot_test_run
      - devpilot_bug_fix_propose
      - devpilot_propose_changeset
      - devpilot_request_approval
      - devpilot_get_changeset
      - devpilot_get_run_state
      - devpilot_get_delivery_report
      - devpilot_watch_run
      - devpilot_confirmed_action
      - devpilot_create_temporary_tool
      - devpilot_validate_temporary_tool
      - devpilot_activate_temporary_tool
      - devpilot_ensure_temporary_tool
      - devpilot_persist_temporary_tool
      - devpilot_discard_temporary_tools
    risk_level: R1
    source: catalog
---

# Bug fix

## Full Access 直通规则

当前 WebUI 回合明确处于 Full Access 时，普通项目修复不需要先取得失败证据、项目 trust、
Approval Token 或 ChangeSet 二次确认。先读取必要源码并生成最小补丁，再调用
`devpilot_bug_fix_propose`；该工具会直接生成 Diff、应用修改并运行可用验证。不要在普通
代码修改前要求用户回复“执行”。

只有补丁、读取结果或准备外发的数据疑似包含密钥、凭据、个人文件、聊天记录或浏览器数据
时，才停止并请求一次隐私确认。Default 模式继续遵循下面的受控流程。

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
- 若用户只描述现象，先用 `devpilot_test_run` 自主运行项目测试并生成服务端失败 Run；若已有
  失败 Run，直接复用。两种情况都必须包含目标文件与失败前 hash；
- symptom、expected behavior、非目标、允许文件和回归 profile 已明确。

## 允许 Tool

- `devpilot_inspect_project`、`devpilot_search_code`、`devpilot_read_code`：只读定位和取得事实；
- `devpilot_test_run`：在已信任项目的沙箱中运行 allowlisted 项目测试，服务端保存真实退出码和
  输出回执；失败时自动形成失败证据，不需要用户先指定终端命令；
- `devpilot_bug_fix_propose`：绑定失败证据并创建 bug-fix proposal，只读提案；
- `devpilot_propose_changeset`：创建不可变 Diff，只读提案；
- `devpilot_request_approval`：登记用户确认的审批意图，不接收或返回 capability token；
- `devpilot_get_changeset`、`devpilot_get_run_state`：读取服务端事实。
- `devpilot_watch_run`：读取脱敏阶段事件，支持断点重放和持久快照降级；只读。
- `devpilot_confirmed_action`：仅把 Gateway 签名的用户确认交给受信任 Broker；不接受 token。

Skill 只能建议上述 Tool。Apply、Verify、Rollback 必须由控制面用户动作完成。停止必须在
nanobot 对话中取得用户明确确认；验证通过后不再要求额外的最终验收。回滚只返回现有 R1
审批入口，不得由 Skill 自行取得 capability。

## 执行步骤

1. 检查项目结构；若没有已保存的失败证据，自主选择匹配的项目测试并调用
   `devpilot_test_run`，不得要求用户先给出测试命令；
2. 读取失败 Run，核对真实退出码、失败现象、目标文件和 before hash；
3. 用 inspect/search/read 取得相关源码事实，不能把模型猜测当作失败证据；
4. 生成最小修复 proposal，绑定 failure evidence、symptom、expected behavior 和回归 profile；
5. 创建 ChangeSet/Diff，展示允许文件、风险、前后 hash、验证和回滚方式；
6. 展示精确 ChangeSet 并等待用户明确表达意图；系统支持自然语言确认（例如“按这个方案修好并跑测试”或“把这次修改撤回”），只有语义不明确或互相冲突时才追问一次，不要求用户记忆固定口令；
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

如果 `approval_pending` 或 `APPROVAL_BROKER_UNAVAILABLE`，要把动作状态说明为“已记录/等待审批，尚未执行”，本轮直接结束，不再要求用户重复确认同一个动作。

## 当前 Run 的临时工具

只有在 Full Access 的 WebSocket 会话中，且现有工具确实无法完成目标时，才调用
`devpilot_ensure_temporary_tool`。它会自动完成生成、FC 校验和当前 Run 热加载；验证失败不得改用
本机直接执行。Run 结束后如果出现加粗的保存询问，只有用户明确同意时才调用
`devpilot_persist_temporary_tool`，用户拒绝则调用 `devpilot_discard_temporary_tools`。
