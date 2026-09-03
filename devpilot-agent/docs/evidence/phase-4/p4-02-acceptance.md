# P4-02 Bug-Fix Acceptance Evidence

Implementation commit: `7895e0d1c9bdd4d9a45eb6d4f23eb1f6f8f1fff8`
Plan: frozen DevPilot plan `2.2.0`

## Delivered outcome

P4-02 now binds a saved, server-produced `bug_check.failed` fact and read-only
repo evidence to a minimal BugFix ChangeSet. It exposes proposal-only control
plane and nanobot entry points, requires separate user approval for Apply and
Verify, persists a recoverable bug-fix contract, and writes success/failure
Artifacts with file and manifest hash evidence.

## G-02 coverage

- Successful Fake-only React static contract path;
- Non-zero verification and failure Artifact path;
- Backend-unavailable fail-closed path;
- Artifact index failure fallback event and failure Artifact;
- Out-of-scope Patch rejection;
- Explicit user-confirmation rejection;
- Conflicting before hashes, including Windows case-fold aliases;
- HTTP API, nanobot payload/error mapping, Skill metadata and command allowlist;
- Restart-time BugFix proposal contract recovery;
- Repository fixture is copied from `tests/fixtures/react-form-project` and is
  asserted to start in the defect state.

## Verification evidence

The following commands were run from the implementation commit above on
2026-09-03 (Asia/Shanghai), from `11:05:58+08:00` through `11:06:19+08:00`.

- `uv run ruff check src tests` — `All checks passed!`;
- `uv run pyright` — `0 errors, 0 warnings, 0 informations`;
- `uv run pytest` — `142 passed, 3 skipped in 13.70s`;
- `uv run alembic heads` — `0007_project_trust (head)`;
- `uv.lock` SHA-256 —
  `982424EFDA5CE0EB08DA9737EBCC9CD1FCFE91CF58D54E0FD30EA5B865E7767D`.

The reviewed working tree retained only the two user-owned Node fixture edits:
`tests/fixtures/node-project/src/index.js` and
`tests/fixtures/node-project/test/index.test.js`; they were not staged or
included in the implementation commit.

The server-side `BugCheckService` computes target-file SHA-256 values from the
allowed project before persisting `bug_check.failed`; storage rejects direct
event injection, untrusted failure-fact objects and canonical-path hash
collisions. The `react-static` Fake profile checks the G-02
`event.preventDefault()` invariant. Artifact DB or filesystem failures return a
stable artifact identity and explicit null path/hash when no file exists, and
bind profile, backend, exit code and before/after manifest hashes to the
failure event.

The command transcript is recorded in
`docs/evidence/phase-4/p4-02-verification.log`.

## Explicit boundary

`react-static` is a server allowlisted Fake contract for P4-02. No npm, React
test runner, arbitrary Shell, network access or real FC Sandbox execution is
claimed. Real execution remains gated by P6. The user's uncommitted Node fixture
changes are intentionally excluded from this implementation commit.
