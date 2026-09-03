# P5 Acceptance Evidence

## Scope

P5 delivers the local API contract, durable event projection, optional React
console, and Skills/Security views. nanobot's chat WebUI remains the natural
language entry point.

## Verified locally

- P5 API/SSE contract tests: 8 passed.
- Full Python suite: 168 passed, 3 skipped.
- Ruff: passed.
- Pyright: passed after syncing the E-drive project environment.
- Web TypeScript check: passed.
- Web production build: passed with Vite.
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
