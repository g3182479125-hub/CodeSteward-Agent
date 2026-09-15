param(
    [string]$StateRoot,
    [string]$ConfigPath,
    [string]$WorkspacePath,
    [string]$DataPath,
    [string]$AllowedRoots = "E:\codex-local",
    [switch]$SkipMigration,
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"

function Resolve-AbsolutePath([string]$PathValue, [string]$ParameterName) {
    if (-not $PathValue) {
        throw "$ParameterName must be provided."
    }
    if (-not [System.IO.Path]::IsPathRooted($PathValue)) {
        throw "$ParameterName must be an absolute path. Received: $PathValue"
    }

    $resolved = [System.IO.Path]::GetFullPath($PathValue)
    $pathRoot = [System.IO.Path]::GetPathRoot($resolved)
    if ($resolved.TrimEnd("\\") -eq $pathRoot.TrimEnd("\\")) {
        throw "$ParameterName must not be a drive root. Received: $resolved"
    }
    return $resolved.TrimEnd("\\")
}

function Resolve-AllowedRoots([string]$PathList) {
    $separator = [regex]::Escape([System.IO.Path]::PathSeparator.ToString())
    $values = @($PathList -split $separator | Where-Object { $_ })
    if ($values.Count -eq 0) {
        throw "AllowedRoots must contain at least one absolute path."
    }
    return @($values | ForEach-Object { Resolve-AbsolutePath $_ "AllowedRoots" } | Select-Object -Unique)
}

function Test-PathWithinRoots([string]$CandidatePath, [string[]]$Roots) {
    foreach ($root in $Roots) {
        if ($CandidatePath.Equals($root, [System.StringComparison]::OrdinalIgnoreCase) -or
            $CandidatePath.StartsWith($root + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

$explicitRuntimePaths = @($ConfigPath, $WorkspacePath, $DataPath | Where-Object { $_ })
if ($explicitRuntimePaths.Count -gt 0) {
    if ($explicitRuntimePaths.Count -ne 3) {
        throw "ConfigPath, WorkspacePath, and DataPath must be provided together when overriding StateRoot."
    }
    $ConfigPath = Resolve-AbsolutePath $ConfigPath "ConfigPath"
    $WorkspacePath = Resolve-AbsolutePath $WorkspacePath "WorkspacePath"
    $DataPath = Resolve-AbsolutePath $DataPath "DataPath"
}
else {
    if (-not $StateRoot) {
        $StateRoot = "E:\codex-local\nanobot-state"
    }
    $StateRoot = Resolve-AbsolutePath $StateRoot "StateRoot"
    $ConfigPath = Join-Path $StateRoot "config.json"
    $WorkspacePath = Join-Path $StateRoot "workspace"
    $DataPath = Join-Path $StateRoot "devpilot-data"
}

$allowedRootList = Resolve-AllowedRoots $AllowedRoots
if (-not (Test-PathWithinRoots $WorkspacePath $allowedRootList)) {
    throw "WorkspacePath must be contained by one of AllowedRoots. Received: $WorkspacePath"
}

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Missing required config file: $ConfigPath"
}

if ($ValidateOnly) {
    Write-Host "Harness paths are valid. Workspace is constrained to the configured allowed roots."
    return
}

New-Item -ItemType Directory -Path $DataPath -Force | Out-Null

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$nanobotRoot = Join-Path $repoRoot "nanobot"
$devpilotRoot = Join-Path $repoRoot "devpilot-agent"
$nanobotExe = Join-Path $nanobotRoot ".venv\Scripts\nanobot.exe"
$devpilotExe = Join-Path $devpilotRoot ".venv\Scripts\devpilot.exe"
$alembicExe = Join-Path $devpilotRoot ".venv\Scripts\alembic.exe"
$databaseUrl = "sqlite:///" + (($DataPath -replace "\\", "/") + "/devpilot.db")

foreach ($requiredPath in @($nanobotExe, $devpilotExe, $alembicExe)) {
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
if (-not $env:DEVPILOT_CANDIDATE_GRANT_SECRET) {
    $env:DEVPILOT_CANDIDATE_GRANT_SECRET = [Environment]::GetEnvironmentVariable(
        "DEVPILOT_CANDIDATE_GRANT_SECRET", "User"
    )
}
if ([Text.Encoding]::UTF8.GetByteCount($env:DEVPILOT_CANDIDATE_GRANT_SECRET) -lt 32) {
    throw "DEVPILOT_CANDIDATE_GRANT_SECRET must contain at least 32 bytes for this Windows user."
}

$env:DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE = "1"
$env:DEVPILOT_ENABLE_FC_SANDBOX = "1"
$env:DEVPILOT_ENABLE_SKILL_IMPROVEMENT = "1"
$env:DEVPILOT_ENABLE_MODEL_CANDIDATES = "1"
$env:DEVPILOT_ENABLE_LOOP_HANDOFF = "1"
$env:DEVPILOT_ENABLE_HARNESS_SELECTION = "1"
$env:DEVPILOT_CONTROL_PLANE_URL = "http://127.0.0.1:18800"
$env:DEVPILOT_ALLOWED_ROOTS = ($allowedRootList -join [System.IO.Path]::PathSeparator)
$env:DEVPILOT_DATA_DIR = $DataPath
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
& $nanobotExe webui --no-open --yes --config $ConfigPath --workspace $WorkspacePath --port 8765 --gateway-port 18790
