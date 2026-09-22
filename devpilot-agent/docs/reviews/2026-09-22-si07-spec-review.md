# SI-07 Spec Review

日期：2026-09-22  
固定点：`41df363`  
候选：`dd171a6`  
审查轴：SI-07 originating specification

## Verdict

PASS。当前没有 P1/P2；SI-07 的退出条件已满足。

## 已核对

- 基线和候选 Replay 使用不同、同源、受信的 Git worktree；真实目录不写入；
- 三个 Profile 的 deterministic grader 不接受模型文本自报通过；
- timeout、cancel、infrastructure error 和 unknown provider usage fail-closed；
- provider 调用前扣除 prompt reservation，持久化边界再次执行单次和聚合预算门禁；
- ReplayTrial 和 EvaluationReport 绑定 Case、snapshot、Skill hash、参数、trace/output 引用；
- repo-analysis 反馈路径来自真实 `read_code` 的 SHA-256 证据，`--expected` 必须命中本次读取路径；
- 新 Run 清理旧证据，避免不同 Run 之间复用路径；
- Replay 通过只产生 `pending_confirmation`，不自动激活；
- 83 项相关 pytest、Ruff、focused Pyright 和差异检查通过。

## 范围边界

SI-08 的用户确认、恢复、Cron 通知和端到端合成 Git fixture 演练仍未实现，不能由本报告推断为已完成。

