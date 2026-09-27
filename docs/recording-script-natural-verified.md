# DevPilot 本地 Harness 功能演示稿

> **录制状态：暂不可完整正式开录（NO-GO）。** ShopSphere 已有真实 WebUI Apply+Verify 与 223 项本地回归通过证据；临时 Tool 的真实 WebUI 创建/本机校验/热加载/下一步调用/Run 清理也已通过。仍需处理全量 Nanobot 的 18 个失败项、完成 disposable clone 上的 Rollback 演练、补齐实时跨会话隔离证据，并拿到 Standards/Spec 最终 PASS。不要把未完成项说成已经通过。

## 范围和现场声明

- 本次不展示 Skill 自进化；不启用、不调用、不回退到任何 FC/cloud 功能。
- 使用可丢弃的 ShopSphere 演示副本和合成内容，不放真实项目、令牌或个人数据。
- 所有本地执行都应明确说：**“本机执行（非 OS 沙箱；候选代码继承当前 Windows 用户权限）。”** 临时 Python Tool 不是安全隔离环境。
- 完成下方现场验收前，只能录制已验证的独立片段；不能称整条链路已完成。

## 录制前门槛

1. 确认模型可用、FC 关闭、使用本机演示实例及一次性工作区。
2. 确认 ShopSphere 挑战副本仍保留预期缺陷；记下初始文件哈希和基线结果。
3. 确认完整 DevPilot 回归、nanobot 全量回归、Ruff、`git diff --check` 和 Standards/Spec 两个 subagent review 有记录；最新 DevPilot 全量为 `577 passed, 5 skipped`；Nanobot 全量为 `6077 passed, 42 skipped, 18 failed`，exit 1，不得隐去或称全绿。改变范围的定向测试为 `278 passed, 1 skipped`，Ruff 与 `git diff --check` 通过。剩余失败复现于未改动的测试路径，尚未在精确基线 SHA 上做对照运行。
4. 不录屏显示任何 Gateway/Control Plane 密钥、API Key、数据库、日志路径或本机私人文件。

## 一、开场与只读检查

对 Agent 说：

```text
我给你一个单独准备的演示项目，你先帮我看看代码和测试，先别改文件。用正常人能听懂的话说说项目大概做什么、现在测试结果怎么样。
```

展示项目读取、测试证据和“没有改文件”。不要预告故障所在或预期失败数。

## 二、同一会话热刷新 Skill

在一次性工作区让 Agent 添加一个只用于演示的 Skill，文件中放入唯一标记 `SKILL_REFRESH_MARKER_V1`。然后在同一对话再问：

```text
刚才那个演示 Skill 里写的标记是什么？不用重新启动，也不要改别的文件。
```

只有在下一轮真实模型上下文包含新 Skill、会话/进程连续且没有重启时，才说“Skill 内容在运行中刷新”。这是 Skill 加载热刷新，不是 Skill 自进化。

## 三、临时 Tool 热插拔

对 Agent 说：

```text
我现在缺一个把两个数字相加的小工具。你先在这次会话里临时给自己加一个，按系统固定的本地检查流程验证，通过以后马上调用它算 17 加 25。接着把它更新成新版本再调用一次，最后停掉并确认这次会话已经看不到它。不要保存成全局插件，不要用云沙箱。
```

展示能力缺口、候选版本/哈希、本地验证回执、模型下一步可见的工具 schema、真实调用结果、版本更新、旧调用排空和 stop/清理；再用另一会话证明不可见。明确讲本机权限边界。**当前这一整段须等补丁后的真实 UI 验收通过后才能录。**

## 四、同一 Host 内 Loop Driver 切换

在允许切换的会话 Profile 中，使用内置命令发起切换：

```text
/plugins switch-loop plan-then-act
```

让同一任务继续走过一个安全检查点，再读状态：

```text
/plugins status <operation_id>
```

随后切回：

```text
/plugins switch-loop default-react
```

再经过安全检查点并查状态。展示 driver/generation 在同一聊天中的变化。准确表述为“稳定 Host 在检查点切换内置 Driver”，不要说核心 AgentLoop Python 代码被热替换。

## 五、ShopSphere ChangeSet 本地交付

只在 disposable challenge copy 对 Agent 说：

```text
这个演示项目好像有个并发问题。你自己先看代码、复现测试并找原因，别手工改文件。先给我一个最小 ChangeSet，把 Diff、要改的文件、验证命令和回滚范围讲清楚；我确认之后再按正式流程执行和验证。
```

该链路已有一次真实证据：ChangeSet `a2a0ab41-6334-48af-b0fb-e3889222c77b`，正式 Apply+Verify 只改 `shop/orders.py`，本地 `devpilot_test_run` Run `132ec7e6-dae9-4c29-96a2-8784355e3915` 返回 `223 tests / exit_code 0`，最终 SHA-256 `6438beb0210e8be83415a1bedc19552d5ef3a515b24144e7f4d356d8b0d4ac09`。控制面实际绑定的 Changeset 检查档位是 `python-syntax` 而非提案请求的 `python-regression`；完整 223 项回归是另一条正式本地测试 Run 的结果，录制时要展示并讲清楚，不能说 Apply+Verify 自身跑了完整回归。若重演，按正式 UI 审批和本地回执走；不得手改文件或绕过确认。只有另一个 disposable clone 才能演示 Rollback。

## 收尾

> 今天展示的是 Agent 在本机完成项目检查、运行时 Skill 刷新、会话级临时 Tool、内置 Loop Driver 检查点切换，以及受控 ChangeSet 交付。FC 和 Skill 自进化都不在本次演示范围。这里的临时 Python Tool 是以当前 Windows 用户权限在本机运行，并不是 OS 沙箱。

## 当前验收状态

- Skill 同一 WebUI 会话刷新：已有现场证据。
- Loop Driver 来回切换：已有现场证据，`plan-then-act@1` generation 4，切回 `default-react@1` generation 6。
- 临时 Tool live lifecycle：个人 Agent 在 WebUI session `websocket:4ce02c71-45dd-47df-b085-463ac918b772` 中走完缺口登记、候选创建、固定本机校验、热加载、下一次模型决策调用、Run 清理。最终 v1.0.4，`source_hash=1ab51dbcc776e4c4be515bf9e1f99bcb2cd355c9f8d526fadd6f7c33e5c878de`，`manifest_hash=2545b0bf7b4b97c9710ff453dde7501bc1594d1cb2f17bd3cc479e0c5b67d96c`；checker `temporary-tool-contract-check/v1`，`execution_location=local`，`exit_code=0`，3 个夹具全 true；generation 2、`route_commit=committed`、`restart_required=false`，Tool 真正返回 `20`。更新时旧版本清理完成；Run 结束 `devpilot_discard_temporary_tools` 返回 `disposed`、`removed=3`，两条 route 均 `remove=ok`。回执里的 removed 数与 route 行数不一致，录制时不要自行解释。实时另一会话隔离仍未展示；自动化临时 Tool 跨会话断言已加入。
- 更新后的 `nanobot-plugins` Skill 在没有重启服务的情况下，下一轮 Agent 已复述本机 `temporary-tool-contract` 与“FC 关闭时不调用 FC 候选工具”的新指引；WebUI listener PID 始终为 `86760`。该 Skill 文件位于本机 workspace，不在本仓库跟踪。
- ShopSphere 一次真实 Apply+Verify 已通过个人 Agent 正式 ChangeSet 流程完成，ChangeSet `a2a0ab41-6334-48af-b0fb-e3889222c77b`、Run `4707f58d-6445-4704-971c-5104c35fc077`，实际 scope 仅 `shop/orders.py`。独立正式本地回归 Run `132ec7e6-dae9-4c29-96a2-8784355e3915` 为 223 tests / exit 0；Apply+Verify 自身绑定的是 `python-syntax`，不是 `python-regression`，录制需明确区分。另一个隔离故障基线 `E:\codex-local\shopsphere-recording-baseline-20260926\training-projects\shopsphere-bugfix-challenge` 的精确并发用例已复现 `AssertionError: 2 != 1`；个人 Agent 读代码并定位了根因，但该路径未注册/信任，DevPilot 测试返回 `HTTP 409: PROJECT_TEST_INVALID`，所以这份隔离基线尚无 Agent ChangeSet，也未 Apply/回滚。不能把先前已修复项目上的回执说成这份隔离基线已完成交付。
- DevPilot 全量：`577 passed, 5 skipped`，exit 0。Nanobot 完整测试集：`6077 passed, 42 skipped, 18 failed`，exit 1。失败分类：10 项检查旧英文错误/子 Agent 文案；1 项媒体测试 fixture 缺 `plugin_manager`；7 项 MCP/WebFetch 网络安全测试失败。它们复现于未修改的实现/测试路径，但尚未对精确基线 SHA 作 A/B；因此仍不能宣称全量绿。改变范围定向测试 `278 passed, 1 skipped`，Ruff 和 `git diff --check` 通过。跨 Run 的隐私能力确认继续 fail closed，若要让旧候选在新 Run 接续需另行批准设计；ShopSphere 隔离基线的 Agent ChangeSet / disposable Rollback 和实时跨会话展示未完成。Standards 复审 PASS；Spec 复审确认 Run 绑定符合冻结规格，但 DH-01 整体验收仍因上述未完成项为 FAIL/NO-GO。完整正式录制仍为 **NO-GO**。
