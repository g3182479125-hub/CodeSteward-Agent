# P2A-01 acceptance evidence

Date: 2026-08-31

## Delivered boundary

This increment adds a controlled structured-patch service for trusted local
projects. It covers the core loop:

`FilePatch draft -> ChangeSet + unified diff -> test-only authorization -> atomic apply -> hash verification -> rollback`

The module intentionally does not add a production CLI, FastAPI write route,
or nanobot write Tool. A later phase must add the real approval/control-plane
adapter before any user-facing write operation is exposed.

## Safety checks

- Relative paths only; traversal, absolute paths, symlink escapes, generated
  directories, and secret-like files are rejected.
- Create/update/delete operations have explicit preconditions.
- File count, patch size, changed-line count, and file size are bounded.
- Apply requires a scoped, expiring, one-time `TestAuthorizationGrant`.
- Stale hashes produce zero writes.
- Partial apply attempts restore already-replaced files; rollback failure locks
  the project.
- Rollback detects user changes made after the apply and fails closed.

## Verification

Commands run from `E:\\codex-local\\devpilot-agent`:

```text
uv run pytest --basetemp E:\\codex-local\\devpilot-test-tmp
uv run pyright
uv run ruff check src scripts tests
uv run python scripts\\secret_scan.py
uv run python scripts\\check_markdown.py
```

Results:

- `41 passed, 1 skipped`
- Pyright: `0 errors, 0 warnings, 0 informations`
- Ruff: `All checks passed!`
- Secret scan passed
- Markdown link check passed

The single skipped test is the symlink-escape case when the Windows test
environment does not allow symlink creation; the path guard remains covered by
the implementation and the other path-rejection tests.
