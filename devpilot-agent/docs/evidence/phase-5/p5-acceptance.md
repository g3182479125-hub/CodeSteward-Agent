# P5 Acceptance Evidence

## Scope

P5 delivers the local API contract, durable event projection, optional React
console, and Skills/Security views. nanobot's chat WebUI remains the natural
language entry point.

## Verified locally

- P5 API/SSE contract tests: 22 passed.
- Full Python suite: 182 passed, 3 skipped.
- Ruff: passed.
- Pyright: passed after syncing the E-drive project environment.
- Web TypeScript check: passed with `npm test`.
- Web production build: passed with Vite.
- Web dependency installation: `pnpm install --frozen-lockfile` passed.
- Local console smoke: `npm run smoke` passed against ports 18800/18801.
- Browser smoke: `pnpm run browser-smoke` is committed and uses an ephemeral
  fake API fixture; the page-level check covers Dashboard, API online,
  ChangeSet accept/stop/rollback controls, and Security/Backends unavailable
  labeling. It also forces an SSE disconnect and verifies the polling fallback;
  a CDP-driven interaction check verifies cancelled confirmation causes no POST
  and confirmed delivery submits exactly once.
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
- an unconfigured or unverified execution backend is displayed as unavailable,
  not healthy.

## Review gate status

The final parallel Standards and Spec review rerun is pending against the
fixed point `5860598`. P5 is not certified and P6 must not start until both
reports return PASS for `git diff 5860598...HEAD`.
