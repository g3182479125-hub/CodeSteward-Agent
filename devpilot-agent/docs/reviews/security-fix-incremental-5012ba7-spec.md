# Security fix incremental review — `4a7fc217...5012ba76`

- Axis: Spec.
- Reviewer: Erdos (`01a0a8da-ef4b-7bc1-b898-abcd3c910da7`).
- Verdict: `CHANGES_REQUESTED`.
- Scope: only the delta from `4a7fc2176fb562d9337b8077105ffc815862667c` to `5012ba76e0892a1f962f3e13663e7670fe6d8ed6`.

The reviewer accepted the remaining fixed-runner and cache requirements, and required two spec-level corrections: reject a signed URL when `expires_at == now`, and close the parent-directory TOCTOU on Windows and POSIX. The next candidate implemented both. No H1–H11 work was reopened.

Evidence at this candidate: DevPilot code-access/candidate tests passed, nanobot media route `16 passed`, Ruff passed, and the delta was clean under `git diff --check`.
