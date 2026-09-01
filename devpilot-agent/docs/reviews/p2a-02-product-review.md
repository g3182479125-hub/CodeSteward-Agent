# P2A-02 Product Review

## Decision

`approve` — the node delivers its frozen R0 code-search/read boundary and can proceed to P2A-03.
This approval does not approve the imported patch/control-plane candidate.

## Evidence reviewed

- Frozen P2A-02 requirement and S-01/S-02 safety expectations.
- `src/devpilot/code_access.py`, storage lookup and nanobot Tool adapters.
- P2A-02 unit/integration tests and full repository quality Gate.
- Runtime registration in `pyproject.toml` and operator guidance in `README.md`.
- Imported P2A/P2B review and the default-off control-plane guard.

## Acceptance mapping

| Acceptance point | Evidence | Result |
|---|---|---|
| Search registered project code | Literal/glob search Tool and service tests | Pass |
| Read bounded source ranges | Line/byte/file limits and SHA-256 result tests | Pass |
| Stay inside trusted project | Stored project lookup plus real allowed-root recheck | Pass |
| Protect sensitive content | Filename/suffix rejection, binary skip and value redaction tests | Pass |
| Resist Windows aliases | Reserved device, ADS and trailing dot/space tests | Pass |
| Remain R0 | Tools declare read-only and have no project write path | Pass |
| Be operable through nanobot | Two registered entry points and adapter integration test | Pass |

## Cross-functional review

- Product: the Agent can now inspect a repository beyond metadata and gather targeted evidence for
  future plans and patches; `project_id` creates a clear inspect-then-read workflow.
- Architecture: code access depends on storage and `AllowedRoots` rather than accepting an
  arbitrary path on every call.
- Security: traversal, secret-like files, binary data, symlinks and Windows aliases fail closed;
  output is budgeted and redacted.
- QA: positive, limit, filter, unknown-project, unsafe-path, binary, Tool adapter and regression
  coverage pass. Symlink cases are present but skip on this Windows account due OS privilege.
- Operations: limits are constants, failures use stable codes, and no external service is required.

## Follow-ups

- Run the existing symlink cases in Linux CI or a Windows runner with symlink privilege.
- P2A-03 should reuse the same canonical path policy rather than introducing a second policy.
- P2A-04 must add durable recovery before any patch application is exposed to users.

## Gate

P2A-02: approved. Next eligible node: P2A-03. P2B remains blocked and default-off.
