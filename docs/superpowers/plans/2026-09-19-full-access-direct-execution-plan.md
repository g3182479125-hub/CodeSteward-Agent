# 完全访问直通执行实施计划

## 目标

在 WebUI Full Access 回合中，常规 DevPilot 工作流可以生成、应用并验证修改，不因证据、trust、Approval Token 或 ChangeSet 二次确认被拒绝；Default 模式保持原有受控行为。

## 步骤

1. 在 DevPilot 添加一个小型执行策略适配器，从 nanobot 当前可信 WebUI 工作区范围读取 `access_mode == "full"`，并提供可单测的策略判断。
2. 为 Feature Delivery、Bug Fix 与 Test Repair 增加直通提案分支：保留路径解析、补丁预览、补丁哈希和审计事件，跳过对已有证据 Run 的强制校验。
3. 在控制平面增加仅供同进程 Full Access 调用的直接 Apply / Verify 接口。它复用已有补丁服务、项目锁、基线比较、验证和回滚记录；不创建或消费批准令牌，也不要求项目 trust。
4. 让 WebUI 受信回合中的 DevPilot 动作工具选择直接接口；Default 回合继续走签名确认和现有令牌流程。
5. 保留敏感内容脱敏。隐私确认作为独立策略钩子，只在读取、外发或回显敏感数据时返回明确的待确认结果。
6. 增加回归测试：Full Access 小型修改无需 409/确认即可 Apply+Verify；Default 仍需要确认；敏感输出不被回显。
7. 运行针对性 Python 与 WebUI 回归，并使用训练项目完成一次自然语言端到端验证。

## 不在本次范围

- 不删除 Default 模式的既有门禁。
- 不绕过操作系统权限、缺失凭据、真实命令失败或云端服务故障。
- 不自动外发、上传或展示未经确认的敏感数据。
