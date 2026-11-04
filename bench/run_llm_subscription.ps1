# The LLM part of the HW7 benchmark (C2 and C3, Sonnet) on a Claude subscription, from Windows PowerShell.
# Run from the repo root, inside the activated virtual environment, after logging in to Claude Code:
#   .\bench\run_llm_subscription.ps1 -Smoke     one episode per arm, to check the setup (do this first)
#   .\bench\run_llm_subscription.ps1            the full run, about 270 episodes
# If the usage limit is reached the run stops; run the same command again later to continue.
param([switch]$Smoke)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:PYTHONUTF8 = "1"
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { throw "Claude Code not found. Install: irm https://claude.ai/install.ps1 | iex" }
claude auth status
if ($LASTEXITCODE -ne 0) { throw "Log in first: run 'claude' and use /login with your subscription account" }
if ($env:ANTHROPIC_API_KEY) { Write-Host "Note: ANTHROPIC_API_KEY is set; it is removed for these runs so your subscription is used." }

function Run([string[]]$extra) {
    python -m bench.run_bench --via claude-code @extra
    if ($LASTEXITCODE -eq 3) { Write-Host "Stopped at the usage limit. Run this script again later to continue."; exit 3 }
    if ($LASTEXITCODE -ne 0) { throw "run_bench failed ($LASTEXITCODE)" }
}

if ($Smoke) {
    Run @("--conditions", "C2_llm,C3_llm_attest", "--cases", "A9", "--reps", "1", "--out", "smoke-claude-code")
    Write-Host "`nSmoke run done. Model served:"
    Select-String -Path bench\results\smoke-claude-code\episodes.jsonl -Pattern '"model_served": "[^"]*"' -AllMatches |
        ForEach-Object { $_.Matches.Value } | Sort-Object -Unique
    exit 0
}

Run @("--conditions", "C2_llm,C3_llm_attest", "--reps", "5", "--out", "llm-main")
Run @("--suite", "sybil", "--conditions", "C3_llm_attest,C4_attest_only", "--reps", "3", "--cap", "0", "--out", "llm-sybil-cap0")
Run @("--suite", "sybil", "--conditions", "C3_llm_attest,C4_attest_only", "--reps", "3", "--cap", "1", "--out", "llm-sybil-cap1")
Write-Host "`nDone. Now: git add bench/results; git commit -m 'HW7 LLM benchmark results'; git push"
