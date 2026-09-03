# P4-04 交付报告与决策设计

日期：2026-09-03
前置节点：P4-03 `test-repair`
目标节点：P4-04 交付报告、停止/接受/回滚决策与 P4 evidence

## 目标与边界

P4-04 不增加新的执行后端，也不改变 nanobot Runtime。它在现有三条工作流
（`feature-delivery`、`bug-fix`、`test-repair`）之上提供统一的交付结果读取和
用户决策收口。报告只能由服务端已持久化的 ChangeSet、Run、RunEvent 和 Artifact
事实构成；模型不能直接设置终态或伪造成功。

本节点仍然 Fake-only：不运行真实 Python、Node、npm、Shell、网络或自动修复循环。
真实 Sandbox 继续由 P6 负责。

## 方案

新增深模块 `DeliveryDecisionWorkflow`，外部接口保持最小：

- `report(change_set_id)`：读取并汇总交付事实，返回真实状态、改变文件、验证结果、
  Artifact、停止原因和可用的下一步决策；不产生副作用。
- `accept(change_set_id, confirmation)`：要求明确的“接受”确认，只允许
  `verified` 或 `verification_failed` ChangeSet 收口为 `accepted`，追加审计事件；不修改
  项目文件。
- `stop(change_set_id, confirmation)`：要求明确的“停止”确认，记录停止原因；若关联 Run
  仍在运行则将其结束为失败，不把失败改写成成功，不自动回滚。
- `rollback(change_set_id)`：只返回现有控制面回滚入口和所需能力，实际回滚仍由
  `ControlPlaneManager.rollback` 校验一次性 capability token、哈希和幂等键。

`DeliveryDecisionWorkflow` 不复制 Apply、Verify 或 Rollback 状态机；它只调用已有
控制面和存储接口，并以统一结构向 API/Tool 适配器提供结果。

## API 与 Tool

- `GET /v1/change-sets/{change_set_id}/delivery-report`：只读报告；
- `POST /v1/change-sets/{change_set_id}/decision`：接受或停止的用户确认；
- `devpilot_get_delivery_report`：nanobot 只读 Tool，只读取报告；
- 不新增模型可调用的 Apply、Verify 或 Rollback Tool。

决策请求拒绝未知动作、空确认、错误状态和重复收口。所有结果保留稳定错误码、
ChangeSet/Run 关联、Artifact 引用和前后 manifest hash；报告不包含 token、完整 stdout/
stderr、环境变量或密钥。

## 测试与证据

P4-04 测试覆盖：成功报告、验证失败报告、显式接受、显式停止、重复决策拒绝、报告
脱敏、回滚入口仍需既有 capability、HTTP/Tool contract，以及 P4-01/P4-02/P4-03
回归。验收文档和 manifest 只记录实际执行的命令和结果。
