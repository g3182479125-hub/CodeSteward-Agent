# Standards review: FC project-test fallback

- Fixed base: `e5d168e`
- Initial candidate: `d722a67`
- Incremental candidate: `bf70a22`
- Standards reviewer: `01a0ad1c-09cf-7153-a564-eae0c6d4be7e`
- Supplemental standards finding: `01a0ad25-0863-7111-b525-4e733eacd5c3`

## Initial review

The reused Standards reviewer returned **PASS** for the unsupported-fixture
guard. A supplemental independent check found one P1: the fallback scanned
only `tests/`, while the server-side Python test-entry contract also accepts
root-level `test_*.py` and `*_test.py` files. That could report a false pass
without executing a root-level test.

## Resolution and incremental review

`bf70a22` changes fallback discovery to scan the project root recursively,
while filtering to the same `test_*.py` / `*_test.py` filename contract. The
regression also proves that a root-level test runs and `conftest.py` is not
executed. The reused Standards reviewer returned **PASS**: no remaining P0–P3
finding, Fowler-smell blocker, safety regression, or scope expansion.

## Checks observed

```text
uv run pytest -q tests/test_agent_test_runs.py
uv run ruff check src/devpilot/test_runs.py tests/test_agent_test_runs.py
git diff --check
integrated pytest suite: 100% passed, 3 existing skips
```

