# P2A-02 bounded code access acceptance

Date: 2026-09-01

## Delivered boundary

P2A-02 adds two R0 nanobot Tools for projects previously registered by
`devpilot_inspect_project`:

- `devpilot_search_code`: bounded literal source search with glob, result and file-scan limits.
- `devpilot_read_code`: bounded UTF-8 line reads with a full-file SHA-256 precondition hash.

Both operations resolve `project_id` through DevPilot storage and re-check the configured real
allowed roots. They do not execute project code or write into the project.

## Safety behavior

- Rejects traversal, absolute paths, NTFS streams, Windows reserved device names, trailing dots
  and trailing spaces.
- Skips generated/VCS directories, secret-like names, sensitive suffixes, binary/non-UTF-8 files
  and symbolic links.
- Redacts common API-key, bearer-token, password, secret and token values from returned text.
- Limits query length, glob length, files scanned, result count, file size, line count and returned
  bytes.
- The imported structured patch candidate now applies the same Windows alias defense and treats
  case-only duplicate paths as one target.

## Verification

Run from the repository root:

```text
python -m pytest -q -ra
python -m ruff check src scripts tests migrations
python -m pyright
python scripts/secret_scan.py
python scripts/check_markdown.py
cd web && pnpm build
```

Results on 2026-09-01:

- 64 tests collected; 62 passed and 2 skipped because this Windows account cannot create symlinks.
- Ruff passed.
- Pyright reported 0 errors, 0 warnings and 0 information messages.
- Secret scan and Markdown link checks passed.
- Frontend TypeScript/Vite production build passed.

## Gate decision

P2A-02 is accepted. The imported patch engine remains a candidate: P2A-03/P2A-04/P2A-05 and all
P2B nodes are not accepted by this evidence.
