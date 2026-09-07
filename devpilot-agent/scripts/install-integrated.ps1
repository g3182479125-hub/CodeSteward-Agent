param()

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\")).Path
$nanobotRoot = Join-Path $repoRoot "nanobot"
$devpilotRoot = Join-Path $repoRoot "devpilot-agent"
$python = Join-Path $nanobotRoot ".venv\Scripts\python.exe"
$localData = Join-Path $nanobotRoot "local-data"
$requirements = Join-Path $localData "devpilot-runtime-requirements.txt"

New-Item -ItemType Directory -Path $localData -Force | Out-Null
uv sync --project $nanobotRoot --locked --inexact --no-dev
uv export --project $devpilotRoot --locked --no-dev --no-emit-project `
  --format requirements.txt --output-file $requirements
uv pip install --python $python --requirement $requirements
uv pip install --python $python --no-deps --editable $devpilotRoot

Write-Host "Integrated nanobot + DevPilot runtime installed from both uv.lock files."
Write-Host "Verify entry points with the command in docs/runbooks/local-nanobot-devpilot.md."
