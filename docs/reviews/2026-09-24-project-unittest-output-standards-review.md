# 2026-09-24 project-unittest output fix — Standards review

## Review scope

- Fixed point: `81039c2`
- Candidate: `9c93123`
- Review axis: repository standards and Fowler-smell baseline
- Required references: `AGENTS.md`, `CURRENT_PLAN.md` sections 0/6/7,
  ADR-0004, the local-first execution design, and the ShopSphere challenge plan.

## Result

**PASS**

The fixed `python-unittest` command is consistent in all three server-owned
allowlists:

- `test_runs.py` selects `python -m unittest discover`;
- `local_execution.py` validates and executes the same profile;
- `execution.py` accepts only the same fixed command shape.

The execution backend still marks output over the configured limit as
`output_truncated`, and `ProjectTestRunner` still rejects a truncated receipt.
The new 500-case regression proves a large passing suite uses a complete,
non-verbose receipt; the existing regression still proves a truncated receipt
cannot become failure evidence.

Targeted tests, the full DevPilot pytest suite, Ruff, and `git diff --check`
passed. The only observation was non-blocking duplicated literals across the
three allowlists; it does not violate the current repository standards.

The review did not treat the absent ShopSphere challenge copy or live FC/WebUI
run as completed evidence; the handoff records those limits explicitly.
