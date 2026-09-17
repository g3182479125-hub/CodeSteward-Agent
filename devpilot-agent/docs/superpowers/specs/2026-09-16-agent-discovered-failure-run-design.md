# Agent 自主发现失败并登记 Failure Run 设计

日期：2026-09-16  
新增节点：N1，受控测试执行与 Failure Run 自动登记

## 目标

用户把项目交给 Agent 后，只需用自然语言要求检查和修复。Agent 自己读取项目、判断测试入口、执行必要测试；测试失败时，服务端自动保存可供 `bug-fix` 提案引用的 Failure Run。用户不需要预先知道缺陷，也不需要手动复制终端输出。

## 用户路径

`检查项目是否有问题并修好` → Agent 读取项目与测试配置 → Agent 调用受控测试执行工具 → 服务端重新读取并哈希实际项目文件、在 FC sandbox 执行测试 → 退出码非 0 时持久化 Failure Run → Agent 用该 Failure Run 与分析证据生成 Diff 提案 → 用户只在 Apply、Rollback 等既有修改边界确认。

## 设计

### 1. 新工具与权限

新增 DevPilot 工具 `devpilot_test_run`，只在已有 `bug-fix` Profile 与当前 WebUI 已授权项目范围内可见。Agent 根据已读取的项目事实选择受支持的测试种类并自行调用，无需逐次聊天确认。

工具不会执行宿主机 Shell，也不会接受模型提供的文件内容、退出码或日志作为事实。Control Plane 根据已绑定的可信项目路径重新读取文件、应用既有敏感文件排除与大小限制，生成 `ExecutionRequest`，再交给既有 FC 执行后端。`provider-default` 继续如实标注，不宣称网络隔离。

### 2. 命令处理

第一版支持项目内常见测试命令：Python `pytest`、Node `npm test`。Agent 可以根据项目的 `pyproject.toml`、`pytest.ini`、`package.json`、测试目录和已读取事实自行选择；不要求用户预先给出命令。

命令不是开放的宿主 Shell：必须在已绑定项目的 sandbox 工作目录内运行，不允许 shell 串联、重定向、模型提供的下载依赖、网络凭据或越出上传清单的路径。Python 项目测试可按 ADR-0008 在测试命令前执行服务端固定的 pytest 启动安装；该安装不接受模型参数，且失败时只返回未知执行结果。不存在可识别测试入口时，工具返回结构化“未找到测试入口”，Agent 继续只读分析并如实报告，不杜撰 Failure Run。

### 3. Failure Run 事实

测试执行创建独立 Run，持久化：项目归属、命令、执行后端、sandbox ID、退出码、截断标记、stdout/stderr 摘要，以及目标文件的执行前 SHA-256。退出码非 0 时写入 `test_check.failed` 事实并返回 `failure_run_id`；退出码为 0 只返回成功 receipt，不伪造失败证据。

`BugFixWorkflow.propose` 保持既有 `failure_evidence_ids` 与 before-hash 校验，不放宽门禁；新工具生成的 Run 只是其可信输入来源之一。提案时若相关文件哈希已经变化，仍以 `failure_evidence_changed` 停止。

### 4. 失败、取消与恢复

超时、上传/执行失败、取消、输出过大或 sandbox 回执不完整，都持久化为执行失败/未知 receipt，但不登记为业务测试 Failure Run，也不能推动修复提案。系统只展示已知事实与下一步。

测试运行复用既有任务预算与聊天流式活动展示。取消与重连沿用任务运行时的既有机制；当执行端只返回基础设施错误或未知结果时，不会生成可用于提案的 Failure Run。

## 验收

1. Python fixture：Agent 可自行运行 `pytest`，失败后拿到真实 `failure_run_id`，并能被 `bug-fix` 提案接受。
2. Node fixture：识别 `package.json` 测试脚本并生成同样的可信失败证据。
3. 无测试入口、命令越界、敏感文件、非零基础设施错误、超时、取消、重复提交和文件外部改动均不能生成可用于修复的 Failure Run。
4. Apply、Verify、Rollback、已有 Profile、FC 上传排除、取消与回执门禁不被绕过。
5. 真实 WebUI 中用户仅描述问题；Agent 自行测试→发现失败→提案，且不会要求用户预先告诉它测试失败内容。

## 非目标

- 不增加宿主机任意 Shell、模型可控的依赖安装、自动 Apply、自动 Git 提交或自动发布；
  固定的 pytest 启动安装例外见 ADR-0008。
- 不信任模型生成的测试输出、聊天文本或用户粘贴日志来制造 Failure Run。
- 不重开已完成的 P0–P8、H1–H11 节点；N1 是独立增量。
