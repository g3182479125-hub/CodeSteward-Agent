# Project test recovery fault recovery Standards review

- Baseline: `3dfffc5`
- Candidate: `5c186c1`
- Scope: `git diff 3dfffc5...5c186c1`
- Reviewer: attached Standards subagent `01a0a424-5c18-70c0-a90e-69c4ea4124ed`
- Result: **PASS**

The incremental review found no standards or Fowler-smell hard issue. The FC
adapter maps timeout/deadline failures to `FC_TIMEOUT` and connection reset,
disconnect, and service-unavailable failures to `FC_PROVIDER_UNAVAILABLE`.
Only those two provider-transient codes enter the existing bounded recovery
allowlist; ordinary create/upload/execute failures, dependency failures,
assertion failures, and unknown receipts remain non-retryable.

Evidence: the adapter and control-plane fault-injection tests passed, the
targeted recovery set passed (`61 passed`), Ruff passed, and `git diff --check`
passed. Live cloud provider fault injection was not part of this local review.
