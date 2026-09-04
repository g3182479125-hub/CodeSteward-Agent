# P5 Acceptance Evidence

> 当前状态：`in_progress`。本文件中的历史结果只用于追溯；本轮事件桥接和 Action Broker
> 尚未完成 Gateway 真实接入、nanobot 聊天 E2E 和新的并行 Standards/Spec review。

## Scope

Historical P5 evidence: the local API contract, durable event projection and
the optional React console were verified before ADR-0003. The React console
scope is now superseded; nanobot's chat WebUI is the only current user entry
point. The current implementation additionally contains event bridge hardening
and the Action Broker core, but does not yet certify the full confirmation-to-action path.

## Verified locally

- P5 API/SSE contract tests: 23 passed.
- Full Python suite: 182 passed, 3 skipped.
- Ruff: passed.
- Pyright: passed after syncing the E-drive project environment.
- React/Vite build, console smoke and browser smoke: passed before ADR-0003;
  retained as historical evidence only and removed from the current gate.
- `git diff --check`: passed.
- tracked secret-pattern scan: no secret-like tokens found.
- Current focused bridge/Broker/API tests: `35 passed`.

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

The historical parallel Standards and Spec review passed against fixed point
`5860598`; that result certifies only the historical console scope. A new parallel
review is required for the current event bridge/Broker diff after it is committed.
The current nanobot Gateway confirmation injection, real ControlPlane callbacks,
durable Broker consumption and chat E2E are still open. P6 has not started.
