<#
Stop and remove the "HeliumRecovery" Windows service (administrator PowerShell):

    powershell -ExecutionPolicy Bypass -File scripts\uninstall_service.ps1

Nothing else is deleted (logs, state, .env stay). Afterwards the monitor only
runs when started by hand, e.g. `uv run heliumrecovery desktop`.
#>
param([string]$Name = "HeliumRecovery")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run this in an administrator PowerShell (right-click PowerShell > Run as administrator)." }
$Nssm = Join-Path $Root "tools\nssm.exe"
if (-not (Test-Path $Nssm)) { $Nssm = (Get-Command nssm.exe -ErrorAction Stop).Source }
if (-not (Get-Service -Name $Name -ErrorAction SilentlyContinue)) { Write-Host "No service '$Name'."; exit 0 }
& $Nssm stop $Name
& $Nssm remove $Name confirm
# Windows may take a few seconds (longer while the Services window is open) to drop it
for ($i = 0; $i -lt 10 -and (Get-Service -Name $Name -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Seconds 1 }
if (Get-Service -Name $Name -ErrorAction SilentlyContinue) {
    throw "Service '$Name' is still installed - see the NSSM message above (close the Services window if it is open)."
}
Write-Host "Service '$Name' removed."
