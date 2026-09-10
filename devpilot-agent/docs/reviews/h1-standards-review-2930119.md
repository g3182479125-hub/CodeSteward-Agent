# H1 Standards Review

- Axis: Standards
- Reviewer: Hume (`01a08b9b-578b-7f00-997b-6ec1dc65bf0f`)
- Baseline: `2368a19d53672ba9ad0edfa2f20e9c4bd6b6990d`
- Initial candidate: `f4866afb75e25b4e9a1f6e5e334123d1fd59d102`
- Fixed candidate: `2930119f7191ec22c80d00bc27a05d54862fed5c`
- Full review: `git diff 2368a19...f4866af`
- Incremental review: `git diff cdb30ab...2930119`
- Note: the two candidates share `2368a19` as their parent, so the three-dot
  incremental diff has the same merge base as the cumulative diff. The direct
  two-point diff was also inspected.

## Initial findings and closure

| ID | Finding | Closure |
|---|---|---|
| ST-H1-01 | Illegal `PluginRecord` scope bindings used raw `ValueError`. | Fixed with `PluginContractError(SCOPE_MISMATCH)` and missing/double-binding tests. |
| ST-H1-02 | NUL/control characters in manifest entry could escape as a raw path error. | Fixed by rejecting control characters with `INVALID_ENTRY` and adding a test. |

## Final result

PASS. The reviewer found no new hard standards issue and no actionable Fowler
smell in the fixed candidate. The duplicate shape in the two example tools is
intentional H1 evidence, not accidental duplication. `PluginTaskHandle` and
`ToolNameSource` are required H1 seams, not speculative abstractions.

## Verification and limits

The main agent verified 23 H1/compatibility tests, Ruff, basedpyright, Markdown,
manifest hashes and `git diff --check`. A repository-outside regression subset
verified 102 passed and 1 skipped. The full run used an in-repository basetemp
and consequently caused existing nested-Git fixture failures; those were not
used as H1 evidence. An unrelated `context_prompt_cache` test remains a
pre-existing failure (25 passed, 1 failed) and is outside this diff.

H2+ lifecycle integration, real runtime loading, persistence and FC execution
were intentionally not reviewed as H1 requirements.
