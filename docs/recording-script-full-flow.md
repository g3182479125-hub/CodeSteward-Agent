# DevPilot Agent 完整录制稿

这份稿子用于正式录制 Agent 的完整 Bug-fix 流程。台词尽量保持自然，不需要逐字念完解释部分；代码块中的内容直接复制到 Agent 对话框即可。

## 一、录制前准备

### 项目位置

训练项目在：

```text
E:\codex-local\nanobot-devpilot-agent\training-projects\python-project-template-bugfix
```

项目关键文件：

```text
main.py
tests\main_test.py
```

Agent WebUI：

```text
http://127.0.0.1:8765
```

进入 WebUI 后，选择项目：

```text
python-project-template-bugfix
```

录制前确认：

- 项目已经注册并处于 trusted 状态；
- Agent 已连接；
- 训练项目处于原始基线；
- 不要提前修改 `main.py`；
- 浏览器画面中能看到项目名称和对话区。

## 二、开场介绍

对着镜头说：

> 今天我来完整跑一下这个 Agent 的 Bug-fix 流程。我们先让它自己检查项目和测试，确认问题以后生成一个最小修改方案，然后经过确认再执行，最后在受控沙箱里验证，并且把改动回滚掉。整个过程我不会直接改代码。

## 三、第一步：检查项目和测试

发送给 Agent：

```text
我们现在从头开始完整走一遍。项目就在 E:\codex-local\nanobot-devpilot-agent\training-projects\python-project-template-bugfix。先别改代码，先检查这个项目，读一下 main.py 和 tests\main_test.py，然后用受控 FC 沙箱跑测试。测试目标文件要同时包含 main.py 和 tests\main_test.py，确认是不是 3 个失败、2 个通过，把项目路径、Run ID 和失败原因都告诉我。
```

这里要展示：

- Agent 知道项目的准确位置；
- 读取 `main.py` 和 `tests\main_test.py`；
- 失败 Run 的目标文件同时登记 `main.py` 和 `tests\main_test.py`；
- 测试在 FC 沙箱中执行；
- 输出 `3 failed, 2 passed`；
- 返回项目 ID、分析 Run ID、测试 Run ID 和沙箱 ID。

Agent 给出结果后，对着镜头说：

> 这里可以看到，它没有直接改代码，而是先读项目、读测试，再在受控沙箱里复现问题。现在确认是三个测试失败，两个测试通过。

## 四、第二步：让 Agent 解释失败原因

如果 Agent 没有完整说明原因，发送：

```text
你把这三个失败再用正常人能看懂的话说一下：每个测试为什么失败，具体是 main.py 哪个函数的问题，先别改代码。
```

预期看到三个问题：

1. `add_task` 使用 `len(tasks) + 1`，删除任务后会重新复用旧 ID；
2. `remove_task` 把任务 ID 当成列表下标使用；
3. `complete_task` 找不到任务时直接对 `None` 操作，没有抛出清晰的 `KeyError`。

可以说：

> 这一步体现的是 Agent 不只是告诉我测试失败了，它还把失败现象、代码位置和根因对应起来了。

## 五、第三步：生成最小 ChangeSet 提案

发送给 Agent：

```text
可以，基于刚才这次失败证据给我一个最小的 ChangeSet 提案。只改 main.py 里的 add_task、complete_task、remove_task 这三个函数，先不要执行，把 Diff、验证命令、预期结果和回滚范围都写清楚。
```

如果 Agent 开始解释 patch 格式，改发：

```text
不用继续解释 patch 格式了，直接调用专用的 bug-fix propose。就用刚才的分析 Run 和失败 Run，生成一个只改 main.py 的 ChangeSet。先不要执行，告诉我 ChangeSet ID、Diff 和验证命令。
```

这里要展示：

- ChangeSet ID；
- 状态为 `awaiting_user_approval`；
- 改动范围只有 `main.py`；
- Diff 只涉及三个函数；
- 没有修改其他文件；
- 预期结果为 `5 passed`；
- 回滚目标是原始文件 SHA。

对着镜头说：

> 现在只是提案，还没有执行。Agent 把修改范围、验证方式和回滚范围都列出来了，所以我可以先审 Diff，再决定要不要执行。

## 六、提案范围错误时的补救

如果 Agent 返回类似下面的错误：

```text
HTTP 409: BUGFIXSCOPEINVALID
```

不要改代码。这个错误通常表示刚才的失败 Run 只登记了 `tests\main_test.py`，没有把要修复的 `main.py` 纳入失败证据范围。发送下面这句话，让 Agent 重新跑一次失败测试：

```text
刚才的失败 Run 目标文件范围不完整。请重新用受控 FC 沙箱跑一次失败测试，target_files 必须同时包含 main.py 和 tests\main_test.py。不要改代码，跑完告诉我新的失败 Run ID 和 3 failed、2 passed 的结果。
```

拿到新的失败 Run 后，再发送：

```text
现在基于新的失败 Run 重新生成 ChangeSet。只改 main.py 的 add_task、complete_task、remove_task，先不要执行。
```

这个错误是范围校验保护，不代表沙箱没钱，也不代表项目损坏。

## 七、第四步：确认并执行

检查 Diff 没问题后，发送：

```text
Diff 我看过了，现在可以执行。
```

如果系统出现审批请求，发送：

```text
执行
```

这里要展示：

- Agent 检查原始 SHA；
- 校验项目状态没有变化；
- 只应用允许的 `main.py`；
- ChangeSet 状态从 planned 变为 applied。

可以说：

> 这里需要经过明确确认才会真正改代码，提案阶段和执行阶段是分开的。

## 八、第五步：在 FC 沙箱中验证

发送：

```text
现在马上验证，还是跑刚才同一套 python pytest 测试，必须在受控 FC 沙箱里执行。把测试命令、退出码、沙箱 ID 和完整结果告诉我。
```

如果系统要求审批，发送：

```text
验证
```

预期结果：

```text
5 passed
exit code: 0
backend: fc-agent-sandbox
```

对着镜头说：

> 可以看到，三个失败现在全部通过了，而且不是本地直接跑的，是在受控 FC 沙箱中验证的。这里还有测试命令、退出码和沙箱 ID，可以完整追溯。

## 九、第六步：回滚并再次验证

发送：

```text
验证通过了。现在把刚才的改动撤销，恢复到原始版本，然后再跑一次同样的测试，确认回滚确实生效。
```

如果系统要求审批，发送：

```text
撤销
```

回滚结束后发送：

```text
回滚完成后再跑一次测试，把最终结果告诉我。
```

预期结果：

```text
3 failed, 2 passed
```

对着镜头说：

> 回滚以后结果重新回到了最初的三个失败、两个通过，说明这次不是只演示了一个修复，而是完整走通了 Apply、Verify、Rollback 三个阶段。

## 十、沙箱状态的解释

如果页面出现：

```text
status: degraded
network policy: provider-default
```

你可以说：

> 这里的 degraded 不是沙箱创建失败，而是当前使用 provider-default 网络策略，不能证明严格网络隔离。沙箱本身已经正常创建、执行并返回结果，所以这不影响本次代码验证。

## 十一、扩展展示：Skill、Tool、Loop 热插拔

这一段建议在主流程录完以后单独录。如果当前版本的动态管理入口已经在界面中出现，就继续；如果没有出现，不要临时猜接口。

先发送：

```text
你先把当前已经加载的 skill、tool 和 loop 列出来，告诉我每个分别负责什么，先不要修改项目。
```

然后发送：

```text
现在不重启 harness，动态加载一个用于 Python 回归测试的 skill。加载完成以后告诉我它是否已经生效。
```

再发送：

```text
还是不重启。现在直接用刚刚加载的 skill 跑一下这个项目的回归测试，并把实际调用的 tool 和 loop 告诉我。
```

最后发送：

```text
现在把刚才临时加载的 skill 卸载掉，再确认当前 harness 里的 skill、tool 和 loop 状态。整个过程不要重启。
```

这一段的解说词：

> 这里展示的是 harness 运行期间动态加载和卸载 skill、tool、loop，不需要重启整个 Agent。Agent 的能力不是写死在一次启动过程里的，可以根据任务动态扩展和调整。

## 十二、录制时不要做的事情

- 不要在主流程中途手动编辑 `main.py`；
- 不要跳过 ChangeSet 审核画面；
- 不要只展示本地测试，要展示 FC 沙箱 Run；
- 不要把 `degraded` 直接说成沙箱失败；
- 不要现场让 Agent 手工重建整份文件来处理换行问题；
- 不要在没有看到实际结果前提前说“验证成功”。

## 十三、结尾台词

```text
今天这个流程已经完整跑通了：先发现 3 个测试失败，再生成最小修复提案，经过确认后执行，在 FC 沙箱里验证 5 个测试全部通过，最后回滚并恢复到原始失败状态。整个过程有项目路径、Run ID、ChangeSet、沙箱 ID、验证结果和回滚结果，可以追溯，也可以安全录制。
```

## 十四、录制时必须保留的画面

1. 项目路径和项目名称；
2. `3 failed, 2 passed` 的初始结果；
3. 三处逻辑 Diff；
4. `awaiting_user_approval` 状态；
5. `5 passed` 的 FC 沙箱验证结果；
6. 回滚后的 `3 failed, 2 passed`；
7. Run ID、ChangeSet ID 和沙箱 ID。
