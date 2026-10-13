<#
Stop and remove the "HeliumRecovery" Windows service (administrator PowerShell):

    powershell -ExecutionPolicy Bypass -File scripts\uninstall_service.ps1

Nothing else is deleted (logs, state, .env stay). Afterwards the monitor only
runs when started by hand, e.g. `uv run heliumrecovery desktop`.
#>
param([string]$Name = "HeliumRecovery")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Nssm = Join-Path $Root "tools\nssm.exe"
if (-not (Test-Path $Nssm)) { $Nssm = (Get-Command nssm.exe -ErrorAction Stop).Source }
if (-not (Get-Service -Name $Name -ErrorAction SilentlyContinue)) { Write-Host "No service '$Name'."; exit 0 }
& $Nssm stop $Name
& $Nssm remove $Name confirm
Write-Host "Service '$Name' removed."
