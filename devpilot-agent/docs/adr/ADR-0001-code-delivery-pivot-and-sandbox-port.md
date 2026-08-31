# ADR-0001：转向代码交付产品并冻结 Sandbox 接口

日期：2026-08-31  
状态：已接受

## 背景

原冻结计划的 P2–P7 以 Git 总结、日报和研发自动化为中心。P0/P1 已完成的只读仓库分析仍然有效，但不足以成为个人代码 Agent 的核心使用场景。用户决定将产品目标升级为“受控个人代码交付 Agent”，并要求暂不选定 Sandbox，只预留未来可接入的端口。

## 决策

1. 保留 `outputs/DevPilot-Agent-冻结版开发计划.md` 及 P0/P1 历史证据，不重写、不否定其已验收结果。
2. 新的 `outputs/DevPilot-Code-Delivery-Agent-冻结版实施计划.md` 接管 P2–P7 的执行权，目标闭环为：需求/缺陷→分析→计划→批准→结构化补丁→验证→最多三轮受限修复→证据/回滚。
3. nanobot 继续为唯一主 Runtime；DeepSeek Harness、Codex Harness 仅作设计参考，绝不复制 Rust Sandbox 或运行第二套 Runtime。
4. 先冻结 `ExecutionBackend` Protocol、能力模型、Fake 后端、契约与安全测试；不实现或宣称具体 Sandbox。
5. GuardedLocalBackend 只能是受信任项目、逐次批准的本地降级执行，并且明确不是 Sandbox；它不得进入自动修复循环。
6. 在未来具体 Sandbox 方案通过独立 ADR 与契约/安全/E2E 门禁前，任何 R2 自动验证或自动修复均 fail closed。
7. nanobot 直接暴露给模型的 Tool 只负责 R0 分析、ChangeSet 草案和审批请求；R1/R2 动作由本地 DevPilot 控制面消费一次性授权后执行，授权秘密永不进入 Prompt、Session、Tool 参数或 SSE。

## 后果

后续实现将优先学习结构化补丁、事务/回滚、授权令牌、执行协议、FastAPI 和 React，而日报、广泛 MCP、多 Agent、云端平台等不再是主线。Agent 在审批处暂停，由用户控制面触发副作用，再通过持久事件续跑；这会增加少量编排工作，但消除了把 capability token 暴露给模型的风险。历史计划中与此冲突的 P2–P7 条目不再执行；P0/P1 的安全边界继续有效。
