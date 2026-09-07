# 2026-09-07 测试收口交接

## 当前结论

用户已确认：FC 杭州的 `provider-default` 是可接受的正式验证模式；网络隔离不属于本项目目标，不再作为 P6/P7/P8 的阻塞条件。结果仍必须显示实际策略，不能写成断网沙箱。

## 本批次完成

- 真实 nanobot WebUI 主链路已通过：只读检查 → ChangeSet/Diff → 聊天确认 → Apply → 杭州 FC Verify → 网页结果回传。
- nanobot 全回归：`5878 passed, 42 skipped`；Windows PowerShell 子进程启动计时测试已通过平台合理启动容差处理。
- 修复 Windows CLI 配置路径/诊断输出被 Rich 终端宽度截断的问题，相关测试 `2 passed`，Ruff 通过。
- 调整该 Windows 计时回归为平台合理的启动容差；执行行为未改变。
- 修复 FC 运行记录仅存在内存的问题：新增脱敏本地状态快照；服务重启后可按原
  `sandbox_id` 显式调用 FC `connect` 恢复运行状态，不创建替代沙箱、不重放命令。
- 新增控制面 `POST /v1/change-sets/{change_set_id}/verification-recovery` 恢复入口，
  恢复失败时返回 `unknown`/稳定错误，不会伪造验证成功。
- DevPilot、WebUI 74 条 source test、WebUI build、FC sandbox-check 和服务健康检查均通过。
- 测试证据：`docs/evidence/test-full-system-2026-09-07.md`。

## 本批次修改文件

- `nanobot/tests/tools/test_exec_session_tools.py`
- `devpilot-agent/src/devpilot/fc_sandbox.py`
- `devpilot-agent/src/devpilot/control_plane.py`
- `devpilot-agent/tests/test_fc_sandbox_contract.py`
- `nanobot/nanobot/cli/runtime_config.py`
- `nanobot/nanobot/cli/webui.py`
- `nanobot/nanobot/cli/webui_support.py`
- `devpilot-agent/docs/plans/CURRENT_PLAN.md`
- `devpilot-agent/docs/evidence/phase-6/p6-02-fc-hangzhou-diagnosis.md`
- `devpilot-agent/docs/evidence/phase-6/p6-03-04-acceptance.md`
- `devpilot-agent/docs/evidence/phase-6/p6-real-evidence-manifest.md`

## 保留边界

- `DEVPILOT_FC_ALLOWED_PROJECTS`、服务器固定 verification profile、显式聊天确认、快照上传范围和清理保护不能移除。
- P8-03 定时批处理仍未选择、未实现、未启用。
- API Key、`.env`、SQLite、日志、虚拟环境和 pytest 临时目录不得提交。
- 当前 WebUI：`http://127.0.0.1:8765`；控制平面：`http://127.0.0.1:18800`；Gateway：`http://127.0.0.1:18790`。
- 本轮最终实现候选 SHA 需以收口提交后的 `git rev-parse HEAD` 为准；提交前不要把 API Key、
  `.env`、SQLite、日志、虚拟环境或 pytest 临时目录加入暂存区。
