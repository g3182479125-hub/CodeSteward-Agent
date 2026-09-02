# P4-01 修复后复核记录

Date: 2026-09-02
Implementation commit: `2958ae3`
Decision: `approve` (independent final Review; P4-02 unlocked)

## Closed findings

- B-01：从 `pyproject.toml` 移除 `devpilot_execute_changeset`、`devpilot_verify_changeset`、
  `devpilot_rollback_changeset` 三个 nanobot entry point；新增的
  `devpilot_request_approval` 不接收、不签发、不返回 capability token。
- M-01：Skill allowlist 与 nanobot entry point 集合逐项相等，并有合同测试；Diff 通过
  `devpilot_get_changeset` 提供，审批请求通过无 token 的 intent endpoint 提供。
- M-02：manifest 改为绑定本次实现提交、冻结计划 v2.2.0、当前 uv.lock 和当前测试结果。
- M-03：项目记录新增 `trust_state`、`repo_fingerprint`、`allowed_root_id`；项目默认
  `untrusted`，只有控制面显式 trust 后才允许 R1/R2，指纹或 root 绑定变化立即拒绝。
- B-02：无 `ProjectStore` 的临时管理器现在只能生成 Preview；审批意图、批准、Apply、
  Verify 和 Rollback 均以 `PERSISTENCE_REQUIRED` 失败关闭，并有回归测试覆盖。
- M-04：迁移配置只在使用 Alembic 默认占位地址时读取 `DEVPILOT_DATABASE_URL`；测试显式
  指定的数据库地址优先，避免外部环境污染验收结果。

## Verification

- `DEVPILOT_DATABASE_URL=sqlite:///E:/codex-local/pytest-temp/should-not-be-used.db uv --no-cache run pytest --tb=no -r a -o addopts='' --basetemp E:/codex-local/pytest-temp/review-basetemp -p no:cacheprovider`：`124 passed, 3 skipped`
- `uv --no-cache run ruff check --no-cache src scripts tests`：passed
- `uv run pyright`：0 errors, 0 warnings, 0 informations
- `uv --no-cache lock --check`：passed
- `uv run python scripts/secret_scan.py`：passed
- `uv run python scripts/check_markdown.py`：passed
- `uv run alembic upgrade head`：`0007_project_trust`
- `git diff --check`：passed

最终独立只读 Review 已通过；P4-02 已解锁。历史 Review 文档中的旧结论保留为历史记录，
不代表当前实现状态。
