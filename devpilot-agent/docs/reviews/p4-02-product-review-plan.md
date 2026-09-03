# P4-02 Product Review Plan

## User outcome and non-goals

The user can turn a server-saved failed `bug-check` Run into a minimal, reviewable
bug-fix ChangeSet, inspect its exact Diff and evidence binding, then use the existing
control-plane approval path for Apply and bounded verification. A successful or failed
delivery produces a server-owned Artifact without exposing credentials or raw command
output.

This node does not deliver a React control console, dependency installation, arbitrary
Shell, autonomous approval, real Sandbox execution, automatic rollback, or production
deployment. P6 remains the gate for real execution.

## Acceptance questions

1. Does the proposal require both server-generated repo-analysis evidence and a failed,
   project-bound `bug-check` Run?
2. Are symptom, expected behavior, target files, before hashes, allowed files, profile,
   patch hash, scope hash and stop conditions bound into the persisted contract?
3. Is every Patch limited to the saved failure target and explicit `allowed_files`, with
   stale or mismatched before hashes rejected before planning?
4. Does nanobot receive proposal-only access, while Apply/Verify remain separate
   control-plane capabilities requiring explicit user confirmation?
5. Do G-02 success, verification failure, out-of-scope and user-rejection paths produce
   truthful state and Artifact evidence?
6. Does adding the React static profile remain an allowlisted Fake-only contract and
   avoid launching npm, node, Shell or network work during P4-02?

## Evidence required

- Focused G-02 tests and full pytest, Ruff and Pyright results.
- API and nanobot adapter contract coverage, including registered Skill/tool parity.
- Persisted Run events, ChangeSet contract, before/after hashes and Artifact metadata.
- Read-only review of changed files and explicit confirmation that no user fixture,
  credential, database, local log or virtual environment is committed.

Unit tests alone do not prove real Sandbox isolation or production readiness; those stay
unanswered until the P6 evidence gate.

## Security, privacy and compatibility checks

- Allowed-root and trusted-project checks remain server-side and unchanged.
- No approval token is accepted from or returned to nanobot tools.
- No arbitrary command, network permission, secret file or raw stdout/stderr enters the
  P4-02 Artifact.
- Existing feature-delivery and P2B routes/tests remain backward compatible.
- Artifact filenames and verification profiles remain server allowlisted.

## Scope and blocker checks

Reject scope creep into UI, dependency management, real Sandbox execution, automatic
rollback, multi-project changes or unrelated refactors. External FC/E2B availability is
not a P4-02 acceptance dependency.

Severity: `blocker` means a safety, approval, path, secret, hash, persistence, backend
boundary or core-test failure; `must-fix-before-next-node` means a missing core contract
or required evidence; `follow-up` means documentation, formatting or non-release
observability work that may allow the next node but must be tracked; `observation` is a
non-blocking note. Decisions are `approve`, `approve-with-follow-ups`, `request-changes`,
or `blocked`.

## Review report template

```text
# P4-02 Product Review
Decision: approve | approve-with-follow-ups | request-changes | blocked

## Findings
- [severity] finding, evidence, and concrete recommendation

## Evidence inspected
- files, tests, commands, API/Run/Artifact facts

## Unanswered questions
- owner decision or P6 evidence still required

## Release recommendation
- what this decision unlocks and what remains prohibited
```
