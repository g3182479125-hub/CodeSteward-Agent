# WebUI Stop 回执门禁：Spec Review — b87781c

- Reviewer: `01a0a7d2-fe09-7992-ac17-abe650e17de1`
- Axis: Spec
- Base: `6b0a007`
- Candidate: `b87781c`
- Scope: `git diff 6b0a007...b87781c`

## Verdict

**PASS**。

候选保留 `/stop` 的 priority system-command 通道，也保留“不创建用户 `/stop` 气泡”的契约。只有收到同一 system `turn_id` 的后端 `message` 或 `turn_end` 回执时，才清理流、计时与 client 的本地 run 状态；拒绝、socket 关闭和超时都保持活动回合权威，因而不会让下一条消息与尚未取消的任务竞争。未发现缺失、错误假设或范围蔓延。

## Verification and limits

评审覆盖交接文档的停止后复用要求与交互超时计划的“后端终态为唯一事实来源”原则。自动化验证通过；真实 LLM 网页回合因本机模型未配置而待用户配置后按本文复验。
