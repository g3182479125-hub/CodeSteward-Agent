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
