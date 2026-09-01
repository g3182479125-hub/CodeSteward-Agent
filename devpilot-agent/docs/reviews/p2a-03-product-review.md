# P2A-03 Product Review

## Decision

`approve` — the planning boundary satisfies the frozen P2A-03 node. Approval is limited to parsing,
canonicalization, preconditions, budgets and Diff construction; apply/rollback remains unapproved.

## Evidence reviewed

- Frozen P2A-03 requirement and S-01/S-02/S-03 cases.
- Immutable domain and patch records in `src/devpilot/patches.py`.
- Positive planning and reverse safety tests in `tests/test_p2a_patches.py`.
- P2A-02 public error normalization and the full repository quality Gate.
- Runtime entry points, confirming no patch/apply Tool is registered.

## Acceptance mapping

| Acceptance point | Evidence | Result |
|---|---|---|
| Immutable FilePatch/plan | Frozen dataclasses and mutation rejection test | Pass |
| Exact preconditions | Create/update/delete hash and content tests | Pass |
| Canonical scope | Relative path and case-insensitive identity checks | Pass |
| No overlapping targets | Exact, case-only and parent/descendant rejection tests | Pass |
| Bounded proposal | File, byte, changed-line and per-file tests | Pass |
| Reviewable output | Deterministic patch/scope hashes and unified Diff test | Pass |
| No premature write exposure | Only inspect/publish/search/read entry points registered | Pass |

## Cross-functional review

- Product: this creates a deterministic review object that can later power a user-visible Diff
  approval flow instead of asking the model to write files directly.
- Architecture: proposal construction is separated from control-plane transport and future
  ExecutionBackend/Sandbox integration.
- Security: path aliases, secrets, symlinks, stale hashes, overlap and resource abuse fail before
  write authorization.
- QA: the node has positive, negative, boundary and immutability coverage; three symlink tests are
  present but require a privileged Windows or Linux CI runner to execute.
- Operations: stable internal error codes and deterministic hashes support later audit records.

## Required next-node work

- Persist patch bodies/manifests and lifecycle state before entering `applying`.
- Recover interrupted applies at startup; lock the project when state cannot be proven.
- Ensure partial failure fully restores files and records any residual directories.
- Require independently authorized rollback and never expose capability secrets to the model.

## Gate

P2A-03: approved. Next eligible node: P2A-04. P2B remains blocked and default-off.
