# Security fix incremental review — `4a7fc217...5012ba76`

- Axis: Standards.
- Reviewer: Volta (`01a0a8da-ed41-7732-8b2e-90b6b59b80fa`).
- Verdict: `CHANGES_REQUESTED`.
- Scope: only the delta from `4a7fc2176fb562d9337b8077105ffc815862667c` to `5012ba76e0892a1f962f3e13663e7670fe6d8ed6`.

The reviewer accepted the expiry-based cache header and actual fixed-runner subprocess test. It requested handle-based path reads rather than check-then-use reads, noting that the current Windows platform cannot use POSIX `O_NOFOLLOW` and that parent-directory replacement remained a race. The finding drove the next patch: POSIX descriptor-relative traversal, Windows final-handle containment checks, single-handle bounded reads, and explicit close paths. The `pytest-asyncio` lockfile change was judged in scope because it makes the existing async test suite executable.

Evidence at this candidate: DevPilot relevant tests passed, nanobot media route `16 passed`, Ruff and `git diff --check` passed.

