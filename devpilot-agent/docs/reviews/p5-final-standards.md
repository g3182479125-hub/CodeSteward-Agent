# P5 Final Standards Review

## Status

The Standards reviewer completed its inspection of `git diff 7ec7bd6...HEAD`,
but the review service also reported an intermittent 404 during the run. The
report found no code-level hard documented-standard violation.

The review service error was:

`unexpected status 404 Not Found: Unknown error, url: https://chatgpt.com/backend-api/codex/responses`

Per `AGENTS.md`, the service error remains a process blocker for certification.
Local quality gates are recorded in `docs/evidence/phase-5/p5-acceptance.md`.

## Standards report

- No hard documented-standard violation was found.
- Judgement-call: `control_plane.py` has divergent change because it now
  handles idempotency, project registration, HTTP contracts, SSE, catalogues,
  and route registration.
- Judgement-call: HTTP handlers reach directly into `manager.store`.
- Judgement-call: `/api/v1` and `/v1` route decorators duplicate registration;
  this is justified by the compatibility requirement.

## Pre-fix findings addressed

The earlier Standards review identified credential-redaction gaps, stale
idempotency reservations, forwarded-request handling, absolute path exposure,
missing frontend event listeners, and missing dependency reproducibility.
The current code adds broader redaction, aborts failed reservations, rejects
forwarded loopback requests, removes local paths from canonical responses,
adds SSE fallback/listeners, and pins the web dependency graph with pnpm.
