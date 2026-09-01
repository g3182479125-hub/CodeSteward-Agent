# P2A-03 immutable structured patch acceptance

Date: 2026-09-01

## Delivered boundary

P2A-03 accepts a bounded tuple of immutable `FilePatch` values and produces an in-memory immutable
patch plan containing canonical project-relative paths, expected and resulting hashes, scope hash,
patch hash, before/after metadata and a unified Diff.

This node is planning-only acceptance. Although the imported module contains candidate apply and
rollback code, those operations are not registered as nanobot Tools and are not accepted here.

## Deterministic rejection rules

- Only create, update and delete are valid operations.
- Update/delete require the exact current SHA-256; create requires a missing target and no expected
  hash.
- Duplicate paths, case-only aliases and parent/descendant overlapping paths are rejected.
- Traversal, absolute/different-drive paths, Windows reserved devices, NTFS streams, trailing dots
  or spaces, secret/generated paths and symbolic links are rejected.
- ChangeSets are limited to 12 files, 80 KiB of replacement content, 1,200 changed lines and 512
  KiB per file; text must be UTF-8 and non-binary.
- R0 Tool path failures are normalized to the frozen public `PATH_DENIED` code.

## Verification

```text
python -m pytest -q -ra
python -m ruff check src scripts tests migrations
python -m pyright
python scripts/secret_scan.py
python scripts/check_markdown.py
cd web && pnpm build
```

Results:

- 68 tests collected; 65 passed and 3 skipped because this Windows account cannot create symbolic
  links.
- Ruff, Pyright, secret scan and Markdown link checks passed.
- Frontend TypeScript/Vite production build passed.

## Gate decision

P2A-03 is accepted. P2A-04 is next and must add durable manifests, crash recovery, project locking
and rollback evidence before any write operation is exposed.
