# P4-02 Bug-Fix Acceptance Evidence

Implementation commit: `800dfb6`
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

## Explicit boundary

`react-static` is a server allowlisted Fake contract for P4-02. No npm, React
test runner, arbitrary Shell, network access or real FC Sandbox execution is
claimed. Real execution remains gated by P6. The user's uncommitted Node fixture
changes are intentionally excluded from this implementation commit.
