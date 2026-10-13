# Helium Recovery

Monitoring and control console for the lab's helium recovery system. It reads
the MP Hub recovery pump, the three ATL liquefiers, the Janis LHe level meter,
the CMI / Wei-He3 cryostat levels (from Google Sheets) and the ATL3 inlet valve;
shows them in a web console; runs the helium transfer procedure; logs to Google
Sheets; and sends alerts and a nightly report to Slack.

It runs as the Windows service **HeliumRecovery** on the lab PC, so it keeps
monitoring with nobody logged in and starts again by itself after a reboot.

- Console: `http://<lab PC>:8090` in any browser on the lab network, or
  `uv run python main.py` on the lab PC (a window that only *shows* the console;
  closing it stops nothing).
- Moving to another computer: [docs/moving_to_new_pc.md](docs/moving_to_new_pc.md).

---

## User manual

### Header: operation mode

The pill next to the clock (top right) shows the system's operation mode. Click
it to change it.

| Mode | Meaning |
|---|---|
| **Observer** (grey, default) | The program reads and shows everything and changes nothing by itself. Operators still use the write buttons. |
| **Transfer** (yellow) | A helium transfer is in progress. Starts automatically when Transfer **Step 1** is opened and ends when **Step 3** restores normal pump settings. "ATL not responding" alerts are suppressed meanwhile. |
| **Agent** (blue) | The program may change settings by itself. Automatic actions are not implemented yet. Always returns to Observer when the program restarts. |

### Tabs

| Tab | What it is for |
|---|---|
| **Table View** | All live readings: pump pressures, the three ATLs, Janis / CMI / Wei-He3 levels. ATL **Liquefaction mode** buttons and the ATL3 inlet valve Open / Close. |
| **Graphic View** | Dewars, pressure gauges and cryostat levels at a glance. A **yellow** border/label = warning, **red** = critical (same rules as the Slack alerts); a dashed label = the condition is still being confirmed. |
| **Diagram View** | The recovery and liquefaction circuit with live values. |
| **Transfer Control** | Pump settings editor (SET) and the **Step 1 / Step 2 / Step 3** helium transfer buttons, with inlet / tank pressure charts. |
| **BP Valve Tuning** | Back-pressure valve settings and register diagnostics. |
| **Status** | Connection state of every instrument and the error list. |
| **Internal** (lock icon, maintenance only) | Reading intervals, a "read everything now" refresh, the Message communication system, transfer simulation, and **Restart service**. |

Greyed-out cards marked **inactive** belong to instruments switched off under
Internal → Message communication system → Activation status.

### Helium transfer

1. **Step 1**: set the valves as pictured (close the dewar valve, open the
   cryostat and pump bypass valves) → choose the transfer preset → **Proceed**.
   The pump is disabled with the transfer settings and switches itself back on
   when the inlet pressure jumps. The system enters **Transfer** mode.
2. **Step 2** (at the end of the transfer): set the countdown → **Proceed**. When
   the pump stops, **open the cryostat pressure relief valve** right away.
3. **Step 3**: restore normal settings (preset 0) → set the valves back as
   pictured → reconnect the ATL → fill in the **transfer log** (pre-filled where
   possible; every field but Notes is required) and **Log transfer**, or
   **Skip**. The system returns to its previous mode.

The transfer log goes to the "Helium Recovery System Operation" spreadsheet
(tab "Helium Transfer") and is also kept locally in `state/transfer_log.csv`.

To practise, use Internal → **Transfer simulation**: the same dialogs, nothing
is written to the pump.

### Changing an ATL mode

Table View → the ATL card → **Mode selection** → choose the mode → confirm. The
mode is sent to the unit and read back. After **Stop**, the compressor needs
**60 s** to cool down: the other modes stay disabled (with a countdown) until then.

### Alerts and Slack

Alerts go to the Slack channel set in `.env`:

- **Critical** (red, managers @mentioned): outlet pressure ≥ 7.5 bar or ≤ 2.0 bar,
  inlet / tank over-pressure, pump left disabled, pump not responding,
  Janis / CMI very low.
- **Warning**: outlet ≤ 2.3 bar, oil temperature high, ATL stopped,
  ATL not responding, Janis / CMI low, Step 1 left waiting.
- Each alert is sent once, with reminders after 1 h, 3 h, then every 6 h, and a
  "resolved" message when it clears.
- **10 PM report**: current values and an overnight forecast to 08:00.

Internal → **Message communication system**:

- **Activation status**: switch an instrument off while it is under engineering.
  It then sends no messages and is greyed out everywhere (its controls are
  disabled). Currently off: Janis, ATL2, Wei-He3.
- **Alerts**: Mute 1 h / Mute until 08:00 / Unmute, Preview report, Send report
  now, and the active alerts and recent messages.

### Safety switches

Writes to the hardware are refused unless enabled in `.env`:
`HR_PUMP_ALLOW_WRITES=1` (pump settings, transfer steps), `HR_ATL_ALLOW_WRITES=1`
(ATL modes), `HR_VALVE_ALLOW_WRITES=1` (ATL3 inlet valve). Every write asks for
confirmation, is validated, and is read back from the device.

### When something looks wrong

- **Page does not load**: the service may be restarting (about 15 s), or stopped:
  check Windows Services → HeliumRecovery.
- **An instrument shows "error"**: Status tab for the message; Internal →
  **Refresh** reads everything once.
- **After changing `.env` or updating the code**: Internal → **Restart service**.
- Log file: `logs\service.log`.

---

## Installation (administrator, once per computer)

Requirements: Windows 10/11, [Git](https://git-scm.com/download/win),
[uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`), and the lab
network (the instrument addresses go into `.env`).

```powershell
git clone <repository URL> C:\HeliumRecovery
cd C:\HeliumRecovery
uv sync                                   # installs Python 3.13 and the packages
copy .env.example .env                    # then fill in (see Configuration)
# copy devices.json (ATL3 inlet valve keys) into this folder
powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1 -Download
```

The install script downloads NSSM into `tools\`, registers the service (delayed
auto-start, restart on failure, log rotation) and starts it. Remove it with
`scripts\uninstall_service.ps1`.

Other ways to run (only when the service is **not** running):
`uv run heliumrecovery desktop` (console + acquisition in one window),
`uv run heliumrecovery serve` (browser), `uv run heliumrecovery run` (text only).

## Configuration (`.env`)

`.env` is never committed. Main entries (full list with defaults in `.env.example`):

| Setting | Meaning |
|---|---|
| `HR_UI_PORT` | Console port (8090) |
| `HR_PUMP_HOST`, `HR_ATL_HOSTS`, `HR_JANIS_HOST` | Instrument IP addresses (required) |
| `HR_ATL_USER`, `HR_ATL_PASSWORD`, `JANIS_USERNAME`, `JANIS_PASSWORD` | Instrument logins |
| `HR_*_ALLOW_WRITES` | Safety switches (above) |
| `HR_STATUS_SHEET_URL`, `HR_STATUS_SHEET_TOKEN` | Google Apps Script web app for the status / log sheets |
| `HR_GAS_DEPLOYMENT_ID` | That web app's deployment id (for `scripts\gas_cycle.ps1`) |
| `HR_CRYO_SHEET_ID`, `HR_CMI_GID`, `HR_WEI_GID` | Spreadsheet and tab ids with the CMI / Wei-He3 levels (reading is off when unset) |
| `SLACK_BOT_TOKEN`, `SLACK_CHANNEL`, `HR_SLACK_MANAGERS` | Slack bot, channel ID, member IDs to @mention |
| `HR_ALERT_ARCHIVED` | Instruments switched off at first start (afterwards the Internal tab toggles are kept in `state/modes.json`) |
| `HR_REPORT_TIME`, `HR_REPORT_HORIZON` | Nightly report time and forecast horizon |

## Google Sheets

The Apps Script in `apps_script/HeliumStatus.gs` is bound to the **status**
spreadsheet and deployed as a web app. Its per-site settings are script
properties (Project Settings → Script properties): `LOG_SHEET_ID` (the log
spreadsheet), and optionally `ARCHIVE_FOLDER_ID` / `ARCHIVE_NAME` (monthly archive
folder and file name, default `LogArchives/HeliumRecoveryStatusLog_YYYYMM` next to the
log) and `READ_SOURCES` (other spreadsheets to read CMI / Wei-He3 levels from, when
they are not tabs of the status spreadsheet itself). The service posts to it:

| File | Content |
|---|---|
| status spreadsheet | One tab per instrument, latest values (every 30 s) |
| log spreadsheet, tab `Log` (+ `Charts`) | One row every 3 min; archived monthly to `LogArchives/HeliumRecoveryStatusLog_YYYYMM` |
| Helium Recovery System Operation, tab `Helium Transfer` | Transfer log (created next to the log spreadsheet) |

The same web app reads the CMI / Wei-He3 level spreadsheet. Charts and tabs can
be published to the web and embedded in Notion.

## Maintenance

```powershell
uv run pytest                                            # offline tests, no instrument access
powershell -File scripts\gas_cycle.ps1 [-SkipLive]       # Apps Script: tests, push, redeploy, live check
```

`scripts\gas_cycle.ps1` needs Node.js and `clasp` (`npm i -g @google/clasp`,
then `clasp login`) and a `.clasp.json` pointing at the script (see the moving guide).

Kept in `state/` (not committed): alert state, 24 h trend, operation mode and
activation toggles, local transfer log.

## Project layout

```
main.py                  desktop viewer of the running service
src/heliumrecovery/
  drivers/               pump (PCOM over TCP), ATL (HTTP XML), Janis, valve (Tuya), sheet cryostats
  core/                  data engine, workers, transfer procedure, modes, history/trend
  services/              Google Sheets, Slack alerts + report, transfer log, Notion
  ui/                    NiceGUI console (app.py), transfer dialogs, diagram, theme
apps_script/             Google Apps Script for the status / log / transfer sheets
scripts/                 service install / uninstall, Apps Script test cycle
docs/                    moving guide, alert rules, auto-start notes
tests/                   offline tests (+ tests/gas for the Apps Script)
```
