# 2026-09-07 测试收口交接

## 当前结论

用户已确认：FC 杭州的 `provider-default` 是可接受的正式验证模式；网络隔离不属于本项目目标，不再作为 P6/P7/P8 的阻塞条件。结果仍必须显示实际策略，不能写成断网沙箱。

## 本批次完成

- 真实 nanobot WebUI 主链路已通过：只读检查 → ChangeSet/Diff → 聊天确认 → Apply → 杭州 FC Verify → 网页结果回传。
- nanobot 全回归（2026-09-08 正确环境）：`5878 passed, 42 skipped`，退出码 `0`，用时 `302.63s`；命令详见测试证据，临时目录位于仓库外。
- 修复 Windows CLI 配置路径/诊断输出被 Rich 终端宽度截断的问题，相关测试 `2 passed`，Ruff 通过。
- 调整该 Windows 计时回归为平台合理的启动容差；执行行为未改变。
- 修复 FC 运行记录仅存在内存的问题：新增脱敏本地状态快照；服务重启后可按原
  `sandbox_id` 显式调用 FC `connect` 恢复运行状态；已结束运行复用持久化结果，不创建替代沙箱、不重放命令。
- 修复恢复快照输出的敏感信息风险：持久化前统一执行敏感信息和绝对路径脱敏，并由契约测试
  验证类似 `E2B_API_KEY=...` 的内容不会在恢复结果中出现。
- 新增控制面 `POST /v1/change-sets/{change_set_id}/verification-recovery` 恢复入口，
  恢复失败时返回 `unknown`/稳定错误，不会伪造验证成功。
- DevPilot、WebUI 74 条 source test、WebUI build、FC sandbox-check 和服务健康检查均通过。
- 测试证据：`docs/evidence/test-full-system-2026-09-07.md`。
- 当前交接时间点：2026-09-08；P6 的 provider-default 口径和恢复实现已完成，P6-04 双轴
  复审结果以最终提交后的复审记录为准。

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
- 本轮实现候选基线：`3bab72b78908d5b9d397c78dbaf24bfdc9345c91`；本次文档修正提交后的
  HEAD 是最终复审候选。不要把 API Key、`.env`、SQLite、日志、虚拟环境或 pytest 临时目录
  加入暂存区。

## 2026-09-08 本机接续记录

- 已从 `origin/main` 快进同步至 `fed01d2`，无冲突；当前本地分支为
  `handoff-2026-09-01`，未推送的本地修复提交为 `8d8196a`、`30a77f8`、
  `d97e5d0`、`4c6ba7a`、`c13871c`。
- 本批次修复 FC 状态快照的跨实例持久化竞态：加入跨进程文件锁、revision/CAS
  防陈旧覆盖、持久 `run_id` 去重、写失败临时文件清理，以及重启后对
  `cleanup_error`（含已停止沙箱）的有界清理重试；普通停止状态仍返回
  `unknown/FC_RESULT_UNAVAILABLE`，不伪造恢复成功。
- 验证：FC 契约测试 `27 passed`；DevPilot 集成全量测试通过；DevPilot Ruff、
  Secret Scan、`git diff --check` 通过；Node fixture `npm test` 为 `2 passed`。
  本批次通过同一对 Standards/Spec subagent 做增量复审，最终均为 `PASS`。
- 当前工作区仍保留用户未提交的 Node fixture 修改：
  `tests/fixtures/node-project/src/index.js` 和 `test/index.test.js`；它们未纳入
  本批次提交。上述五个修复提交尚未推送，下一台电脑可先查看本文件后再决定是否推送。
