# Security fix batch — Standards review

## Review identity

- Axis: repository standards, Fowler-smell baseline, implementation quality and test quality.
- Reviewer: Volta (`01a0a8da-ed41-7732-8b2e-90b6b59b80fa`).
- Required reading: root `AGENTS.md`, vendored Matt Pocock skills, ADR-0004, and CURRENT_PLAN sections 0, 6 and 7.

## Review timeline

### Batch candidate `916a5464...4a7fc217`

Verdict: `CHANGES_REQUESTED`.

The reviewer identified a media cache header that could outlive the signed URL, and check-then-use reads in media/code access. The review also recommended an execution-level regression for `_FIXED_RUNNER_SOURCE`. These findings were retained as the acceptance checklist for the next batch.

### Candidate `4a7fc217...5012ba76`

Verdict: `CHANGES_REQUESTED`.

The cache lifetime and actual fixed-runner subprocess regression were accepted. The remaining findings were Windows media/code-access parent-directory TOCTOU and the need to avoid relying on a POSIX-only no-follow implementation on the actual Windows platform.

### Candidate `49216f385...3ad4f937`

Verdict: `PASS`.

The final delta added `O_NONBLOCK` to POSIX final-file opens. The reviewer confirmed that FIFO/special-file opens cannot block before `fstat`, while regular-file behavior, Windows handle containment, component-wise POSIX traversal, and cleanup remain intact. No new Fowler smell, scope creep, resource leak, or cross-platform regression was found.

## Evidence

- DevPilot targeted code-access/candidate tests: passed.
- nanobot signed-media route tests: `16 passed`.
- DevPilot full suite in the integrated nanobot environment: passed to 100%.
- Ruff for both `devpilot-agent` and `nanobot`: passed.
- `git diff --check`: passed for each reviewed candidate delta.
