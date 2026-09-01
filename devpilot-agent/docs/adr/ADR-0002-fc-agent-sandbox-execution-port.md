# ADR-0002：接入阿里云 FC Agent Sandbox 作为可选执行后端

日期：2026-09-01  
状态：已接受

## 决策

1. 阿里云 FC Agent Sandbox 通过官方 E2B 兼容 Python SDK 接入，不在本项目中复制
   FC 控制面或再实现一套 Runtime。
2. `ExecutionBackend` 是唯一执行端口；本地 Fake 后端用于契约测试，FC 后端按需懒加载，
   没有云凭据时不影响 P2B 本地审批服务启动。
3. 每次验证使用短生命周期 Sandbox：创建、写入显式文件包、执行一个显式命令、收集
   stdout/stderr/exit code，最后在 `finally` 中销毁 Sandbox。
4. 本机项目目录不直接挂载到云端。输入必须由上层根据 ChangeSet 快照和允许文件清单
   组装；云端结果只能作为验证证据，不能直接写回本地项目。
5. FC Sandbox 接入不授予 nanobot 新 Tool 权限，不接收 API Key，不绕过 P2B 的 Apply /
   Rollback 审批。Control Plane 的 `/verify` 只接受独立的 `r2.verify` 授权，并且不把
   验证结果自动升级为 Apply 授权。
6. 默认拒绝网络访问请求。当前适配器不把“未配置网络”表述为绝对网络隔离；最终网络
   边界必须由 FC 模板、地域、账号和云侧配置共同验证。

## 当前不做

- 不依赖 FC Snapshots、Volume、Team 管理或 Sandbox Logs 等官方标注为暂不兼容/受限的能力。
- 不把云沙箱当作长期存储；需要保留的结果回到 DevPilot Artifact/数据库或经过明确授权的
  OSS/NAS。
- 不在本阶段自动安装依赖、不执行任意模型生成的 Shell、不把执行结果自动应用到本地仓库。
  当前 Control Plane 只开放 Python/Node 语法检查 Profile，未开放任意命令输入。

## 接入配置

依赖使用官方当前验证版本：`e2b==2.31.0`、`e2b-code-interpreter==2.8.1`。通过环境变量
注入 `E2B_API_KEY`、`E2B_API_URL` 和 `E2B_DOMAIN`；API Key 不得进入代码、日志、Prompt、
Session 或前端页面。地域必须与 Endpoint 和 Sandbox 资源一致。
