# H1 Spec Review

- Axis: Spec
- Reviewer: Mendel (`01a08b9b-58a4-7e72-8fa5-c9c79523d497`)
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
| SP-H1-01 | H1 claimed two implementations, but the first candidate only had two manifests. | Added `AdditionTool` and a second manifest; the test loads and executes both and verifies their content hashes. |
| SP-H1-02 | `spawn_task` returned a bare task without an owned cleanup seam. | Added `PluginTaskHandle(task, dispose)` to the public `PluginContext` contract. Actual lifecycle execution remains H2/H3. |
| SP-H1-03 | Reserved names depended on an optional caller-provided set. | Added `PluginCatalog.from_registry()` to derive both live tool names and `tool:<name>` capability IDs. |
| SP-H1-04 | Model candidates could use in-process execution. | Model candidates now require `FC_PROXY`, with a dedicated error code and test. |

## Final result

PASS. No new or still-valid H1 spec defect was found. The fixed candidate covers
manifest/result/context contracts, the two independent examples, legacy
loader/Profile compatibility, reserved names, read-only list/status, session
isolation, separate candidate/runtime states and unchanged default loader
behaviour.

## Scope and limits

This candidate intentionally contains only the H1 contract/catalog layer. It
does not implement H2 manager commands, online load/stop, persistence, real
runtime routing or FC execution; those are later nodes and were not counted as
H1 failures. The plan and H1 evidence documents were pre-existing requested
planning deliverables included for continuity, not new runtime behaviour.
