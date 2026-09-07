# Nanobot DevPilot Agent

这是一个本地运行的个人 Agent 项目，包含：

- `nanobot/`：nanobot 主 Agent、工具系统和原生 WebUI；
- `devpilot-agent/`：DevPilot 控制平面、项目分析、变更方案和受控补丁能力。

AG-UI 不属于本项目。API key、运行时配置、数据库、虚拟环境和依赖目录均不会提交。

## Agent 开发规范

项目根目录的 `AGENTS.md` 是所有编码任务的必读入口。工程开发 Skill 的本地固定副本位于
`agent-skills/mattpocock-skills/skills/`，每次任务需要先阅读其总说明和与任务匹配的
`SKILL.md`。

## Windows 本地启动

在仓库根目录执行：

```powershell
uv sync --project .\nanobot --locked
uv pip install --python .\nanobot\.venv\Scripts\python.exe -e .\devpilot-agent

cd .\nanobot\webui
npm install
npm run build
cd ..\..
```

然后准备 nanobot 配置文件，填写自己的 OpenAI-compatible API key、base URL 和模型名，再启动：

```powershell
\.\nanobot\.venv\Scripts\nanobot.exe webui --config .\nanobot-config.json --workspace .\workspace
```

默认页面是 `http://127.0.0.1:8765`，网关健康检查地址是 `http://127.0.0.1:18790/health`。

## DevPilot 控制平面

DevPilot 的本地控制平面可以单独启动，具体命令和 P2A/P2B 流程见：

- `devpilot-agent/README.md`
- `devpilot-agent/docs/runbooks/local-development.md`
- `devpilot-agent/docs/evidence/phase-2a/p2a-acceptance.md`

nanobot 启动后会通过同一环境中已安装的 `devpilot-agent` entry points 加载 DevPilot 工具。
完整的换机配置、entry point 验证、控制面启动、密钥边界和恢复流程见
`devpilot-agent/docs/runbooks/local-nanobot-devpilot.md`。
