<#
Install the Helium Recovery monitor as a Windows service ("HeliumRecovery").

Run in an ADMINISTRATOR PowerShell from the project folder:

    powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1
    powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1 -Download   # fetch NSSM 2.24 from nssm.cc first

What the service does: runs `python -m heliumrecovery serve` (pump, ATLs,
Janis, Sheets, Slack, transfer logic + the web console on HR_UI_PORT) from the
project folder, as LocalSystem, so it
  - starts at boot without anyone logging in (delayed start, after the network),
  - restarts 10 s after a crash,
  - is stopped cleanly (Ctrl+C) before Windows shuts down for an update.
main.py then only shows the console in a window.

Running it again updates the settings of an existing service.
#>
param(
    [string]$Name = "HeliumRecovery",
    [switch]$Download
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Logs = Join-Path $Root "logs"
$Log = Join-Path $Logs "service.log"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run this in an administrator PowerShell." }
if (-not (Test-Path $Python)) { throw "No virtual environment at $Python - run 'uv sync' in $Root first." }

# --- NSSM ------------------------------------------------------------------
$Nssm = Join-Path $Root "tools\nssm.exe"
if (-not (Test-Path $Nssm)) {
    $onPath = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($onPath) {
        $Nssm = $onPath.Source
    } elseif ($Download) {
        $zip = Join-Path $env:TEMP "nssm-2.24.zip"
        Write-Host "Downloading NSSM 2.24 from https://nssm.cc/release/nssm-2.24.zip"
        Invoke-WebRequest "https://nssm.cc/release/nssm-2.24.zip" -OutFile $zip -UseBasicParsing
        $dir = Join-Path $env:TEMP "nssm-2.24"
        Expand-Archive $zip -DestinationPath $env:TEMP -Force
        New-Item -ItemType Directory -Force (Split-Path $Nssm) | Out-Null
        Copy-Item (Join-Path $dir "win64\nssm.exe") $Nssm
    } else {
        throw "NSSM not found. Either run again with -Download, install it with 'winget install NSSM.NSSM', " +
              "or put nssm.exe in $(Split-Path $Nssm)."
    }
}
Write-Host "Using NSSM: $Nssm"

# --- port check: the all-in-one desktop app (old main.py) must be closed ----
$port = 8090
$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
    $m = Select-String -Path $envFile -Pattern '^\s*HR_UI_PORT\s*=\s*(\d+)' | Select-Object -First 1
    if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}
$existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listener -and -not ($existing -and $existing.Status -eq "Running")) {
    $procId = $listener[0].OwningProcess
    $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
    throw "Port $port is in use by $($p.ProcessName) (PID $procId). Close the running Helium Recovery " +
          "window/terminal first, so only the service talks to the pump."
}

# --- install / update --------------------------------------------------------
New-Item -ItemType Directory -Force $Logs | Out-Null
if (-not $existing) {
    & $Nssm install $Name $Python "-m heliumrecovery serve" | Out-Null
} else {
    & $Nssm stop $Name | Out-Null
    & $Nssm set $Name Application $Python | Out-Null
    & $Nssm set $Name AppParameters "-m heliumrecovery serve" | Out-Null
}
$settings = @(
    @("AppDirectory", $Root),
    @("DisplayName", "Helium Recovery monitor"),
    @("Description", "Pump/ATL/Janis acquisition, transfer control, Sheets logging and Slack alerts; console on port $port."),
    @("Start", "SERVICE_DELAYED_AUTO_START"),
    @("ObjectName", "LocalSystem"),
    @("AppExit", "Default", "Restart"),
    @("AppRestartDelay", "10000"),
    @("AppThrottle", "30000"),
    @("AppStopMethodConsole", "15000"),
    @("AppStdout", $Log),
    @("AppStderr", $Log),
    @("AppRotateFiles", "1"),
    @("AppRotateOnline", "1"),
    @("AppRotateBytes", "10485760"),
    @("AppEnvironmentExtra", "PYTHONUNBUFFERED=1", "PYTHONIOENCODING=utf-8", "HR_SERVICE=1")
)
foreach ($s in $settings) { & $Nssm set $Name @s | Out-Null }
# if NSSM itself ever dies, let Windows restart the service too
sc.exe failure $Name reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null

& $Nssm start $Name | Out-Null
Write-Host "Waiting for the console on port $port ..."
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        Write-Host "OK: service '$Name' is running; console at http://localhost:$port  (log: $Log)"
        Write-Host "Open it with: uv run python main.py"
        exit 0
    }
}
Write-Warning "The service did not open port $port within 60 s. Check $Log and 'sc query $Name'."
exit 1
