# FC Agent Sandbox 接入端口产品审查

日期：2026-09-01  
决策：approve-with-follow-ups

## 审查结论

本阶段完成了可替换执行后端和阿里云 FC Agent Sandbox 的 E2B 兼容适配，满足“保留
沙箱接入口、默认不启用、不影响 P2B 本地审批”的目标。Fake 后端和模拟 SDK 生命周期
测试提供了可重复的本地证据。

## 通过项

- 没有修改 nanobot 上游源码，也没有把 FC API Key 放入 Tool、Prompt、Session 或前端。
- FC 后端只接收调用方显式提供的文件包和命令，路径、超时、重复输入和网络请求 fail closed。
- Sandbox 创建、文件写入、Commands 执行、结果收集和 `kill()` 清理职责集中在适配器中。
- Control Plane 的 `/verify` 使用独立 `r2.verify` 授权；本地 P2B Apply/Rollback 流程未被
  绕过，云端验证结果不会直接写回本机项目。
- 可选依赖懒加载；无 FC 凭据时，原有 P2B 测试和本地服务不受影响。

## 必须保留的后续门禁

1. 真实账号连通性测试必须使用脱敏 fixture、最小权限 API Key、明确地域和资源超时。
2. P3 需要扩展完整项目快照和更严格的测试命令白名单，禁止把任意模型文本直接交给 Commands。
3. 仍需补充验证取消、结果审计和成本上限，并把验证证据接入最终交付决策。
4. 沙箱内的环境变量、网络、外部存储和输出仍需单独治理，不能把 Sandbox 视为绝对可信边界。
5. 每次真实调用必须确保 `kill()`，并记录 sandbox_id、region、template、耗时、退出码和
   脱敏后的结果摘要，不记录 API Key 或完整敏感输出。
