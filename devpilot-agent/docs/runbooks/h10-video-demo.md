# H10 录屏演示

此演示使用真实 DeepSeek、原生 nanobot WebUI 与已配置的 FC。FC 的实际网络策略是
`provider-default`：演示应如实说“固定 profile、显式确认和受控上传限制了执行”，不要说
“FC 已严格断网”。只对隔离 demo 项目执行，绝不指向用户实际项目。

## 1. 启动（不要展示密钥或 config 内容）

在仓库根目录设置三个本机路径：模型配置、隔离 demo 工作区、全新的隔离 DevPilot 数据目录。
工作区必须是 `AllowedRoots` 的子目录；配置文件不应录屏展示。

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
$configPath = 'D:\path\to\nanobot\config.json'
$workspacePath = 'D:\path\to\h10-demo-workspace'
$dataPath = 'D:\path\to\h10-video-runtime'

powershell -ExecutionPolicy Bypass -File "$repoRoot\devpilot-agent\scripts\start-integrated-harness.ps1" `
  -ConfigPath $configPath -WorkspacePath $workspacePath -DataPath $dataPath `
  -AllowedRoots $workspacePath -ValidateOnly

powershell -ExecutionPolicy Bypass -File "$repoRoot\devpilot-agent\scripts\start-integrated-harness.ps1" `
  -ConfigPath $configPath -WorkspacePath $workspacePath -DataPath $dataPath `
  -AllowedRoots $workspacePath
```

浏览器打开 `http://127.0.0.1:8765`。终端不应打印任何 API key；若路径检查失败，先修正
路径，不要为了通过而将 `AllowedRoots` 设置成整个盘符。

首次启动若提示缺少 `DEVPILOT_HARNESS_SELECTION_INTEGRITY_KEY` 或
`DEVPILOT_CANDIDATE_GRANT_SECRET`，按
[`local-nanobot-devpilot.md`](local-nanobot-devpilot.md) 的一次性初始化代码生成本机用户密钥；
不要手工编造短字符串，也不要把它们录进视频。

## 2. 录制顺序

1. 新建会话，输入 `/task-profile feature-delivery`；确认可见工具包含
   `devpilot_model_plugin`，说明 H6/H8/H9 已启用。
2. 说明传统 Python entry-point 插件与本次能力的区别：前者是例如
   `pip install my_calendar_plugin` 后由 nanobot 启动时发现的包元数据，安装、依赖升级或卸载
   可能改变解释器模块图，所以需要重启；这不是 H6 的运行中插件。
3. 在同一会话让模型按 `inspect → define → trial → activate` 创建一个纯计算小工具，调用它后
   再定义 v2 并 activate。展示下一步换成新版本即可；宿主 PID、会话和 WebUI 均不重启。
   之后执行 `stop`、`remove`，展示工具不再出现在后续步骤。
4. 输入 `/plugins switch-loop plan-then-act`，继续当前任务，再切回
   `default-react`。说明切换发生在安全步骤边界，任务/会话连续，未结算副作用不会重放。
5. 对隔离 `main.py` 进行一个极小的 feature-delivery：先提案，检查 Diff，再单独输入
   `执行`。展示 FC 返回的固定 `python-syntax` 验证、`provider-default` 标签和结果查询。
6. 单独输入 `撤销`，接受独立确认，再读取文件确认内容恢复。刷新页面，展示会话记录仍可查询。

## 3. 口径与停止条件

- “无需重启”仅指 H2/H3 管理的受信本地动态版本、H6 的 FC 代理候选工具、H8 的 Driver
  切换；它不允许模型在宿主上运行任意 `pip` 或 import 任意生成代码。
- H6 候选代码始终由 FC 代理执行；宿主只注册经 trial/授权后生成的受控代理，不 import
  候选 Python 文件。
- FC 不可用、路径不在 allowlist、候选未试用、确认过期或验证失败时，停止并展示真实错误，
  不回退到本机 shell，也不使用 fake 伪装成功。
- 录制结束后关闭 nanobot 前台窗口；如需保留证据，保留隔离 `$dataPath`，不要提交它。
