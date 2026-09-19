# Temporary Tool Chain — Live Harness Evidence

Date: 2026-09-19 (Asia/Shanghai)

## Real FC runs

The FC backend was invoked through `TemporaryToolValidator`; no Fake backend was
used for these two rows.

| Case | Run ID | Sandbox ID | Exit | Result |
|---|---|---|---:|---|
| Valid Tool import/schema contract | `fc-live-run` | `sbx-1b116e11-c226-4700-b3d8-a0fb361261a7` | 0 | `validated` |
| Missing Python dependency during import | `fc-invalid-run` | `sbx-03a98a1b-79b9-45c9-b8af-3e2f9653d247` | 1 | `rejected / TEMPORARY_TOOL_CONTRACT_FAILED` |

The successful FC stdout was `{"ok": true, "entry_class": "TemporaryLookupTool"}`.
The failed case produced `ModuleNotFoundError` for the deliberately missing
fixture dependency. It was rejected before `PluginManager` activation.

## Automated evidence

Commands were run from the repository's `nanobot/.venv` so the nanobot test
environment supplied `pytest-asyncio` and the installed editable DevPilot entry
points.

```text
devpilot-agent/tests/test_temporary_tools.py
17 passed

devpilot-agent/tests/test_h6_model_plugins.py
20 passed

devpilot-agent/tests/test_h5_candidate_runner.py
46 passed

nanobot plugin/profile/loop/WebSocket regression selection
267 passed
```

The temporary-tool chain test covers `generate → FC validate → hot-load → run
cleanup → project persistence`. A deterministic `FC_TIMEOUT` backend test also
confirms timeout rejection has no activation path.

Ruff passed for the changed DevPilot temporary-tool files and the changed
nanobot plugin/loop files. Entry-point discovery after editable installation
reported all six temporary-tool tools, including the high-level
`devpilot_ensure_temporary_tool` route.

## Known boundary

The real WebUI visual recording is a separate manual evidence step. The server
side WebSocket regression suite passed, including Full Access scope and
confirmation-envelope cases. A final independent Standards + Spec review is
still required before this plan is marked formally accepted.
