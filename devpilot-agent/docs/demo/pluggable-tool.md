# 可插拔工具演示

这个示例展示 DevFlow Agent 借鉴 DeepSeek Harness 的“Provider / Tool Registry / Entry Point”思路：
新增一个工具只需要独立实现 Tool，并在 `pyproject.toml` 声明 `nanobot.tools` entry point，
不修改 nanobot Agent Loop，也不修改 DevPilot 的 Apply、Verify 和审批边界。

示例位置：

`examples/pluggable-tool/`

## 现场演示

在 PowerShell 中执行：

```powershell
cd E:\codex-local\nanobot-devpilot-agent\devpilot-agent\examples\pluggable-tool
uv pip install --python E:\codex-local\nanobot-devpilot-agent\nanobot\.venv\Scripts\python.exe -e .
```

重启 nanobot 后，新工具会被 ToolLoader 自动发现。新建一个普通 nanobot 对话，输入：

```text
请调用 demo_greeting，名字叫小明，并告诉我这个工具来自哪里。
```

预期结果包含：`插件工具已加载：你好，小明！`。这一步只读，不会修改项目文件。

## 插拔动作

卸载插件：

```powershell
uv pip uninstall --python E:\codex-local\nanobot-devpilot-agent\nanobot\.venv\Scripts\python.exe devflow-greeting-tool
```

再次重启 nanobot，`demo_greeting` 就不会出现在工具列表中；DevPilot 交付工具和原生 nanobot
功能不受影响。

## 录制时要强调的设计点

- `tool.py` 只实现一个独立 Tool，输入输出和权限边界清晰。
- `pyproject.toml` 的 entry point 是插头，ToolLoader/ToolRegistry 是插座。
- 插件是只读工具，不能调用 Apply、Verify、Rollback，也不能取得审批 token。
- DevPilot 的受控代码交付仍然走原有 `feature-delivery` Profile，不与演示插件混用。
