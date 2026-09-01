# Local development runbook

## 1. Start from a clean terminal

```powershell
cd D:\Users\tianmin.li\Documents\Codex\2026-08-10\new-chat\work\devpilot-agent
uv sync --dev
```

The command creates or updates the project virtual environment and lock file. It does not install nanobot into this repository.

## 2. Run the read-only doctor

```powershell
uv run python .\scripts\doctor.py --config .\config\inspect.example.json
```

The expected Windows result is a clear Sandbox unavailable/degraded message. This is correct; workspace restriction is not OS-level process isolation.

## FC Agent Sandbox (optional)

The FC backend is an explicit remote execution port, not a nanobot Tool. Install the
optional dependencies with the versions currently validated by Alibaba Cloud:

```powershell
uv sync --extra fc-sandbox
```

Configure the API key and region-specific E2B-compatible endpoint in the process
environment. Never place the values in JSON, source code, logs, prompts, or the WebUI:

```powershell
$env:E2B_API_KEY = "<API key from the FC console>"
$env:E2B_API_URL = "https://api.<region>.e2b.fc.aliyuncs.com"
$env:E2B_DOMAIN = "<region>.e2b.fc.aliyuncs.com"
$env:DEVPILOT_FC_TEMPLATE = "code-interpreter-v1"
```

Run the harmless connectivity/lifecycle check below. It creates a temporary Sandbox,
writes one non-secret fixture file, runs a fixed `python3` print command, and destroys
the Sandbox. It does not access any local project:

```powershell
uv run devpilot sandbox-check
```

The first verification call should use `FCSandboxBackend` with a tiny, explicit file bundle
and a bounded command. The adapter creates one short-lived Sandbox, writes only those files,
returns stdout/stderr/exit code, and always calls `kill()` in cleanup. It does not upload the
whole local repository or write results back to the local project. The current P2B control
plane does not invoke this backend automatically; a future verification Gate must add its own
approval capability and evidence before it can affect Apply decisions.

FC Agent Sandbox currently uses the E2B-compatible SDK path. Region, API URL, domain and
resource region must match, and the official compatibility list marks Snapshots, Volume,
Team management and Sandbox Logs as unavailable or limited for this integration.

## 3. Run the quality gates

```powershell
uv run ruff check src scripts tests
uv run pyright
uv run pytest
uv run python .\scripts\secret_scan.py
uv run python .\scripts\check_markdown.py
uv run alembic upgrade head
pnpm --dir web install
pnpm --dir web build
```

## 4. Publish the P1 Skill

Only a reviewed and enabled local catalog Skill can be synchronized to nanobot:

```powershell
uv run python .\scripts\publish_skill.py `
  --skill .\skills\catalog\repo-analysis\SKILL.md `
  --workspace D:\path\to\nanobot\local-state\workspace
```

Set `DEVPILOT_ALLOWED_ROOTS` before starting nanobot. The value is a path-separated
list of real project parent directories; an inspection outside those roots is rejected.

## 5. nanobot boundary

Use the independently prepared nanobot checkout for local smoke tests. Do not put its `local-state`, credentials, session history, or generated WebUI assets into this repository.
