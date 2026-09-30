$scriptPath = Join-Path $PSScriptRoot "..\scripts\start-local-recording-demo.ps1"

Describe "start-local-recording-demo" {
    It "creates a separate disposable ShopSphere baseline" {
        $scriptText = Get-Content -LiteralPath $scriptPath -Raw

        $scriptText | Should Match 'shopsphere-recording-demo'
        $scriptText | Should Match 'c001238cfbd54e90a01ee7a129bf7b199f084873'
        $scriptText | Should Match 'shopsphere-visible-bug.patch'
        $scriptText | Should Match '\[switch\]\$ResetDemo'
        $scriptText | Should Match '\.devpilot-recording-demo\.json'
        $scriptText | Should Match 'Refusing to reset an unverified recording directory'
    }

    It "publishes the local recording Skills and keeps FC disabled" {
        $scriptText = Get-Content -LiteralPath $scriptPath -Raw

        $scriptText | Should Match 'publish_skill.py'
        $scriptText | Should Match 'bug-fix'
        $scriptText | Should Match 'repo-analysis'
        $scriptText | Should Match 'feature-delivery'
        $scriptText | Should Match 'skills\\\$skillName\\SKILL\.md'
        $scriptText | Should Not Match 'EnableFCSandbox'
        $scriptText | Should Match 'DEVPILOT_ENABLE_FC_SANDBOX\s*=\s*"0"'
    }

    It "has a safe validation-only mode and starts both local pages" {
        $scriptText = Get-Content -LiteralPath $scriptPath -Raw

        $scriptText | Should Match '\[switch\]\$ValidateOnly'
        $scriptText | Should Match 'http://127.0.0.1:\$ShopPort'
        $scriptText | Should Match '\[int\]\$AgentPort = 8765'
        $scriptText | Should Match '\[int\]\$GatewayPort = 18790'
        $scriptText | Should Match '\[int\]\$ControlPlanePort = 18800'
        $scriptText | Should Match 'start-integrated-harness.ps1'
        $scriptText | Should Match 'http://127.0.0.1:\$AgentPort'
    }

    It "preflights a new computer and opens onboarding only when configuration is absent" {
        $scriptText = Get-Content -LiteralPath $scriptPath -Raw

        $scriptText | Should Match "Require-Command 'git'"
        $scriptText | Should Match "Require-Command 'uv'"
        $scriptText | Should Match 'cat-file -e'
        $scriptText | Should Match 'onboard.*--wizard'
        $scriptText | Should Match 'Local model configuration is missing'
    }
}
