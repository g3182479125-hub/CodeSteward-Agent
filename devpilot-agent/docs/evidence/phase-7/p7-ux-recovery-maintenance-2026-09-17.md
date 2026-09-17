# P7 用户体验恢复维护记录（2026-09-17）

## 范围

本次是对现有 P7 WebUI / nanobot 对话流程的用户体验维护，不新增工具权限，也不改变审批、执行、回滚和未知副作用调用不重放等安全边界。

目标是让用户在工具失败、诊断暂停或任务模式操作失败后，仍然能看懂发生了什么，并能继续发普通消息或采取明确的下一步。

## 已处理的问题

1. 本轮处理结束但没有最终答复时，不再显示英文兜底句；现在会用中性中文说明没有整理出最终答复，允许继续发消息，并给出重新整理结果的自然说法。
2. 重复错误或诊断分支用尽时，不再把 `PathBoundaryError` 等内部异常原文直接展示给用户；常见路径、权限、超时错误会转换成中文说明，并明确提示提供新路径、查看状态或继续普通问题。
3. `/task-profile` 的成功、失败、关闭和权限拒绝提示改成面向用户的中文；中途切换模式会明确引导用户点击“新建话题”，不再泄露 Python 异常。
4. 输入框 `/` 菜单增加“选择任务模式”和“管理临时工具”的自然语言标题与说明，并同步所有已注册语言，避免切换语言后菜单结构不完整。
5. 录制手册与本地开发手册同步改为描述实际用户可见的中文提示，不再要求用户记住内部工具名或输出字段。

## 验证证据

- 后端相关回归命令（工作目录 `nanobot`）`$env:PYTHONPATH='../devpilot-agent/src;.'; python -m pytest -q tests/command/test_model_command.py tests/agent/test_task_profiles.py tests/agent/test_loop_runner_integration.py tests/agent/test_runner_core.py`：`76 passed`，退出码 0。
- WebUI 相关回归命令 `npm test -- --run src/tests/i18n.test.tsx src/tests/thread-composer.test.tsx src/tests/settings-view.test.tsx`：3 个文件、160 项通过，退出码 0。
- WebUI lint 命令 `npm run lint`：通过，退出码 0。
- WebUI production build 命令 `npm run build`：通过，退出码 0。
- 实现候选提交范围 `git diff --check aaf2a2c...990dfb3`：通过，退出码 0；随后仅追加本证据文档的记录修订，没有再修改实现代码。Git 的 LF/CRLF 提示是现有工作树换行策略提示，不是内容错误。
- 后端全量扫描命令 `python -m pytest -q --tb=short --maxfail=3`：扫描到 `888 passed, 12 skipped, 3 failed` 后按 `--maxfail=3` 停止。失败用例为 `test_agent_loops_do_not_share_exec_session_managers`、`test_h7_default_driver_runs_each_task_profile_main_chain[bug-fix]` 和 `test_main_filesystem_guard_and_controlled_shell_boundary`；原因分别是测试环境未注册 `exec` 工具、缺少 effective `bug-fix` Skill 快照、最小配置未启用 `ExecTool`。相关失败未被隐藏，录制前如需全绿应先准备 Harness/Skill/Exec 测试环境。

## 用户验收口径

在 WebUI 中输入 `/`，可以看到“选择任务模式”；选择后看到中文成功提示。让一次工具调用失败或让诊断暂停后，继续发送“你好”或“把刚才的结果总结一下”，对话不应再被固定成英文失败句，也不应要求用户先恢复内部运行引用。
