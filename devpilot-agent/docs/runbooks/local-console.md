# DevPilot Local Console Runbook

This is an optional DevPilot control console. It is separate from the nanobot
chat WebUI and does not replace the chat approval conversation.

## Start the API

From `E:\codex-local\nanobot-devpilot-agent\devpilot-agent`:

```powershell
$env:DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE = "1"
$env:DEVPILOT_ALLOWED_ROOTS = "E:\codex-local"
$env:DEVPILOT_DATA_DIR = "E:\codex-local\nanobot-state\devpilot-data"
uv run devpilot serve --host 127.0.0.1 --port 18800
```

The API is loopback-only. If FC Sandbox is configured, add the existing
`E2B_API_KEY`, `E2B_API_URL`, `E2B_DOMAIN`, and
`DEVPILOT_ENABLE_FC_SANDBOX=1` variables before starting it. Never put these
values in Git or browser storage.

## Start the console

In a second PowerShell window:

```powershell
cd E:\codex-local\nanobot-devpilot-agent\devpilot-agent\web
pnpm install --frozen-lockfile
pnpm run dev
```

Open `http://127.0.0.1:18801`. The console reads only `/api/v1` and
`/healthz`; it does not open SQLite or the project directory directly.

## Normal path

1. Register a project under Projects.
2. Ask nanobot to inspect/propose a change and copy the returned ChangeSet ID.
3. Review the ChangeSet/Diff view.
4. Use the Run Timeline to read the durable event snapshot or connect SSE.
5. Use Artifacts to inspect evidence metadata.
6. Keep Apply, Verify, Rollback, and final acceptance in the existing explicit
   confirmation chain. The console never stores capability tokens.

For local browser smoke and bookmarked diagnostics, the console also accepts
these optional query parameters: `page` selects a view; `change_set_id` loads
the ChangeSet and delivery report; `run_id` loads a Run or its Artifacts; and
`sse=1` connects the Run Timeline to the event stream. These parameters are
loopback-only convenience links and do not grant permissions or bypass the
nanobot confirmation chain.

## Troubleshooting

- `API offline`: confirm port `18800` is running and the control-plane feature
  flag is enabled.
- `PATH_DENIED`: the project is outside `DEVPILOT_ALLOWED_ROOTS`.
- `SSE unavailable`: click “读取快照”; the UI falls back to the database-backed
  Run projection.
- `EXECUTION_BACKEND_UNAVAILABLE`: the Security / Backends page reports the
  backend as unavailable; do not treat it as a successful verification.
