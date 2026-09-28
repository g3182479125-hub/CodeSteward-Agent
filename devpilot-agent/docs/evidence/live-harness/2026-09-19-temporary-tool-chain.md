# Temporary Tool Chain — Live Harness Evidence

Date: 2026-09-19 (Asia/Shanghai)

## Real FC runs

The FC backend was invoked through `TemporaryToolValidator`; no Fake backend was
used for these two rows.

| Case | Run ID | Sandbox ID | Exit | Result |
|---|---|---|---:|---|
| Valid Tool import/schema contract | `fc-live-run` | `sbx-1b116e11-c226-4700-b3d8-a0fb361261a7` | 0 | `validated` |
| Missing Python dependency during import | `fc-invalid-run` | `sbx-03a98a1b-79b9-45c9-b8af-3e2f9653d247` | 1 | `rejected / TEMPORARY_TOOL_CONTRACT_FAILED` |

Final-checker live evidence after the fixture and receipt changes:

| Case | Execution Run ID | Sandbox ID | Exit | Result |
|---|---|---|---:|---|
| Final checker success/invalid/controlled-error fixtures | `run-1:temporary-tool-validation:0261416f802f:d2338863f1454e8ba60a382f7cb97b42` | `sbx-c45934ae-f095-47d1-8108-eecb25314408` | 0 | `validated`, `validation_version=temporary-tool-validation/v1` |

The final live stdout was:
`{"entry_class":"TemporaryLookupTool","fixtures":{"controlled_error":true,"invalid_params":true,"success":true},"ok":true}`.

The older captured real FC row predates the final fixture-output extension. The
final checker was subsequently run against the real FC provider above, and its
stdout includes `fixtures.controlled_error=true`,
`fixtures.invalid_params=true`, and `fixtures.success=true`.
The failed case produced `ModuleNotFoundError` for the deliberately missing
fixture dependency. It was rejected before `PluginManager` activation.

## Automated evidence

Commands were run from the repository's `nanobot/.venv` so the nanobot test
environment supplied `pytest-asyncio` and the installed editable DevPilot entry
points.

Commands and exit codes from the final targeted pass:

```powershell
cd E:\codex-local\nanobot-devpilot-agent\devpilot-agent
..\nanobot\.venv\Scripts\ruff.exe check src\devpilot\temporary_tools.py tests\test_temporary_tools.py
# exit 0: All checks passed
..\nanobot\.venv\Scripts\pytest.exe --basetemp .pytest-evidence-final tests\test_temporary_tools.py -q -rA
# exit 0: 28 passed, 1 skipped (live FC is opt-in in the default suite)

cd ..\nanobot
.\.venv\Scripts\ruff.exe check nanobot\agent\loop.py nanobot\agent\plugins\manager.py nanobot\agent\task_profiles.py
# exit 0: All checks passed
.\.venv\Scripts\pytest.exe --basetemp .pytest-review-final-live tests\agent\test_plugin_manager.py tests\agent\test_loop_tool_context.py tests\agent\test_loop_direct_websocket_status.py tests\agent\test_task_profiles.py tests\agent\test_task_profile_session.py nanobot\channels\websocket\tests\test_websocket_channel.py tests\channels\test_websocket_devpilot_confirmation.py ..\devpilot-agent\tests\test_temporary_tools.py -q
# exit 0: 263 passed in 18.79s

cd E:\codex-local\nanobot-devpilot-agent\nanobot\webui
npm test -- src/tests/agent-activity-cluster.test.tsx src/tests/generic-tool-model.test.ts src/tests/activity-message-model.test.ts src/tests/activity-copy.test.ts src/tests/markdown-text-renderer.test.tsx
# exit 0: 5 files passed, 131 tests passed
```

Earlier gates also passed: the DevPilot H6 selection `20 passed`, H5
candidate-runner selection `46 passed`, and the broader nanobot plugin/profile/
loop/WebSocket selection `268 passed`. The final pass additionally covers the
new privacy classification, gap record, FC receipt, cleanup-pending guard,
server-owned WebUI Run binding and unique FC validation Run IDs.

The temporary-tool chain test covers `generate → FC validate → hot-load → run
cleanup → project persistence`, including the persisted lifecycle sidecar and
invalid-history rejection. A subprocess-backed FC contract test executes the
candidate and records success, invalid-parameter, controlled-error and output
schema fixtures. A deterministic `FC_TIMEOUT` backend test also confirms
timeout rejection has no activation path. Sensitive capability
activation/persistence requires a gateway-signed, one-shot confirmation bound
to the exact session, Run, project, tool, version, source hash and precise
read/write/external capability action. Run cleanup advances the lifecycle
sidecar to `retiring`; activation failures remove the registered route and
record `rejected`.

The final incremental pass also covers session+project route isolation,
credential-shaped gap redaction, parent/runtime-computed path privacy gating,
the versioned validation receipt, and cleanup-pending retry without a
persistence prompt. Ruff passed for the changed DevPilot temporary-tool files
and the changed nanobot plugin/loop files. Entry-point discovery after editable installation
reported all six temporary-tool tools, including the high-level
`devpilot_ensure_temporary_tool` route.

The WebUI focused pass covers persisted/live activity stream rendering,
generic-tool semantic summaries and argument redaction, activity coalescing,
and Markdown rendering of the bold persistence confirmation. The full WebUI
Vitest suite was sampled but is not a release gate here because the dirty
workspace contains unrelated pre-existing locale/activity failures; the exact
focused command and exit code above are the evidence for this feature surface.

## Real WebUI evidence

The same in-app browser WebUI session completed the real missing-tool chain; no
mocked activity or fake FC response was used. The recording session was:

`http://127.0.0.1:8765/#/chat/websocket%3A50b8c120-213e-441f-9c36-ddd209274006`

At 18:59 (Asia/Shanghai), a fresh Run `webui-ce78dc4721bff35ea3e3d643f38430aa`
was used for a Python AST import-graph task against the repository. The model
first established that the existing tools were insufficient, then completed
the following chain in that same Run:

1. Created a fresh `importgraph` candidate with a server-owned WebUI Run
   binding.
2. Performed real FC validation in sandbox
   `sbx-07977bb3-3f76-4bfc-93b1-b50e5bf8a29c`.
3. Hot-loaded the candidate after internal FC revalidation. The successful
   activation used source hash
   `5396b08c4ca2cfe0f3c28e1566eb50727b49bf6c93492b5e7c508dd93240387d`,
   sandbox `sbx-f8ed9e03-1d84-4da1-9de6-7f63e6b8a6e5`, operation
   `c24a14e8-...`, state `active`, and `restart_required=false`.
4. Immediately called the newly hot-loaded `importgraph` tool. The real call
   persisted a 37,625-character result, scanned 5,276 Python files, and
   reported `scan_truncated=false`.

The recording exposed and the implementation fixed two real integration bugs:
the WebUI turn had no server-owned Run binding, and standalone FC validation
could collide with the activation revalidation because it reused a single FC
Run ID. Trusted WebUI turns now receive an opaque server-generated binding,
and every FC validation gets a unique backend Run ID. Full Access ordinary
workspace reads no longer trigger the generic sensitive-read prompt; privacy
and external-send capabilities remain confirmation-gated.

The model's final parsing of the large persisted result was stopped after the
successful tool call to avoid a redundant long-running summary step. This does
not invalidate the recorded create → FC validate → activate → real tool call
chain; the activity stream retains the actual return, sandbox IDs, source hash,
and persistence result.

The Run-completion persistence question is emitted by
`nanobot/nanobot/agent/loop.py` as the exact Markdown string
`**这个临时工具已完成本次任务，要保存为可复用插件吗？**`; the focused regression
`test_temporary_tool_persistence_prompt_is_marked_bold` verifies both bold
delimiters. The same completion path now suppresses that question when cleanup
returns `cleanup_pending`, so a failed cleanup cannot be presented as a tool
that is safe to save.

## Review status

An initial independent review found and blocked three gaps; commits `7296572`
and the current review-fix changes address bound confirmation, lifecycle
durability, cleanup transition, activation rollback, WebUI Run binding, and FC
Run-ID uniqueness. The latest incremental reviews found six additional blockers
and then four more; all are now covered by the final targeted tests and evidence
above. The same current-task subagent has now returned the required Standards
PASS and Spec PASS.

Final acceptance: current-task subagent
`01a0b95f-92cb-7201-8891-039a17d60b92` returned Standards Review PASS,
Spec Review PASS, and `Blocking issues: none`. The frozen plan is formally
accepted; unrelated dirty-worktree changes and pytest temporary directories
were explicitly deemed non-blocking.

## 2026-09-28 local WebUI recording recheck

The previous local recording attempt stopped at
`TEMPORARY_TOOL_DEFINITION_INVALID: TemporaryToolContractError` because the
model did not have the concrete `Tool`/`tool_parameters` source contract. The
implementation now includes the exact source template and schema-field
guidance in both the create and ensure tool descriptions. Controlled contract
errors expose their message without exposing arbitrary exception details.

Focused verification after the change:

```text
devpilot-agent/tests/test_temporary_tools.py: 39 passed, 1 skipped
devpilot-agent/tests/test_local_execution.py: 25 passed
ruff check: passed
git diff --check: passed
```

The live Personal Agent/WebUI recording was re-run after restarting the local
Harness/WebUI. In a fresh `recording-baseline` topic, the model first submitted
an invalid `text-uppercase` 1.0.0 candidate, received the visible schema
mismatch, and corrected it to 1.0.1. The corrected candidate then reached
`validated`, `execution_location=local`, `fixtures {success, invalid_params,
controlled_error} all true`, and `active` without a restart. Real calls
returned `HELLO DEVPILOT 123 — TEST` and `HELLO DEVFLOW`.

The update/removal segment also passed: version 1.0.2 added
`str(text).strip().upper()`, local validation and activation succeeded, padded
and whitespace-separated examples were transformed correctly, and
`devpilot_discard_temporary_tools` returned `state=disposed`, `removed=1`,
with `stop=ok` and `remove=ok`. A subsequent call was rejected with
`TOOL_VERSION_RETIRED`, proving the route could no longer execute. No project
files were read, no network was accessed, and no persistence was requested.
