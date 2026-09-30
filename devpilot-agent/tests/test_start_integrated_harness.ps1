$scriptPath = Join-Path $PSScriptRoot "..\scripts\start-integrated-harness.ps1"

Describe "start-integrated-harness" {
    It "allows an isolated local control-plane port" {
        $scriptText = Get-Content -LiteralPath $scriptPath -Raw

        $scriptText | Should Match '\[int\]\$ControlPlanePort = 18800'
        $scriptText | Should Match 'DEVPILOT_CONTROL_PLANE_URL = "http://127.0.0.1:\$ControlPlanePort"'
    }

    It "keeps FC disabled unless the operator explicitly opts in" {
        $scriptText = Get-Content -LiteralPath $scriptPath -Raw

        $scriptText | Should Match '\[switch\]\$EnableFCSandbox'
        $scriptText | Should Match '\$env:DEVPILOT_ENABLE_FC_SANDBOX = if \(\$EnableFCSandbox\)'
        $scriptText | Should Match '\$env:DEVPILOT_ENABLE_MODEL_CANDIDATES = if \(\$EnableFCSandbox\)'
        $scriptText | Should Not Match '\$env:DEVPILOT_ENABLE_FC_SANDBOX\s*=\s*"1"'
        $scriptText | Should Match 'Remove-Item Env:E2B_API_KEY'
        $scriptText | Should Match 'Remove-Item Env:E2B_API_URL'
        $scriptText | Should Match 'Remove-Item Env:E2B_DOMAIN'
    }

    It "defaults the FC test allowlist to the configured allowed roots" {
        $scriptText = Get-Content -LiteralPath $scriptPath -Raw

        $scriptText | Should Match 'DEVPILOT_FC_ALLOWED_PROJECTS'
        $scriptText | Should Match '\$allowedRootList -join \[System\.IO\.Path\]::PathSeparator'
    }

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
