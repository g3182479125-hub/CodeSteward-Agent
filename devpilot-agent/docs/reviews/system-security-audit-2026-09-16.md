# System security audit — 2026-09-16

## Scope and fixed point

- Fixed point: `4d6df54` (`codex/h11-complete-handoff`); `git diff 4d6df54...HEAD` was empty.
- Scope: nanobot WebUI/WebSocket, media and attachment routes, workspace/network/exec boundaries, Docker deployment; DevPilot control-plane authorization, code access, patch application, candidate execution and redaction.
- Method: read-only source audit, local runtime/configuration inspection, independent Standards and Spec reviews, and targeted automated checks. No product code or configuration was changed.

## Current local exposure

The running gateway listens only on `127.0.0.1:8765` and `127.0.0.1:18790`; no external channel is enabled. The local configuration has `tools.exec.enable=true`, `tools.restrictToWorkspace=false`, and no process sandbox. This does not create a remote listener, but it gives model-controlled shell/file actions the full authority of the signed-in Windows user when the Agent processes untrusted content.

The local configuration file is owned by the current user and the ACL check found no broad `Everyone`, `Users`, `Authenticated Users`, or `Guests` read/write rule.

## Confirmed findings

### SEC-01 — High: loopback control-plane routes can mint approval authority without a Gateway-bound user confirmation

`devpilot-agent/src/devpilot/control_plane.py` accepts any loopback HTTP client at the middleware boundary (1528–1539). The public `/projects/{project_id}/trust` and `/change-sets/{change_set_id}/approve` routes directly call `trust_project` and `approve` (1885–1893). `approve` creates and returns an apply/verify/rollback token (657–714), while `trust_project` accepts a fixed confirmation string (989–1007); neither verifies a WebUI-signed confirmation envelope, session binding, or a specific user event.

Impact: an untrusted process already running on the same machine can create trusted project state, mint a capability token, and apply an allowed-root patch without going through the WebUI confirmation flow. The service is intentionally loopback-only, so this is not an Internet-facing endpoint; it is a broken local privilege boundary and violates the user-confirmation contract.

Minimum remediation: remove or make internal the token-minting `/approve` and trust routes. Only the Gateway/ActionBroker should call them via a one-time, signed confirmation bound to the user session, action, ChangeSet hash and expiry. A permission-protected local IPC channel is stronger than a loopback HTTP endpoint trusted by every local process.

### SEC-02 — High: candidate source filter can be bypassed with indirect builtins access

`devpilot-agent/src/devpilot/candidate_runner.py:438–456` rejects direct forbidden calls and certain AST nodes, but allows attribute calls and indirect access through `__builtins__`. A non-executing validation check of a `transform` function using `getattr(__builtins__, "__import__")` returned `None` from `_validate_source`, proving that the source is accepted despite the pure-computation/no-import contract. Accepted source is later uploaded for FC execution (`candidate_runner.py:1730`).

Impact: a candidate can obtain module, process, or filesystem capabilities inside the FC runtime. Where the runtime reports `provider-default` rather than proven network denial, approved input could also be exfiltrated. This violates the H5/H11 pure-computation boundary.

Minimum remediation: do not treat an AST blacklist as execution isolation. Use a strict syntax whitelist and execute only with a minimal builtins mapping. Reject attribute calls, dangerous subscript access, and constructs beyond the explicitly supported expression/function subset. Do not permit real input in a candidate run unless the execution backend can attest to a no-network policy.

### SEC-03 — Medium: path validation has a time-of-check/time-of-use race

`devpilot-agent/src/devpilot/patches.py:531` validates components and resolves a path, then later uses ordinary path operations such as `unlink` (`409`) and `os.replace` (`650`). The code-read and snapshot paths have the same check-then-open pattern in `devpilot-agent/src/devpilot/code_access.py:134–152`.

Impact: a concurrent process that can modify a trusted project directory can replace a checked component with a symlink/reparse point after validation, potentially moving a read, write, or sandbox upload outside the allowed root.

Minimum remediation: use root-directory handles and no-follow operations for each path component; bind the final open/write to the checked object and revalidate final metadata. Resolving a string path before a later file operation cannot close this race.

### SEC-04 — Medium: signed media URLs have no expiry and do not require an API token

`nanobot/nanobot/webui/media_api.py:101–115` signs only the relative media path. `serve_signed_media` validates that HMAC and reads the file directly (`192–220`), while `nanobot/nanobot/webui/ws_http.py:846–859` does not impose an API-token check on the media route. The response is marked `private, max-age=31536000, immutable` (`media_api.py:225–229`).

Impact: anyone who obtains a valid media URL from browser history, an image embed, a proxy log, a screenshot, or an accidentally forwarded transcript can read that specific attachment until the gateway process restarts. Attachments can contain user images, audio, or staged agent output.

Minimum remediation: include an expiry and canonical path in the signed payload, validate a short TTL, and use a short private/no-store cache policy. Prefer an authenticated download endpoint for long-lived access.

## Deployment and configuration risks (not currently externally exposed on this machine)

### DEP-01 — High impact: Compose exposes the WebUI port on all host interfaces

`nanobot/docker-compose.yml:18–20` publishes `8765:8765`, unlike the gateway API's loopback-only binding. The WebSocket protocol supports query-string tokens and has no TLS by default. On a host where the in-container WebUI listens on a reachable interface, direct LAN/public exposure makes tokens observable and replayable.

Remediation: publish `127.0.0.1:8765:8765` by default; expose it publicly only through a TLS reverse proxy with deliberate authentication. Do not use query-string credentials as a long-term public authentication mechanism.

### DEP-02 — High impact design trade-off: bwrap Docker override widens the container kernel attack surface

`nanobot/docker-compose.bwrap.yml:1–6` adds `SYS_ADMIN` and disables AppArmor and seccomp so nested bubblewrap can work. The deployment guide documents this trade-off. It is not an independent application defect, but it must only be used for a trusted single-user environment; a dedicated VM, rootless/user-namespace configuration, or an external sandbox is safer for untrusted workloads.

### CFG-01 — High local risk: this machine runs Agent shell/file tools with full user authority

The current local configuration enables `exec`, leaves `restrictToWorkspace` false, and has no sandbox. On Windows, bwrap is unavailable, so prompt injection from web content, attachments, or installed skills can cause actions across files accessible to the logged-in user.

Remediation: use a dedicated low-privilege Windows account or VM for Agent development. Enable `tools.restrictToWorkspace=true`, set the workspace to a narrowly scoped directory, and disable `tools.exec` outside deliberate development sessions. Treat OS isolation as required when processing untrusted inputs.

## Evidence and checks

- Targeted nanobot WebUI/attachment/network security checks: `206 passed`.
- Independent Standards reviewer: `175 passed, 2 skipped`; confirmed SEC-04 and deployment risks, and found no path traversal/SSRF bypass in the reviewed WebUI paths.
- Targeted DevPilot control-plane, patch, code-access, action-broker, candidate and H11 tests: passed (with expected skipped cases).
- `pip-audit` against both project environments: no known dependency vulnerabilities.
- DevPilot deterministic secret scan reported only `tests/test_h5_candidate_runner.py`, caused by a non-production test fixture containing a pattern that resembles the scanner's API-key signature. No production secret was printed or confirmed.
- `nanobot` WebUI/attachments security regression tests: `307 passed`; separate route/media/network run: `206 passed`.

## Review separation

### Standards review

Reviewed the nanobot WebUI/WebSocket, media, static files, attachment ingress, workspace, SSRF, shell and Docker boundaries. It confirmed SEC-04, DEP-01 and DEP-02; it found the remaining WebUI path, token and attachment controls covered by tests.

### Spec review

Reviewed DevPilot against CURRENT_PLAN sections 0, 6 and 7 and the Live Harness extension. It confirmed SEC-01, SEC-02 and SEC-03. It also noted that `provider-default` is not a proof of network isolation and that redaction must not be treated as the primary secret boundary.

## Priority order

1. Block SEC-01 and SEC-02 before enabling candidate execution or expanding DevPilot use beyond controlled development.
2. Fix SEC-04 before sharing WebUI links or deploying behind a reverse proxy.
3. Resolve SEC-03 as part of the next path-boundary hardening batch.
4. Apply DEP-01 and CFG-01 configuration hardening immediately for any environment that processes untrusted web or attachment content.
