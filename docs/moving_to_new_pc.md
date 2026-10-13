# Moving Helium Recovery to another computer (and to the shared Google folder)

Two moves happen together:

1. **Program**: from the current PC to the new lab PC, through git.
2. **Google files**: from the personal test files (`status_now`, `status_log`,
   the test "Helium Recovery System Operation") to new files in the **shared
   Helium Recovery drive folder**, owned by the lab's shared Google account.

Only one computer may run the service at a time (two would both poll the pump
and both send every Slack alert), so the old one is stopped right before the
new one starts (Part E).

---

## Part A — on the current PC: put the code in git

The project is its own git repository (`Nanowave_heliummanager/`). What is
committed: `src/`, `tests/`, `apps_script/`, `scripts/`, `docs/`, `main.py`,
`pyproject.toml`, `uv.lock`, `README.md`, `.env.example`, `.gitignore`.
What is **not** committed (secrets or machine-specific): `.env`, `devices.json`,
`state/`, `logs/`, `tools/` (NSSM is downloaded by the install script),
`.clasp.json`, IDE folders.

1. Commit the latest changes:
   `git add -A` → `git commit -m "..."`.
2. Get the repository to the new PC, either:
   - **GitHub (recommended)**: create a **private** repository (e.g. under the
     lab account), then
     `git remote add origin https://github.com/<account>/helium-recovery.git` and
     `git push -u origin main`; or
   - **USB stick, no GitHub**: `git bundle create helium-recovery.bundle --all`,
     copy the file, and on the new PC `git clone helium-recovery.bundle C:\HeliumRecovery`.
3. Copy these files **by hand** (USB stick or the lab's password manager; never
   by e-mail, Slack or git):
   - `.env`
   - `devices.json` (ATL3 inlet valve keys)
   - optional: the `state\` folder (keeps alert state, the 24 h trend, the
     operation mode / activation toggles and the local transfer log).

## Part B — prepare the new PC (administrator)

1. Install Git and uv: `winget install Git.Git astral-sh.uv`.
   Only if you will edit the Apps Script: `winget install OpenJS.NodeJS.LTS`,
   then `npm i -g @google/clasp`.
2. Check the network: from the new PC, `ping` the pump, the three ATLs and
   Janis (addresses in `.env`: `HR_PUMP_HOST`, `HR_ATL_HOSTS`, `HR_JANIS_HOST`).
3. Clone to a short path without spaces, e.g. `C:\HeliumRecovery`:
   `git clone <URL or bundle> C:\HeliumRecovery`.
4. `cd C:\HeliumRecovery` → `uv sync` (installs Python 3.13 and the packages).
5. Put `.env`, `devices.json` (and `state\` if copied) into `C:\HeliumRecovery`.
6. `uv run pytest` — must pass (offline, touches no instrument).
7. If port 8090 is taken (e.g. by another program), set `HR_UI_PORT` in `.env`.
   To view the console from other PCs, allow the port in Windows Firewall:
   `New-NetFirewallRule -DisplayName "HeliumRecovery 8090" -Direction Inbound -Protocol TCP -LocalPort 8090 -Action Allow`.

Do **not** install the service yet.

## Part C — new Google files in the shared folder

Do this signed in as the **lab's shared Google account** (not a personal one):
the web app runs as the account that deploys it, so that account owns the
writes and must keep edit access to the shared folder.

Decide the folder, e.g. `Shared drives/Helium Recovery/Data_logs/`.

1. **Create two spreadsheets** in that folder, e.g.
   - `HeliumRecovery Status` (latest values; replaces `status_now`)
   - `HeliumRecovery Log` (one row every 3 min; replaces `status_log`)

   Use new names: do not reuse the old `CurrentData` /
   `HeliumRecoveryStatusLog` files unless nothing else writes to them any more.
   Copy each spreadsheet's id from its URL
   (`docs.google.com/spreadsheets/d/<ID>/edit`).
2. Note the id of the spreadsheet with the CMI / Wei-He3 levels (`HR_CRYO_SHEET_ID` in `.env`).
3. **Create the script on the status spreadsheet**: open `HeliumRecovery Status`
   → Extensions → Apps Script. Note the script id (Project Settings → IDs).
4. **Upload the code with clasp** (on the new PC):
   ```powershell
   clasp login                       # as the shared lab account
   ```
   Create `.clasp.json` in `C:\HeliumRecovery`:
   ```json
   {"scriptId": "<script id from step 3>", "rootDir": "apps_script"}
   ```
   then `clasp push --force`.
   (Without clasp: paste `HeliumStatus.gs` and `appsscript.json` into the editor
   by hand; show `appsscript.json` via Project Settings → "Show manifest file".)
5. In the Apps Script editor, Project Settings → **Script properties**, add
   `LOG_SHEET_ID` = the id of `HeliumRecovery Log` and `READ_SOURCES` = the
   CMI / Wei-He3 level spreadsheet id. Then run **`setup`** once, accept the permissions, and
   copy the token from the execution log (`HR_STATUS_SHEET_TOKEN=...`).
6. **Deploy** → New deployment → Web app, *Execute as: Me*, *Who has access:
   Anyone*. Copy the **web app URL** (`.../exec`) and the **deployment id**.
7. **Cryostat levels**: the shared account must be able to *view* the
   spreadsheet with the CMI / Wei-He3 levels (`HR_CRYO_SHEET_ID`). If that sheet
   also moves, put the new id and tab gids in `.env`.
8. **Update `.env`** on the new PC:
   ```
   HR_STATUS_SHEET_URL=<web app URL>
   HR_STATUS_SHEET_TOKEN=<token from setup>
   HR_GAS_DEPLOYMENT_ID=<deployment id>
   # only if changed:
   HR_CRYO_SHEET_ID=...
   HR_CMI_GID=...
   HR_WEI_GID=...
   ```
9. **Transfer log file**: "Helium Recovery System Operation" is created
   automatically next to `HeliumRecovery Log` at the first logged transfer. To
   keep earlier entries, copy the rows of the old test file's "Helium Transfer"
   tab into the new one after it exists (or create the file yourself in the
   shared folder with that exact name beforehand; the script will use it).
10. After the service has run ~15 min (Part E), run **`buildCharts`** in the
    Apps Script editor, then File → Share → **Publish to web** for the charts /
    tabs you want, and replace the `/embed` links in Notion.

The live check `scripts\gas_cycle.ps1` (without `-SkipLive`) writes and then
deletes temporary `ZZTEST` tabs in these files: run it before go-live, or use
`-SkipLive` once the files are in daily use.

## Part D — Slack

Nothing to change: the bot token and channel move with `.env`. Optionally add
manager member IDs to `HR_SLACK_MANAGERS`.

## Part E — switch over

1. **Old PC**: stop the service so it no longer reads or writes anything:
   administrator PowerShell in the old project folder →
   `powershell -ExecutionPolicy Bypass -File scripts\uninstall_service.ps1`
   (or keep it installed but stopped: `tools\nssm.exe stop HeliumRecovery` and set
   it to Manual in Windows Services, so it does not come back after a reboot).
2. **New PC** (administrator PowerShell in `C:\HeliumRecovery`):
   `powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1 -Download`.
3. **Check** within ~10 minutes:
   - Console `http://localhost:8090`: Status tab — pump, ATLs, valve connected;
     CMI / Wei-He3 readings present.
   - Slack: "Helium recovery program started".
   - `HeliumRecovery Status` tabs update every 30 s; `HeliumRecovery Log` gets
     a row every 3 min.
   - Internal → Activation status and the header mode are as expected.
   - One transfer simulation (Internal → Transfer simulation) opens fine.
4. **Restart test**: reboot the new PC once and confirm the console comes back
   without logging in.

**Roll back** if needed: uninstall on the new PC, reinstall / start on the old PC.
The old test Google files are untouched by all of this.

## Part F — afterwards

- Keep the old test files (`status_now`, `status_log`, the test operation file)
  read-only for a month, then archive or delete them.
- Retire the old web app: in its Apps Script project, Deploy → Manage
  deployments → Archive, so nothing can post to it any more.
- On the old PC, the project folder can be deleted once the new PC has run a
  week without problems (keep a copy of `state\` and `logs\` if wanted).
