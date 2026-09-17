# Training project upload — Spec review

- Fixed point: `c6b4725`
- Candidate: `15b0432`
- Diff: `git diff c6b4725...15b0432`
- Reviewer: Spec subagent `01a0ad94-b1f1-79a1-b8ce-13b853596bea`
- Governing references: `AGENTS.md`, `CURRENT_PLAN.md` sections 0/6/7, and ADR-0004.

## Verdict

**PASS**

The review confirmed that the GitHub branch contains the complete `python-project-template-bugfix`
fixture, including its entry point, tests, challenge instructions, dependencies, and three project
Skills. The documented baseline remains `3 failed, 2 passed`; the FC flow documents temporary pytest
installation, the expected `5 passed` verification, and rollback to the failing baseline.

The clone/start/register/trust flow is executable on a new machine: it handles first-start token
creation, preserves the token in the local user environment, and rebuilds `$repoRoot` and `$project`
in the second PowerShell window before calling `/v1/projects` and `/v1/projects/{id}/trust`.
Historical Apply → Verify → Rollback evidence is explicitly separated from results that must be
re-verified after moving to another computer. No incorrect claim is made that the current model
already produced a formal Bug-fix Diff.
