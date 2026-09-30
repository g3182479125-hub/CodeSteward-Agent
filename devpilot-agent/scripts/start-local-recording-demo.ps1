<# Prepares a disposable local ShopSphere fault demo and starts the local Harness. #>
[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $env:USERPROFILE '.nanobot\config.json'),
    [string]$WorkspacePath = (Join-Path $env:USERPROFILE '.nanobot\recording-workspace'),
    [string]$DataPath = (Join-Path $env:USERPROFILE '.nanobot\devpilot-data-recording'),
    [string]$RecordingRoot,
    [ValidateRange(1025, 65535)][int]$ShopPort = 5000,
    [ValidateRange(1025, 65535)][int]$AgentPort = 8765,
    [ValidateRange(1025, 65535)][int]$GatewayPort = 18790,
    [ValidateRange(1025, 65535)][int]$ControlPlanePort = 18800,
    [switch]$ResetDemo,
    [switch]$SkipInstall,
    [switch]$NoBrowser,
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
$baselineCommit = 'c001238cfbd54e90a01ee7a129bf7b199f084873'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$relativeProject = 'training-projects\shopsphere-bugfix-challenge'
$nanobotRoot = Join-Path $repoRoot 'nanobot'
$devpilotRoot = Join-Path $repoRoot 'devpilot-agent'
$nanobotPython = Join-Path $nanobotRoot '.venv\Scripts\python.exe'
$faultPatch = Join-Path $devpilotRoot 'fixtures\recording\shopsphere-visible-bug.patch'
$defaultRecordingRoot = Join-Path (Split-Path $repoRoot -Parent) 'shopsphere-recording-demo'
if (-not $RecordingRoot) { $RecordingRoot = $defaultRecordingRoot }
$RecordingRoot = [IO.Path]::GetFullPath($RecordingRoot)
$defaultRecordingRoot = [IO.Path]::GetFullPath($defaultRecordingRoot)
$projectPath = Join-Path $RecordingRoot $relativeProject
$demoMarkerPath = Join-Path $RecordingRoot '.devpilot-recording-demo.json'

function Require-File([string]$PathValue, [string]$Label) {
    if (-not (Test-Path -LiteralPath $PathValue -PathType Leaf)) { throw "$Label is missing: $PathValue" }
}
function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found. Install it, reopen PowerShell, then run this command again."
    }
}
function Assert-BaselineAvailable {
    & git -C $repoRoot cat-file -e "$baselineCommit`^{commit}"
    if ($LASTEXITCODE -ne 0) {
        throw 'The recording baseline is not available in this clone. Clone the repository without --depth, then run this command again.'
    }
}
function Write-DemoMarker {
    $marker = @{ baseline_commit = $baselineCommit; repository = $repoRoot } | ConvertTo-Json
    [IO.File]::WriteAllText($demoMarkerPath, $marker + [Environment]::NewLine, [Text.Encoding]::UTF8)
}
function Test-VerifiedDemoDirectory {
    if (-not (Test-Path -LiteralPath $demoMarkerPath -PathType Leaf)) { return $false }
    try {
        $marker = Get-Content -LiteralPath $demoMarkerPath -Raw | ConvertFrom-Json
        return $marker.baseline_commit -eq $baselineCommit -and
            [IO.Path]::GetFullPath([string]$marker.repository) -eq $repoRoot
    } catch { return $false }
}
function Test-DefaultLinkedWorktree {
    if ($RecordingRoot -ne $defaultRecordingRoot -or -not (Test-Path -LiteralPath $RecordingRoot)) { return $false }
    $linked = & git -C $repoRoot worktree list --porcelain
    return $linked -contains "worktree $RecordingRoot"
}
function Test-HttpOk([string]$Uri) {
    try { return (Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 2).StatusCode -eq 200 } catch { return $false }
}
function Wait-ForHttp([string]$Uri, [string]$Name) {
    $deadline = (Get-Date).AddSeconds(35)
    while ((Get-Date) -lt $deadline) { if (Test-HttpOk $Uri) { return }; Start-Sleep -Milliseconds 500 }
    throw "$Name did not become ready at $Uri within 35 seconds."
}
function Set-LocalSecret([string]$Name) {
    $value = [Environment]::GetEnvironmentVariable($Name, 'User')
    if (-not $value) {
        $bytes = New-Object byte[] 32; $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $value = [Convert]::ToBase64String($bytes)
        [Environment]::SetEnvironmentVariable($Name, $value, 'User')
        Write-Host "Created a local Windows-user secret for $Name."
    }
    Set-Item -Path "Env:$Name" -Value $value
}
function Quote-Argument([string]$Value) { '"' + $Value.Replace('"', '\"') + '"' }

Require-Command 'git'
Require-Command 'uv'
Require-File $faultPatch 'Recording fault patch'
Assert-BaselineAvailable

$requiredRuntime = @(
    $nanobotPython,
    (Join-Path $nanobotRoot '.venv\Scripts\nanobot.exe'),
    (Join-Path $nanobotRoot '.venv\Scripts\devpilot.exe'),
    (Join-Path $nanobotRoot '.venv\Scripts\alembic.exe')
)
if (($requiredRuntime | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) }).Count -gt 0) {
    if ($SkipInstall -or $ValidateOnly) { throw 'Integrated runtime is not installed. Run without -SkipInstall or -ValidateOnly once.' }
    Write-Host 'Installing the local integrated runtime (first run only)...'
    & (Join-Path $devpilotRoot 'scripts\install-integrated.ps1')
}

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    if ($ValidateOnly) { throw "Local model configuration is missing: $ConfigPath. Run without -ValidateOnly to complete the one-time setup." }
    Write-Host 'Local model configuration is missing. Starting one-time setup now...'
    New-Item -ItemType Directory -Path (Split-Path -Parent $ConfigPath) -Force | Out-Null
    & (Join-Path $nanobotRoot '.venv\Scripts\nanobot.exe') onboard --config $ConfigPath --workspace $WorkspacePath --wizard
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw 'Local model setup did not finish. Complete the wizard, then run this command again.'
    }
}
& $nanobotPython -c 'import sys; from pathlib import Path; from nanobot.config.loader import load_config; load_config(Path(sys.argv[1]))' $ConfigPath
if ($LASTEXITCODE -ne 0) { throw "Local model configuration is invalid: $ConfigPath" }
if ($ValidateOnly) {
    Write-Host "Validation passed. Recording project will be: $projectPath"
    Write-Host 'FC remains disabled; the demo uses only local execution.'
    return
}

if (-not (Test-Path -LiteralPath $RecordingRoot)) {
    Write-Host 'Creating the disposable ShopSphere recording worktree...'
    & git -C $repoRoot worktree add --detach --no-checkout $RecordingRoot $baselineCommit
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the recording worktree.' }
    & git -C $RecordingRoot sparse-checkout set $relativeProject.Replace('\', '/')
    if ($LASTEXITCODE -ne 0) { throw 'Could not configure sparse checkout.' }
    & git -C $RecordingRoot reset --hard $baselineCommit
    if ($LASTEXITCODE -ne 0) { throw 'Could not check out the ShopSphere recording baseline.' }
    Write-DemoMarker
} elseif (-not (Test-VerifiedDemoDirectory)) {
    if (Test-DefaultLinkedWorktree) {
        Write-Host 'Adopting the existing default recording worktree created by an earlier launcher...'
        Write-DemoMarker
    } else {
        throw "Recording directory is not managed by this launcher: $RecordingRoot"
    }
}
Require-File (Join-Path $projectPath 'run.py') 'ShopSphere recording project'
if ($ResetDemo) {
    if (-not (Test-VerifiedDemoDirectory)) { throw "Refusing to reset an unverified recording directory: $RecordingRoot" }
    Write-Host 'Resetting only the disposable recording project...'
    & git -C $RecordingRoot reset --hard $baselineCommit
    if ($LASTEXITCODE -ne 0) { throw 'Could not reset the recording project.' }
    $instancePath = Join-Path $projectPath 'instance'
    if (Test-Path -LiteralPath $instancePath) { Remove-Item -LiteralPath $instancePath -Recurse -Force }
}
$macroPath = Join-Path $projectPath 'shop\templates\_macros.html'
$macroText = Get-Content -LiteralPath $macroPath -Raw
$faultAlreadyApplied = $macroText -match 'product\.stock >= 0 %\}disabled' -and
    $macroText -match "'Out of stock' if product\.stock >= 0 else 'Add to cart'"
if (-not $faultAlreadyApplied) {
    & git -C $RecordingRoot apply --check $faultPatch
    if ($LASTEXITCODE -ne 0) { throw 'Unexpected recording project state. Re-run with -ResetDemo.' }
    & git -C $RecordingRoot apply $faultPatch
    if ($LASTEXITCODE -ne 0) { throw 'Could not inject the visible recording fault.' }
}

New-Item -ItemType Directory -Path $WorkspacePath, $DataPath -Force | Out-Null
foreach ($skillName in @('bug-fix', 'repo-analysis', 'feature-delivery')) {
    $skillPath = Join-Path $repoRoot "training-projects\python-project-template-bugfix\skills\$skillName\SKILL.md"
    & $nanobotPython (Join-Path $devpilotRoot 'scripts\publish_skill.py') --skill $skillPath --workspace $WorkspacePath
    if ($LASTEXITCODE -ne 0) { throw "Could not publish local Skill: $skillName" }
}
Set-LocalSecret 'DEVPILOT_HARNESS_SELECTION_INTEGRITY_KEY'
Set-LocalSecret 'DEVPILOT_CONTROL_PLANE_TOKEN'
$env:DEVPILOT_ENABLE_FC_SANDBOX = "0"
$env:DEVPILOT_ENABLE_MODEL_CANDIDATES = "0"
$env:DEVPILOT_ALLOWED_ROOTS = @($WorkspacePath, $repoRoot, $RecordingRoot) -join [IO.Path]::PathSeparator
$env:DEVPILOT_CONTROL_PLANE_URL = "http://127.0.0.1:$ControlPlanePort"

$shopUrl = "http://127.0.0.1:$ShopPort/c/books-media"
if (Test-HttpOk "http://127.0.0.1:$ShopPort/") { throw "Port $ShopPort already serves a site. Stop it or use -ShopPort." }
$projectPython = Join-Path $projectPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    & $nanobotPython -m venv (Join-Path $projectPath '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the ShopSphere virtual environment.' }
    & $projectPython -m pip install --disable-pip-version-check -r (Join-Path $projectPath 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Could not install ShopSphere requirements.' }
}
$logDir = Join-Path $DataPath 'logs'; New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$previousShopPort = $env:SHOP_PORT; $env:SHOP_PORT = "$ShopPort"
try {
    Start-Process -FilePath $projectPython -ArgumentList 'run.py' -WorkingDirectory $projectPath -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir "shopsphere-$ShopPort.out.log") `
        -RedirectStandardError (Join-Path $logDir "shopsphere-$ShopPort.err.log")
} finally { $env:SHOP_PORT = $previousShopPort }
Wait-ForHttp $shopUrl 'ShopSphere'

$harnessScript = Join-Path $devpilotRoot 'scripts\start-integrated-harness.ps1'
$harnessArgs = @('-NoExit', '-ExecutionPolicy', 'Bypass', '-File', (Quote-Argument $harnessScript), '-ConfigPath', (Quote-Argument $ConfigPath), '-WorkspacePath', (Quote-Argument $WorkspacePath), '-DataPath', (Quote-Argument $DataPath), '-AllowedRoots', (Quote-Argument $env:DEVPILOT_ALLOWED_ROOTS), '-WebUiPort', "$AgentPort", '-GatewayPort', "$GatewayPort", '-ControlPlanePort', "$ControlPlanePort")
if (-not $NoBrowser) { $harnessArgs += '-OpenBrowser' }
Start-Process -FilePath 'powershell.exe' -ArgumentList ($harnessArgs -join ' ') -WorkingDirectory $repoRoot
Wait-ForHttp "http://127.0.0.1:$ControlPlanePort/healthz" 'DevPilot control plane'
$headers = @{ 'X-DevPilot-Internal-Token' = $env:DEVPILOT_CONTROL_PLANE_TOKEN }
$registered = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$ControlPlanePort/v1/projects" -ContentType 'application/json' -Body (@{ project_path = $projectPath } | ConvertTo-Json)
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$ControlPlanePort/v1/projects/$($registered.project_id)/trust" -Headers $headers -ContentType 'application/json' -Body (@{ confirmation = 'trust' } | ConvertTo-Json) | Out-Null
Wait-ForHttp "http://127.0.0.1:$AgentPort/" 'Personal Agent WebUI'

Write-Host ''
Write-Host 'Recording demo is ready.'
Write-Host "Personal Agent: http://127.0.0.1:$AgentPort/"
Write-Host "ShopSphere fault page: $shopUrl"
Write-Host "Trusted demo project: $projectPath"
Write-Host 'Visible fault: in-stock catalogue products falsely show Out of stock.'
Write-Host 'Also present: concurrent cancellation can restore stock twice.'
Write-Host 'In the Agent, choose Full Access and /task-profile bug-fix before starting.'
