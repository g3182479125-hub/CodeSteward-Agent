# Security fix batch — Spec review

## Review identity

- Axis: originating design/spec, security acceptance, scope and requirement coverage.
- Reviewer: Erdos (`01a0a8da-ef4b-7bc1-b898-abcd3c910da7`).
- Required reading: root `AGENTS.md`, vendored Matt Pocock skills, ADR-0004, and CURRENT_PLAN sections 0, 6 and 7.

## Review timeline

### Batch candidate `916a5464...4a7fc217`

Verdict: `PASS`.

The control-plane internal token, strict candidate builtin boundary, expiring signed media payload, loopback Compose binding and second code-access boundary matched the security audit and did not reopen completed H1–H11 scope.

### Candidate `4a7fc217...5012ba76`

Verdict: `CHANGES_REQUESTED`.

The reviewer confirmed that the cache TTL and fixed-runner regression were correct, but required closing the Windows/parent-directory path escape race and making exact signed-URL expiry fail closed.

### Candidate `49216f385...3ad4f937`

Verdict: `PASS`.

The reviewer confirmed that POSIX component-wise `O_NOFOLLOW` traversal plus Windows `GetFinalPathNameByHandleW` root containment close the prior path-escape finding, and `expires_at <= now` closes the exact expiry boundary. The final `O_NONBLOCK` delta closes the special-file blocking boundary. H1–H11 remain unchanged and no scope creep was introduced.

## Evidence

- `tests/test_p2a_code_access.py` and `tests/test_h5_candidate_runner.py`: passed.
- nanobot signed-media route suite: `16 passed`.
- DevPilot full integrated suite: passed to 100%.
- Lockfile and dev dependency remain consistent for `pytest-asyncio`.
- Untracked user directories were not included in any review or commit.

