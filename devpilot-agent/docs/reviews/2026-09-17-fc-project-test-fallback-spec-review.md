# Spec review: FC project-test fallback

- Fixed base: `e5d168e`
- Initial candidate: `d722a67`
- Incremental candidate: `bf70a22`
- Spec reviewer: `01a0ad1c-0bd1-77b2-aecc-1d35e113fefc`

## Initial review

**REQUEST_CHANGES (P1).** The fallback correctly treated unsupported pytest
fixtures as `unknown` without creating `test_check.failed`, but its discovery
rule executed every Python file below `tests/`. That could run helpers,
`conftest.py`, or setup files and make the result unsafe evidence.

## Resolution and incremental review

`bf70a22` aligns fallback discovery with the server-owned Python test-entry
contract: root and nested `test_*.py` / `*_test.py` are discovered, while
helpers and `conftest.py` are skipped. Unsupported pytest imports and fixtures
remain `unknown`, with no `failure_run_id` and no `test_check.failed` fact.
The regression executes the generated runner against a root-level test plus a
failing `conftest.py` and confirms the test passes without running the helper.
The Spec reviewer returned **PASS** and found no remaining requirement gap.

