$scriptPath = Join-Path $PSScriptRoot "..\scripts\start-integrated-harness.ps1"

Describe "start-integrated-harness" {
    It "accepts explicitly separated runtime paths inside the configured project root" {
        $allowedRoot = Join-Path $TestDrive "allowed-projects"
        $workspacePath = Join-Path $allowedRoot "workspace"
        $runtimeRoot = Join-Path $TestDrive "runtime"
        $configPath = Join-Path $runtimeRoot "config.json"
        $dataPath = Join-Path $runtimeRoot "devpilot-data"

        New-Item -ItemType Directory -Path $workspacePath -Force | Out-Null
        New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
        New-Item -ItemType File -Path $configPath -Force | Out-Null

        $failureMessage = $null
        try {
            & $scriptPath -ConfigPath $configPath -WorkspacePath $workspacePath `
                -DataPath $dataPath -AllowedRoots $allowedRoot -ValidateOnly
        }
        catch {
            $failureMessage = $_.Exception.Message
        }

        $failureMessage | Should BeNullOrEmpty
    }

    It "rejects a workspace that is outside the configured project roots" {
        $allowedRoot = Join-Path $TestDrive "allowed-projects"
        $workspacePath = Join-Path $TestDrive "outside-workspace"
        $runtimeRoot = Join-Path $TestDrive "runtime"
        $configPath = Join-Path $runtimeRoot "config.json"
        $dataPath = Join-Path $runtimeRoot "devpilot-data"

        New-Item -ItemType Directory -Path $allowedRoot, $workspacePath, $runtimeRoot -Force | Out-Null
        New-Item -ItemType File -Path $configPath -Force | Out-Null

        $failureMessage = $null
        try {
            & $scriptPath -ConfigPath $configPath -WorkspacePath $workspacePath `
                -DataPath $dataPath -AllowedRoots $allowedRoot -ValidateOnly
        }
        catch {
            $failureMessage = $_.Exception.Message
        }

        $failureMessage | Should Match "WorkspacePath must be contained by one of AllowedRoots"
    }
}
