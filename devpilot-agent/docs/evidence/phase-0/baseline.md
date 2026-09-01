# P0 baseline evidence

状态：P0 基线核验已完成（2026-08-31）。

## 已核验

- nanobot 源码目录可运行 `python -m nanobot --help`。
- nanobot 包版本为 `0.3.0`，要求 Python `>=3.11`。
- 当前 nanobot ZIP 解压目录没有 Git 元数据，不能提供 commit。
- DevPilot 采用独立仓库边界，不复制 nanobot、DeepSeek Harness 或 Codex 源码。
- 配置模板默认回环绑定、workspace 限制、关闭内置任意 exec 和关闭网络。
- Windows 原生环境的 Sandbox 状态必须显示为不可用或降级，不能宣称 OS 隔离。
- `uv sync --dev` 已成功生成并使用 `uv.lock`。
- `uv run pytest`：7 passed。
- Ruff、Pyright、密钥扫描、Markdown 链接检查：全部通过。
- nanobot Gateway `127.0.0.1:18790/health`：200；WebUI `127.0.0.1:8765/`：200。
- nanobot CLI：`python -m nanobot --help` 可用。
- 脱敏 DeepSeek Provider smoke test：返回 `DevPilot P0 provider smoke OK`。
- 空 Vite 工程：`pnpm install` 和 `pnpm run build` 均成功。

## 待执行证据

- [x] 在新虚拟环境中安装并锁定依赖。
- [x] nanobot Gateway/WebUI 健康检查。
- [x] 脱敏 DeepSeek Provider smoke test。
- [x] 缺 Key、错误端口、不可写目录、无 Sandbox 四类诊断脚本入口已建立；Sandboxed 配置在 Windows 上会失败并阻止误用。
- [x] 生成上游归档和关键文件 SHA-256。
- [x] Ruff、Pyright、Pytest、secret scan 和 Markdown 检查。

## 记录规则

本文件只记录可复现命令、脱敏输出和日期，不记录 API Key、完整环境变量、会话内容或用户绝对隐私路径。
