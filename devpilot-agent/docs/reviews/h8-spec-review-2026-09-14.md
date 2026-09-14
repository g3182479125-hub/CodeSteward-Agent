# H8 Spec review — 2026-09-14

Reviewer ID: `H8-SPEC-DIRECT-2026-09-14`

Scope: current working tree; `LIVE_HARNESS_EXTENSION.md` sections 5.5, 5.6,
and H8; ADR-0004; H8 implementation, tests, and evidence.

## Acceptance review

| Item | Decision | Evidence checked |
| --- | --- | --- |
| H8-A | PASS | Same session/task default→plan→default continuity tests. |
| H8-B | PASS | One tool dispatch, fact merge, and duplicate-execution prevention tests. |
| H8-C | PASS | Switch while a tool waits, then automatic safe-boundary continuation. |
| H8-D | PASS | New-Driver initialization failure preserves the original Driver. |
| H8-E | PASS | Uncertain side effects recover through query/pause rather than replay. |
| H8-F | PASS | Independent confirmation, FC validation, and cancellation paths. |
| H8-G | PASS | Database-before-route, route-before-session-display, and result-save crash windows. |
| H8-H | PASS | Stateful plan switching, restart recovery, and completed-position preservation. |
| H8-I | PASS | Redacted remote projection, Host validation, durable budgets, and restart behavior. |

## Commands and decision

The reviewer ran the focused combined H8 suite and reported `88 passed in
4.33s`, then ran Ruff and `git diff --check`, both passing.  The Windows
long-path remote-strategy suite also reported `8 passed`.

No findings remain.  The Windows extended-length filesystem repair is scoped
to Windows and does not alter non-Windows path behavior.

**Spec decision: PASS.**
