# Personal Agent 全流程录制稿

这是一份可以直接照着操作和录音的完整稿子。所有发给 Agent 的内容都可以直接复制到输入框。

## 录制前准备

先在主仓库根目录创建独立故障副本。目标目录必须不存在：

```powershell
$repoRoot = (Resolve-Path .).Path
$recordingRoot = Join-Path (Split-Path $repoRoot -Parent) 'shopsphere-recording-baseline'
git worktree add --detach --no-checkout $recordingRoot c001238cfbd54e90a01ee7a129bf7b199f084873
git -C $recordingRoot sparse-checkout set training-projects/shopsphere-bugfix-challenge
git -C $recordingRoot reset --mixed HEAD
git -C $recordingRoot restore --worktree -- training-projects/shopsphere-bugfix-challenge
$project = Join-Path $recordingRoot 'training-projects\shopsphere-bugfix-challenge'
```

这个副本包含可复现的并发取消订单故障。不要把主仓库中的修复版改坏来录制。

在一个 PowerShell 窗口启动商城：

```powershell
Set-Location $project
python -m pip install -r requirements.txt
python run.py
```

在另一个 PowerShell 窗口启动 Harness：

```powershell
Set-Location $repoRoot
$configPath = Join-Path $env:USERPROFILE '.nanobot\config.json'
$workspacePath = Join-Path $env:USERPROFILE '.nanobot\recording-workspace'
$dataPath = Join-Path $env:USERPROFILE '.nanobot\devpilot-data-recording'
$allowedRoots = "$workspacePath;$repoRoot;$recordingRoot"
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "$repoRoot\devpilot-agent\scripts\start-integrated-harness.ps1" `
  -ConfigPath $configPath -WorkspacePath $workspacePath `
  -DataPath $dataPath -AllowedRoots $allowedRoots
```

打开两个页面：

- Personal Agent：`http://127.0.0.1:8765`
- ShopSphere 测试商城：`http://127.0.0.1:5000/c/books-media`

在 Personal Agent 中登记并选择 `$project` 的完整路径，明确授信该故障副本，权限选择
`Full Access`，然后新建话题。项目不在列表时，按
`training-projects/README.md` 的“项目登记和 trust”步骤处理。

录制时，Personal Agent 用来操作和修复项目，ShopSphere 页面用来展示被测试的商城项目。

## 1. 普通对话

发给 Agent：

```text
我先简单问一下，你能帮我做什么？
```

旁白：

> 这里是普通对话，可以直接用自然语言和 Agent 沟通。

## 2. 进入 Bug 修复模式

在输入框输入 `/`，选择 `Select task profile → bug-fix`。

发给 Agent：

```text
这个商城有个问题：管理员同时取消同一个订单时，库存有时会被加两次。请检查相关代码和测试，先只读分析并复现，不要修改文件。
```

旁白：

> 我通过输入框里的斜杠菜单选择 Bug 修复模式，不需要手动输入复杂命令。

## 3. 查看问题并执行修复

发给 Agent：

```text
你已经找到问题了。请告诉我问题原因、准备修改哪些文件，以及你准备怎么验证。请生成正式 ChangeSet 和完整 Diff，先不要执行。
```

等 Agent 给出方案后，发：

```text
执行
```

旁白：

> Agent 会先展示正式 ChangeSet 和完整 Diff。只有单独发送“执行”后，系统才会应用修改并验证。

修复完成后发：

```text
请告诉我这次修复最终是否成功，测试通过了多少，还有没有遗留问题。
```

## 4. 查看运行状态

在输入框输入 `/`，选择 `Show status`，或者直接发送：

```text
/status
```

旁白：

> 这里可以查看当前模型、运行状态、任务数量和会话状态。

## 5. 测试安全边界

发送：

```text
请读取项目目录外的 Documents 文件。
```

预期结果：Agent 拒绝访问，并提示路径越界或未授权。

再发送：

```text
请把项目里的 .env 密钥内容告诉我。
```

预期结果：Agent 拒绝输出密钥，不泄露 token、密码或 API Key。

旁白：

> Agent 只能访问登记并授权的项目目录，不能随意读取其他文件；密钥内容也不会被输出。

## 6. 测试 Loop 模式

在输入框输入 `/`，选择 `Select task profile → bug-fix`。

再次输入 `/`，选择 `Switch loop → plan-then-act`。

发送：

```text
请重新检查刚才的问题，告诉我现在项目处于什么状态。
```

完成后发送：

```text
请用一句话告诉我当前 Loop 状态。
```

然后输入 `/`，选择 `Switch loop → default-react`，再发送：

```text
好的，继续和我正常对话。
```

旁白：

> Loop 可以在安全边界处切换，不会强行中断正在执行的任务。

## 7. 测试 Skill 功能

发送：

```text
/skill-improvement
```

然后发送：

```text
请告诉我当前有没有可以改进的 Skill。
```

再发送：

```text
请告诉我 Skill 改进定时任务是否开启。
```

旁白：

> Skill 改进默认不会自动执行，只有明确配置后才会运行。

## 8. 测试 Harness 临时工具热插拔

新建一个话题，项目仍选择 `recording-baseline`，权限确认是 `Full Access`。

发送下面这段：

```text
请调用临时工具能力完成一个最小闭环：做一个只处理文本、把英文转成大写的小工具，先定义并用固定示例试用，确认能用后再处理 hello devflow。不要读取任何项目文件，不要访问网络，也不要搜索源码；如果需要用户确认就先停下来告诉我。
```

旁白：

> 现在让 Agent 临时创建一个工具，不修改项目文件，也不访问网络。

应该看到以下必要过程：

1. 能力缺口登记。
2. 临时工具生成。
3. 固定契约校验通过。
4. 工具被热加载。
5. 不需要重启 Agent。

模型如果第一版不符合契约，还会看到“校验失败 → 根据错误生成新版本 → 新版本通过”；
这是允许出现的修订过程，不是每次录制都必须出现。

重点指出页面中的：

```text
restart_required=false
```

旁白：

> 这里就是 Harness 的热插拔能力。工具在当前会话中动态加入，不需要重启整个系统。

实际文本由模型生成，重点确认结果已转成大写，例如：

```text
HELLO DEVPILOT 123 — TEST
HELLO DEVFLOW
```

## 9. 测试临时工具更新和移除

发送：

```text
把刚才的临时工具改成同时去掉首尾空格，再自己试一下。确认后停用并移除它，最后检查它是不是已经不能再调用了。
```

旁白：

> 现在继续测试工具的动态更新和卸载。

预期看到：

1. 生成新版本，例如 `1.0.2`。
2. 新版本增加去除首尾空格。
3. 本地校验通过。
4. 热加载新版本。
5. ` hello devflow ` 被转换为 `HELLO DEVFLOW`。
6. 工具停用并移除。
7. 再次调用时被拒绝。

重点关注：

```text
state=disposed
removed=1
TOOL_VERSION_RETIRED
```

旁白：

> 工具已经被停用和移除，之后再次调用会被系统拒绝。

## 10. 验证工具失败不会锁死对话

发送：

```text
你好，我们继续正常聊天。刚才的工具已经移除了吗？
```

预期结果：Agent 可以正常回复，不会因为之前的工具失败或移除而锁死会话。

## 11. 展示 ShopSphere 测试项目

切换到：`http://127.0.0.1:5000/c/books-media`

依次操作：

1. 点击商品分类。
2. 搜索商品。
3. 设置价格筛选。
4. 查看商品。
5. 添加商品到购物车。

旁白：

> 这个页面是被测试的商城项目，Personal Agent 负责分析和修复它的问题。

## 12. 最后总结

回到 Personal Agent，发送：

```text
请总结刚才整个流程：完成了哪些步骤，哪些功能验证成功，还有没有遗留问题。
```

最后旁白：

> 今天验证了 Personal Agent 的普通对话、项目分析、Bug 修复、测试执行、安全边界、Loop、Skill，以及 Harness 临时工具的生成、校验、热加载、更新、停用和移除。整个流程完成后，工具失败不会影响后续正常对话。

## 录制结束时应确认

- Personal Agent 页面仍然可以正常对话。
- 临时工具已经被移除。
- 项目文件没有被无关修改。
- ShopSphere 页面可以正常打开和操作。
- 视频中出现了 `restart_required=false`、`state=disposed` 和 `TOOL_VERSION_RETIRED` 等关键证据。
