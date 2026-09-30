$scriptPath = Join-Path $PSScriptRoot "..\scripts\start-local-recording-demo.ps1"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$baselineCommit = 'c001238cfbd54e90a01ee7a129bf7b199f084873'
$faultPatch = Join-Path $repoRoot 'devpilot-agent\fixtures\recording\shopsphere-visible-bug.patch'

Describe "start-local-recording-demo" {
    It "creates a separate disposable ShopSphere baseline" {
        $scriptText = Get-Content -LiteralPath $scriptPath -Raw

        $scriptText | Should Match 'shopsphere-recording-demo'
        $scriptText | Should Match 'c001238cfbd54e90a01ee7a129bf7b199f084873'
        $scriptText | Should Match 'shopsphere-visible-bug.patch'
        $scriptText | Should Match '\[switch\]\$ResetDemo'
        $scriptText | Should Match '\.devpilot-recording-demo\.json'
        $scriptText | Should Match 'Refusing to reset an unverified recording directory'
        $scriptText | Should Match 'git -C \$RecordingRoot apply --check \$faultPatch'
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
        $scriptText | Should Match 'AddSeconds\(90\)'
    }

    It "preflights a new computer and opens onboarding only when configuration is absent" {
        $scriptText = Get-Content -LiteralPath $scriptPath -Raw

        $scriptText | Should Match "Require-Command 'git'"
        $scriptText | Should Match "Require-Command 'uv'"
        $scriptText | Should Match 'cat-file -e'
        $scriptText | Should Match 'onboard.*--wizard'
        $scriptText | Should Match 'Local model configuration is missing'
        $scriptText | Should Match 'Join-Path \$nanobotRoot ''\.venv\\Scripts\\devpilot\.exe'''
    }

    It "applies and reverses the visible fault against the committed recording baseline" {
        $worktreePath = Join-Path ([IO.Path]::GetTempPath()) ("devpilot-recording-fixture-" + [guid]::NewGuid())
        try {
            & git -C $repoRoot worktree add --detach $worktreePath $baselineCommit
            $LASTEXITCODE | Should Be 0

            & git -C $worktreePath apply --check $faultPatch
            $LASTEXITCODE | Should Be 0
            & git -C $worktreePath apply $faultPatch
            $LASTEXITCODE | Should Be 0
            & git -C $worktreePath apply --reverse --check $faultPatch
            $LASTEXITCODE | Should Be 0
        } finally {
            if (Test-Path -LiteralPath $worktreePath) {
                & git -C $repoRoot worktree remove --force $worktreePath
            }
        }
    }

    It "keeps recording patches LF-only when Windows checks out the repository" {
        $attributes = Get-Content -LiteralPath (Join-Path $repoRoot '.gitattributes') -Raw

        $attributes | Should Match 'devpilot-agent/fixtures/\*\*/\*\.patch text eol=lf'
    }
}
