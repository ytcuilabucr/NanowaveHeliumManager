# Keep Helium Recovery running after Windows updates and restarts

Status (2026-10-12): scripts ready, `scripts\install_service.ps1` /
`scripts\uninstall_service.ps1`; `main.py` is now only a viewer. The service
is installed by running the script once as administrator (not done yet).

**Use the script** (administrator PowerShell, project folder):
`powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1 -Download`.
It does what the manual commands in section 1 describe, plus: delayed
auto-start (after the network), clean Ctrl+C stop before shutdown, log
rotation (10 MB), UTF-8 logging, Windows-level restart on failure, and a check
that nothing else (e.g. an old all-in-one window) already uses port 8090.

Goal: after any reboot (Windows Update, power cut, crash) the program starts
again by itself and keeps monitoring, without anyone logging in.

---

## 1. Run the monitor as a Windows service (recommended: NSSM)

A service starts at boot, **before anyone logs in**, and can be restarted
automatically if it crashes. [NSSM](https://nssm.cc) turns any program into a
service. In an **administrator** terminal:

```
nssm install HeliumRecovery "D:\PyPlayground\Nanowave_heliummanager\.venv\Scripts\heliumrecovery.exe" serve
nssm set HeliumRecovery AppDirectory "D:\PyPlayground\Nanowave_heliummanager"
nssm set HeliumRecovery Start SERVICE_AUTO_START
nssm set HeliumRecovery AppExit Default Restart
nssm set HeliumRecovery AppRestartDelay 10000
nssm set HeliumRecovery AppStdout "D:\PyPlayground\Nanowave_heliummanager\logs\service.log"
nssm set HeliumRecovery AppStderr "D:\PyPlayground\Nanowave_heliummanager\logs\service.log"
nssm start HeliumRecovery
```

- **`serve`, not `main.py`.** A service has no screen, so the pywebview desktop
  window cannot open there. `serve` does all the monitoring (pump, ATLs,
  Janis, Sheets, Slack, transfer logic) and serves the console on
  `HR_UI_PORT` (8090).
- **`AppDirectory` must be the project folder**: `.env` and `devices.json` are
  read from there.
- **Result:** starts after every reboot without a login; restarts by itself
  10 s after a crash.
- **Do not also run `main.py`** while the service runs: two copies would both
  poll the pump and fight over port 8090. `main.py` is now only a viewer of
  `http://localhost:8090` (no acquisition), so it is safe to open; the
  all-in-one `heliumrecovery desktop` refuses to start while the service runs.
- Useful commands: `nssm restart HeliumRecovery` (after a code update),
  `nssm stop HeliumRecovery`, `nssm edit HeliumRecovery` (GUI),
  `sc query HeliumRecovery` (status).

### Alternative without NSSM: Task Scheduler

Trigger **At startup**, "Run whether user is logged on or not", program
`...\.venv\Scripts\heliumrecovery.exe`, argument `serve`, *Start in* = the
project folder. Settings → "If the task fails, restart every 1 minute".
Restarts after a crash are less dependable than with NSSM.

---

## 2. Does a Windows login password matter?

| Situation | Effect |
|---|---|
| **Windows login password / PIN** (the sign-in screen) | **No problem for the service.** It starts at boot before the sign-in screen and runs without anyone logging in. |
| Service runs as the default **LocalSystem** account (NSSM default) | No password needed or stored. Reads the project folder, the LAN instruments and the internet (Sheets, Slack) fine. |
| Service set to run as **your user account** (`nssm set HeliumRecovery ObjectName .\<user> <password>`, or Task Scheduler "run whether logged on or not") | Windows stores that password. **If you later change the password, the service/task fails to start** until you update it there too. Only needed if something works only under your account; try LocalSystem first. |
| **BitLocker with a startup PIN**, or a **BIOS/UEFI power-on password** | **This does block it**: the PC stops at that prompt before Windows loads, so nothing runs until someone types it. BitLocker with TPM only (no PIN — the usual default) is fine. Check with `manage-bde -status C:` (look for "TPM And PIN"). |
| You want the **desktop window** back after a reboot (auto-login + Startup shortcut) | Auto-login needs the password stored (`netplwiz`, untick "Users must enter a user name and password"). With a Microsoft account set to PIN/Windows Hello-only sign-in, that checkbox is hidden until "For improved security, only allow Windows Hello sign-in" is turned off in Settings → Accounts → Sign-in options. Only do this if the PC is physically secure. Not needed for monitoring when the service is used. |

---

## 3. Make Windows restart less often (Windows 11 Pro)

1. **Active hours**: Settings → Windows Update → Advanced options → Active
   hours, set manually to the widest window (18 h).
2. **Group Policy** (`gpedit.msc`): Computer Configuration → Administrative
   Templates → Windows Components → Windows Update → Manage end user experience:
   - "No auto-restart with logged on users for scheduled automatic updates
     installations" → **Enabled**.
   - Optional: "Configure Automatic Updates" → **3 (download and notify for
     install)**, so you choose when to install and reboot.
3. **Pause updates** (up to 5 weeks) around critical runs.

## 4. Other settings that help

- **BIOS/UEFI**: "Restore on AC power loss" → **Power On**, so the PC comes
  back after a power cut.
- **Power settings**: Sleep → **Never**.
- **Know when it restarted**: the planned Slack `program_started` alert posts
  after every reboot or crash; the optional `heartbeat_missing` watchdog would
  report if it failed to come back up.
