# P4-03 Test-Repair Acceptance Evidence

Implementation commit: `df4378a38ce27b69d907987febc882f2c431a6a9`  
Plan: frozen DevPilot plan `2.2.0`  
Date: 2026-09-03 (Asia/Shanghai)

## Delivered outcome

P4-03 adds a bounded `test-repair` workflow for saved Python and Node test
failures. It turns server-owned `test_check.failed` facts into an explicit,
proposal-only repair ChangeSet, requires a new approval for every Apply and
Verify, persists parent/child attempt contracts, and writes a redacted
success/failure Artifact.

The active nanobot Skill and Tool are:

- Skill: `skills/catalog/test-repair/SKILL.md`
- Tool: `devpilot_test_repair_propose`
- HTTP proposal route: `POST /v1/test-repair/proposals`
- Workflow module: `src/devpilot/test_repair.py`

## G-03 and safety coverage

- Python `python-run` and Node `node-syntax` saved-failure fixtures;
- failure facts bind project, target files, before SHA-256 values and text;
- every attempt is an independent ChangeSet with parent/root IDs;
- maximum three attempts; a fourth attempt is rejected before writing;
- repeated failure fingerprints stop the chain;
- user confirmation is required before Apply;
- non-Fake execution fails closed with `AUTONOMOUS_REPAIR_BLOCKED`;
- prompt-injected scope, unknown profiles and arbitrary command paths are
  rejected by deterministic workflow/policy code;
- Artifact persistence failures close the Run as failed and record a stable
  failure event;
- no real Python, Node, npm, Shell, network, dependency install or automatic
  repair loop is executed before P6.

## Verification evidence

The final local quality run collected 158 tests on an E: drive temporary
directory. Result: `155 passed, 3 skipped`.

- `uv --no-cache run pytest --tb=short -o addopts='' --basetemp E:/codex-local/pytest-temp/p4-03-final -p no:cacheprovider` — exit `0`;
- `uv --no-cache run ruff check --no-cache src scripts tests migrations` — exit `0`;
- `uv --no-cache run pyright` — exit `0`, `0 errors, 0 warnings, 0 informations`;
- `uv --no-cache run python scripts/secret_scan.py` — exit `0`;
- `uv --no-cache run python scripts/check_markdown.py` — exit `0`;
- `uv --no-cache lock --check` — exit `0`;
- `git diff --check` — exit `0`.

The focused P4-03 suite contains 8 passing tests. The P4-02 BugFix regression
suite contains 23 passing tests. The three skipped tests are the existing
Windows symbolic-link cases requiring `SeCreateSymbolicLinkPrivilege`.

## Explicit boundary

P4-03 is Fake-only. The FC/E2B environment does not authorize execution in
this phase, and this node does not claim a real Sandbox, automatic code repair,
automatic rollback, or external provider connectivity. Those remain gated by
P6 and subsequent work.

## Review note

The implementation received a local read-only two-axis review against the
P4-03 design and repository checks; no blocking finding remained. The current
session did not expose the independent subagent runner, so the review record
is explicitly marked local rather than falsely claiming an independent
reviewer.
