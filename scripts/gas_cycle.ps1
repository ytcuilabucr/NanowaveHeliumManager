# Apps Script test cycle: offline JS tests -> clasp push -> redeploy -> live round-trip test.
# Usage:  powershell -File scripts/gas_cycle.ps1 [-SkipLive]
param([switch]$SkipLive)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:Path = "C:\Program Files\nodejs;$env:APPDATA\npm;$env:Path"

function Step($name, [scriptblock]$cmd) {
    Write-Host "== $name" -ForegroundColor Cyan
    & $cmd
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $name" -ForegroundColor Red; exit $LASTEXITCODE }
}

$dep = (Get-Content .env | Where-Object { $_ -match '^HR_GAS_DEPLOYMENT_ID=' }) -replace '^HR_GAS_DEPLOYMENT_ID=', ''
if (-not $dep) { Write-Host 'HR_GAS_DEPLOYMENT_ID missing in .env' -ForegroundColor Red; exit 1 }

Step 'offline Apps Script tests (node)' { node --test 'tests/gas/*.test.js' }
Step 'offline Python tests' { uv run --no-sync pytest -q -p no:cacheprovider }
Step 'clasp push' { clasp push --force }
Step "redeploy $dep" { clasp deploy --deploymentId $dep --description "gas_cycle $(Get-Date -Format s)" }
if (-not $SkipLive) {
    $env:HR_LIVE = '1'
    Step 'live round-trip test' { uv run --no-sync pytest -q -p no:cacheprovider tests/test_gas_live.py }
}
Write-Host 'gas cycle passed' -ForegroundColor Green
