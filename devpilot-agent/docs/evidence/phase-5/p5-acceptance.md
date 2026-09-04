# P5 Acceptance Evidence

## Scope

Historical P5 evidence: the local API contract, durable event projection and
the optional React console were verified before ADR-0003. The React console
scope is now superseded; nanobot's chat WebUI is the only current user entry
point. This file is retained for historical traceability and is not a current
frontend acceptance claim.

## Verified locally

- P5 API/SSE contract tests: 23 passed.
- Full Python suite: 182 passed, 3 skipped.
- Ruff: passed.
- Pyright: passed after syncing the E-drive project environment.
- React/Vite build, console smoke and browser smoke: passed before ADR-0003;
  retained as historical evidence only and removed from the current gate.
- `git diff --check`: passed.
- tracked secret-pattern scan: no secret-like tokens found.

The three skipped Python tests require Windows `SeCreateSymbolicLinkPrivilege`;
they are existing symlink escape tests and are reported as skips rather than
treated as successful security coverage.

## Safety evidence

- API remains loopback-only.
- JSON responses carry a bounded request ID; idempotent replays retain their
  original response identity for compatibility.
- SSE uses database event IDs and `Last-Event-ID` replay semantics.
- event payloads are already redacted by the storage layer, including
  credential-shaped text and absolute filesystem paths.
- nanobot WebUI is the only intended user surface; the future local action
  Broker must not expose approval tokens to the model or chat messages.
- an unconfigured or unverified execution backend is displayed as unavailable,
  not healthy.

## Review gate status

The final parallel Standards and Spec review passed against the fixed point
`5860598`; both independent reports found no actionable code or scope issue
in `git diff 5860598...HEAD`. That result certifies the historical P5 scope.
ADR-0003 requires a new nanobot-only integration review before the amended P5
scope can be called certified. P6 has not started.
