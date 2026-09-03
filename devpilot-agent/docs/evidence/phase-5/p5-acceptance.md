# P5 Acceptance Evidence

## Scope

P5 delivers the local API contract, durable event projection, optional React
console, and Skills/Security views. nanobot's chat WebUI remains the natural
language entry point.

## Verified locally

- P5 API/SSE contract tests: 12 passed.
- Full Python suite: 171 passed, 3 skipped.
- Ruff: passed.
- Pyright: passed after syncing the E-drive project environment.
- Web TypeScript check: passed with `npm test`.
- Web production build: passed with Vite.
- Web dependency installation: `pnpm install --frozen-lockfile` passed.
- Local console smoke: `npm run smoke` passed against ports 18800/18801.
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
- event payloads are already redacted by the storage layer.
- the UI does not store approval tokens or access SQLite/project files.
- an unconfigured execution backend is displayed as unavailable, not healthy.

## Review gate status

The required parallel Standards and Spec reviews were dispatched twice after
the fixes. Both attempts failed in the subagent service with HTTP 404 from
the Codex responses endpoint. This is recorded as a process blocker in
`docs/reviews/p5-final-standards.md` and `docs/reviews/p5-final-spec.md`; it
must not be represented as a clean reviewer approval.
