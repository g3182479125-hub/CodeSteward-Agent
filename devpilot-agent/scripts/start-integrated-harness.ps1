param(
    [string]$StateRoot = "E:\codex-local\nanobot-state",
    [string]$AllowedRoots = "E:\codex-local",
    [switch]$SkipMigration
)

$ErrorActionPreference = "Stop"

function Resolve-EDrivePath([string]$PathValue, [string]$ParameterName) {
    $resolved = [System.IO.Path]::GetFullPath($PathValue)
    if (-not $resolved.StartsWith("E:\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$ParameterName must be located on E:\. Received: $resolved"
    }
    if ($resolved.Length -gt 3) {
        return $resolved.TrimEnd("\\")
    }
    return $resolved
}

$StateRoot = Resolve-EDrivePath $StateRoot "StateRoot"
$AllowedRoots = Resolve-EDrivePath $AllowedRoots "AllowedRoots"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$nanobotRoot = Join-Path $repoRoot "nanobot"
$devpilotRoot = Join-Path $repoRoot "devpilot-agent"
$nanobotExe = Join-Path $nanobotRoot ".venv\Scripts\nanobot.exe"
$devpilotExe = Join-Path $devpilotRoot ".venv\Scripts\devpilot.exe"
$alembicExe = Join-Path $devpilotRoot ".venv\Scripts\alembic.exe"
$configPath = Join-Path $StateRoot "config.json"
$workspacePath = Join-Path $StateRoot "workspace"
$dataPath = Join-Path $StateRoot "devpilot-data"
$databaseUrl = "sqlite:///" + (($dataPath -replace "\\", "/") + "/devpilot.db")

foreach ($requiredPath in @($nanobotExe, $devpilotExe, $alembicExe, $configPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Missing required local runtime path: $requiredPath"
    }
}

if (-not $env:DEVPILOT_HARNESS_SELECTION_INTEGRITY_KEY) {
    $env:DEVPILOT_HARNESS_SELECTION_INTEGRITY_KEY = [Environment]::GetEnvironmentVariable(
        "DEVPILOT_HARNESS_SELECTION_INTEGRITY_KEY", "User"
    )
}
if (-not $env:DEVPILOT_HARNESS_SELECTION_INTEGRITY_KEY) {
    throw "DEVPILOT_HARNESS_SELECTION_INTEGRITY_KEY is not configured for this Windows user."
}

$env:DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE = "1"
$env:DEVPILOT_ENABLE_FC_SANDBOX = "1"
$env:DEVPILOT_ENABLE_SKILL_IMPROVEMENT = "1"
$env:DEVPILOT_ENABLE_MODEL_CANDIDATES = "1"
$env:DEVPILOT_ENABLE_LOOP_HANDOFF = "1"
$env:DEVPILOT_ENABLE_HARNESS_SELECTION = "1"
$env:DEVPILOT_CONTROL_PLANE_URL = "http://127.0.0.1:18800"
$env:DEVPILOT_ALLOWED_ROOTS = $AllowedRoots
$env:DEVPILOT_DATA_DIR = $dataPath
$env:DEVPILOT_DATABASE_URL = $databaseUrl

if (-not $SkipMigration) {
    Push-Location $devpilotRoot
    try {
        & $alembicExe upgrade head
    }
    finally {
        Pop-Location
    }
}

$controlPlaneHealthy = $false
try {
    $controlPlaneHealthy = (Invoke-WebRequest -UseBasicParsing `
        "http://127.0.0.1:18800/healthz" -TimeoutSec 2).StatusCode -eq 200
}
catch {
    $controlPlaneHealthy = $false
}
if (-not $controlPlaneHealthy) {
    Start-Process -FilePath $devpilotExe `
        -ArgumentList "serve", "--host", "127.0.0.1", "--port", "18800" `
        -WorkingDirectory $devpilotRoot -WindowStyle Hidden
    Write-Host "DevPilot control plane started on 127.0.0.1:18800."
}
else {
    Write-Host "DevPilot control plane is already healthy."
}

$deadline = (Get-Date).AddSeconds(30)
do {
    try {
        $controlPlaneHealthy = (Invoke-WebRequest -UseBasicParsing `
            "http://127.0.0.1:18800/healthz" -TimeoutSec 2).StatusCode -eq 200
    }
    catch {
        $controlPlaneHealthy = $false
    }
    if (-not $controlPlaneHealthy) {
        Start-Sleep -Milliseconds 500
    }
} while (-not $controlPlaneHealthy -and (Get-Date) -lt $deadline)

if (-not $controlPlaneHealthy) {
    throw "DevPilot control plane did not become healthy within 30 seconds; inspect port 18800 before starting nanobot."
}

Write-Host "Starting nanobot WebUI with H6/H8/H9 live Harness features enabled."
& $nanobotExe webui --no-open --yes --config $configPath --workspace $workspacePath --port 8765 --gateway-port 18790
