# 一键启动本地录制

首次在新电脑上只要：克隆仓库，并在本机填写一次模型配置。模型 Key、会话、数据库和本机随机密钥不会上传 GitHub。

在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\start-local-recording-demo.ps1
```

脚本会安装首次依赖、创建独立 `shopsphere-recording-demo` 工作副本、放入两个演示故障、发布三个本地 Skill、创建并信任项目，并启动商城 `5000` 和个人 Agent `8765`。

如果之前录过、Agent 已修好副本，重新从故障状态开始：

```powershell
powershell -ExecutionPolicy Bypass -File .\devpilot-agent\scripts\start-local-recording-demo.ps1 -ResetDemo
```

该命令只重置脚本创建的副本，不会改仓库内 ShopSphere 源码。端口占用时可用 `-ShopPort 5001 -AgentPort 8766 -GatewayPort 18791 -ControlPlanePort 18801`；只检查模型配置和路径时用 `-ValidateOnly`。

录制范围为本机 Harness：FC/cloud 默认关闭；临时工具按当前 Windows 用户权限本机运行，不是 OS 沙箱；展示 Skill 热刷新，不展示 Skill 自进化。
