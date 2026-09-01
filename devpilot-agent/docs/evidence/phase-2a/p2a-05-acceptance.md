# P2A-05 fixture and safety acceptance

Date: 2026-09-01

## Delivered boundary

Python and Node fixture projects now exercise the complete P2A test-authorized path:

`registered/read -> structured FilePatch -> deterministic Diff -> test grant apply -> hash receipt -> rollback`

The fixture tests never execute fixture code, install dependencies, invoke Shell, or claim a
Sandbox. They verify file bytes and hashes only.

## Frozen safety coverage

- S-01: traversal, absolute/different-drive paths and symbolic-link boundaries reject before write.
- S-02: `.env`, sensitive suffixes, generated directories, binary files and secret-like values
  are rejected or redacted.
- S-03: stale preconditions, duplicate/overlapping paths, partial replacement failure and
  rollback conflicts do not silently overwrite files.
- S-10: persisted `applying` state on service restart durably locks the project.

## Verification

The P2A suite, including both fixtures, passes. The three symlink cases remain skipped on this
Windows account because the OS denies symlink creation; the cases are required to run in CI with
the appropriate privilege. No project code was executed.

P2A is now complete for its explicitly test-authorized apply boundary. Production write access
remains closed until P2B approval and P3 execution gates are complete.
