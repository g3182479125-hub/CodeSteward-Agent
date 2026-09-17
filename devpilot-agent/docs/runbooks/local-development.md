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
# Python project tests temporarily install pytest in each new Sandbox.
$env:DEVPILOT_FC_INSTALL_PYTEST = "1"
```

The strict default sends a network-deny policy. Alibaba FC endpoints may reject that
optional E2B capability. If you deliberately accept the provider's default network
policy for local development, set the following before starting the control plane:

```powershell
$env:DEVPILOT_FC_NETWORK_POLICY_MODE = "provider-default"
```

This mode is reported as degraded and does not claim network isolation; do not use it
for untrusted code or sensitive snapshots.

When `DEVPILOT_FC_INSTALL_PYTEST` is `1` (the default), only the server-owned
`project-pytest` profile runs `python -m pip install --disable-pip-version-check pytest`
before the test command. The install is temporary and the Sandbox is still destroyed
after the run. Set it to `0` only when using a template that already contains pytest.
The install requires the provider to permit outbound package access; in strict mode,
set `$env:DEVPILOT_FC_ALLOW_NETWORK = "1"` explicitly.

Run the harmless connectivity/lifecycle check below. It creates a temporary Sandbox,
writes one non-secret fixture file, runs a fixed `python3` print command, and destroys
the Sandbox. It does not access any local project:

```powershell
uv run devpilot sandbox-check
```

The verification flow now exposes three server-controlled profiles after a ChangeSet has been
applied:

- `python-syntax`: compile the changed Python files without running them.
- `python-run`: run exactly one changed Python file, allowing that file to contain a small
  self-test or test runner. This is the useful browser-level smoke test for the demo.
- `node-syntax`: syntax-check changed JavaScript files.

Each request uses a complete but bounded UTF-8 project snapshot and a server-controlled command.
The adapter excludes dependency/build/cache directories and secret-like files, creates one
short-lived Sandbox, returns bounded stdout/stderr/exit code, and always calls `kill()` in cleanup.
In strict mode the request includes the deny-network policy; in `provider-default` mode the FC
endpoint receives no network-policy parameter, so provider network behavior is unknown and
isolation is not proven. Remote verification also requires an explicit
`DEVPILOT_FC_ALLOWED_PROJECTS` path allowlist. It does not mount the local project or write
results back to the local project. In the nanobot UI, the user confirms “执行” in chat; DevPilot
internally issues the verification grant and no separate approval page is needed.

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
