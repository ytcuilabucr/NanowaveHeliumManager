# Slack alerts, 10 PM report and Guardian mode — design for review

Status (2026-10-12): **§1 and §2 implemented** (`services/alerts.py`,
`services/forecast.py`, `core/trend.py`; Internal tab → "Message
communication system" → Alerts).
**§3 Guardian mode is now the "Agent" operation mode** (header indicator,
`core/modes.py`): the mode exists, but no automatic actions are implemented
yet; the design below is kept for later.

Operation modes (header, next to the clock): **Observer** (default, the
program changes nothing by itself), **Transfer** (from opening Transfer
Step 1 until Step 3 restores preset 0; "ATL not responding" is not raised),
**Agent** (formerly Guardian; never survives a restart). Mode changes are
posted as info messages.

**Activation status** (Internal tab toggles, kept in `state/modes.json`): a
switched-off instrument gets no alerts or events, an active alert of it is
dropped without a "resolved" message, and its cards are greyed out in every
view. First default: `HR_ALERT_ARCHIVED=atl2,wei,janis` (under engineering).

Implementation notes / deviations:
- `pump_disabled_long`: the 10 min "disabled" condition plus the 5 min
  debounce, i.e. the alert comes ~15 min after the pump was disabled.
  Real transfer dialogs being open or Step 1 waiting pause it.
- `step1_waiting_long`: 30 min, as proposed.
- `atl_offline` is held while the unit's last reading was in precool/transfer
  mode, or while a Transfer Control dialog is open / Step 1 is waiting.
- `pump_settings_changed`: the pump settings are now re-read every 60 s
  (before: only at connect / after a write) so panel changes are seen.
- Offline rules count from the last good reading (no extra debounce).
- Not implemented: `atl_status_led` (needs the LED code table),
  `heartbeat_missing` (needs a separate watchdog), and §2.2 step 6
  (previous nights). The gas-balance cross-check (step 5) is shown as
  information ("liquefiers use ≈ x bar/h") rather than compared, because the
  recovered-gas inflow is not measured.
- Without `SLACK_BOT_TOKEN` / `SLACK_CHANNEL` (the case on the lab PC today)
  everything runs and the Internal tab shows what would have been sent.
- Alert state and the 24 h trend are kept in `state/` (git-ignored).

Numbers marked *(?)* are still open; the open questions are at the end.

What the program can see today (everything below is built only from these):

| Source | Values | Read every |
|---|---|---|
| MP Hub pump | inlet P, tank P, outlet P (bar), outlet tank level %, oil temp °C, He flow in, PLC settings incl. *enabled* | 0.2 s |
| ATL1 / ATL2 / ATL3 | liquid volume L, dewar pressure psi, diode temp K, gas flow, liquefaction rate L/day, heater %, liquid mode / transfer mode, status LEDs | 10 s |
| Janis | LHe level (in) and volume | 180 s |
| CMI, Wei-He3 | LHe level from the Google sheet their own loggers write | 180 s |
| ATL3 inlet valve | open / closed | 30 s |
| The program itself | instrument connection state, error list, pump write results | — |

Teslatron, the He storage bag and the purifier have no sensors, so nothing
can be said about them.

---

## 1) Alerts for unusual conditions

### 1.1 How "not too frequent" is enforced

Every scenario below is an **alert key** (e.g. `outlet_high`). Each key has its
own small state machine:

```
 normal ──(condition true for ≥ debounce)──► ACTIVE: send alert once
 ACTIVE ──(still true)──► reminders only at 1 h, then 3 h, then every 6 h
 ACTIVE ──(clear past the hysteresis band for ≥ debounce)──► send "resolved" once ──► normal
```

- **Debounce**: the condition must hold continuously (e.g. 2 min) before the
  first message, so a single bad reading or a short spike never posts.
- **Hysteresis**: an alert set at 7.5 bar clears only below 7.3 bar, so a value
  hovering around the limit doesn't flap on/off.
- **Reminders back off**: 1 h → 3 h → every 6 h, instead of repeating every
  few minutes for as long as the condition lasts.
- **Bundling**: alerts that start within the same 60 s go out as one message.
- **Global cap**: at most 6 messages per hour to the group channel; anything
  over the cap is held and sent as one summary when the hour ends.
- **Severity**: `info` (no ping), `warning` (channel message), `critical`
  (channel message + @mention of the managers).
- **Mute**: a "Mute alerts for 1 h / until morning" button on the Internal
  tab, for maintenance work; muting is itself posted to Slack.
- **Restart-safe**: alert state is kept in a small JSON file, so restarting
  `main.py` does not re-send every active alert.

### 1.2 Scenario list

Pump / gas storage (MP Hub)

| Key | Condition (proposed)                                               | Debounce | Severity | Why it matters |
|---|--------------------------------------------------------------------|----------|---|---|
| `outlet_high` | outlet P ≥ 7.5 bar (clear < 7.3)                                   | 2 min    | critical | storage nearly full; pump stops at max outlet (8 bar), then recovered gas backs up into the bag |
| `outlet_low` | outlet P ≤ 2.3 bar (clear > 2.5)                                   | 5 min    | warning | little gas left for the liquefiers |
| `outlet_very_low` | outlet P ≤ 2 bar                                                   | 5 min    | critical | liquefiers will starve |
| `inlet_high` | inlet P ≥ 1.40 bar (Transfer tab "Inlet P_Overlimit")              | 1 min    | critical | recovery line over-pressure, risk of losing gas through relief / bag full |
| `tank_high` | tank P ≥ 1.35 bar ("Tank P_Overlimit")                             | 1 min    | critical | same, on the bag side |
| `pump_disabled_long` | pump *enabled = False* for > 10 min and no transfer dialog open    | 5 min    | critical | catches a Step 1/2 left half-finished — gas not being recovered |
| `step1_waiting_long` | Step 1 armed (waiting for the inlet jump) > 30 min *(?)*           | —        | warning | transfer probably abandoned with the pump off |
| `oil_temp_high` | oil temp ≥ 90 °C                                                   | 5 min    | warning | compressor overheating |
| `outlet_rising_fast` | outlet P rising > 0.5 bar/h over 30 min                            | 30 min   | info | unusually high boil-off / recovery |
| `pump_settings_changed` | PLC settings differ from the last ones this program wrote, and nobody used SET | —        | info | someone changed settings on the PLC panel |

Liquefiers (ATL1–3)

| Key | Condition (proposed)                                    | Debounce | Severity | Why it matters |
|---|---------------------------------------------------------|---|---|---|
| `atl_mode_changed` | liquid mode changed and **not** by this program         | — | info | someone changed it at the unit |
| `atl_stopped` | mode = Stop while it was liquefying                     | 2 min | warning | unexpected stop |
| `atl_dewar_full` | liquid volume ≥ 85 % of capacity *(?)*                  | 10 min | info | time to transfer out; liquefying further does nothing |
| `atl_status_led` | status LED code indicates a fault *(code table needed)* | 2 min | warning | |

Cryostats

| Key | Condition (proposed)                                                   | Debounce | Severity |
|---|------------------------------------------------------------------------|---|---|
| `janis_low` | Janis level ≤ 17 in                                                    | 10 min | warning |
| `janis_very_low` | Janis level ≤ 15 in                                                    | 10 min | critical |
| `cmi_low` / `cmi_very_low` | CMI level ≤ 10 / 6 in                                                  | 10 min | warning / critical |
| `cryo_stale` | CMI / Wei sheet not updated by its logger for > 1 h (already detected) | — | info |

Communication / the program itself

| Key | Condition                                                                                      | Debounce | Severity |
|---|------------------------------------------------------------------------------------------------|---|---|
| `pump_offline` | no pump reading for 2 min                                                                      | 2 min | critical (the pump data is what everything else relies on) |
| `atl_offline` | one ATL unreadable for 10 min, while not during helium transfer                                | 10 min | warning |
| `janis_offline` | Janis unreadable for 30 min                                                                    | 30 min | info |
| `valve_offline` | ATL3 inlet valve unreadable for 30 min                                                         | 30 min | info |
| `sheets_failing` | Sheets upload failing for 30 min                                                               | 30 min | info |
| `write_failed` | a pump / ATL / valve write failed or the read-back did not match                               | — | warning |
| `program_started` | `main.py` started (after a crash or a reboot this tells you)                                   | — | info |
| `heartbeat_missing` | *(optional)* a tiny separate watchdog posts if the main program stops posting its daily report | — | critical |

---

## 2) 10 PM report with an overnight forecast

### 2.1 When and to whom

- Every day at **22:00 America/Los_Angeles** to the group channel.
- If any forecast reaches a danger point before **08:00** next morning (the
  "horizon", configurable), the message starts with a ⚠️ line that
  @mentions the managers (Slack user IDs from `.env`, e.g.
  `HR_SLACK_MANAGERS=U01ABC,U02DEF`).
- A "Send report now" button on the Internal tab sends the same report on
  demand (useful to test the wording).

### 2.2 How the expectation is made

1. **Keep a longer trend.** Today the program keeps only ~50 min of history
   for charts. Add a 1-minute-average buffer of 24 h for each forecast value
   (outlet P, inlet P, tank P, ATL liquid volumes, Janis / CMI / Wei levels).
   On start-up it can be back-filled from the `status_log` sheet (3-min rows),
   so a restart at 21:30 still gives a good 22:00 forecast.
2. **Fit the recent slope.** For each value, fit a straight line to the last
   **3 h** (19:00–22:00) using a robust fit (Theil–Sen: the median of pairwise
   slopes), so a single spike or one transfer step does not dominate.
3. **Check whether the slope is trustworthy.**
   - If the value had a step change in the window (e.g. a transfer at 20:30),
     fit only the part after the step.
   - If the scatter around the line is large compared to the predicted change,
     report the value but say "trend unclear" instead of a forecast.
   - If a mode changed in the window (ATL mode, pump enabled/disabled), use
     only the data since the change.
4. **Project to the horizon.** value at 08:00 = now + slope × hours left.
   Also compute **time to threshold** = (threshold − now) / slope when moving
   toward it.
5. **Cross-check with the gas balance** (outlet storage only):
   equivalent liquid in storage L = (outlet P − 1) × 3000 / 757 (the same
   formula the status log uses). Gas consumed by the liquefiers ≈ sum of ATL
   liquefaction rates (L/day, only for units that are liquefying). If the
   slope-based and the balance-based outlet forecasts disagree by more than
   a factor of 2, both are shown.
6. **Compare with previous nights** *(phase 2)*: the median overnight change
   of the last 7 nights from the status log; a night far outside that is
   flagged as unusual.
7. **Classify.** For each value: `OK` / `watch` (reaches a warning threshold
   before 08:00) / `danger` (reaches a critical threshold before 08:00 → mention
   managers). Thresholds are the same ones as in the alert table above.

### 2.3 Example messages

Normal night:

```
🌙 Helium recovery · nightly status · Sat 11 Oct 22:00

Pump (MP Hub) · enabled
  Inlet 1.302 bar · Tank 1.291 bar · Outlet 5.84 bar (≈ 19.2 L liquid in storage)
  Outlet trend −0.11 bar/h → ≈ 4.7 bar at 08:00   ✅
ATL1  Slow      41.2 / 80 L   +0.9 L/h → ≈ 50 L at 08:00   ✅
ATL2  Medium    63.0 / 80 L   +1.1 L/h → ≈ 74 L at 08:00   ✅  (92 % at 08:00)
ATL3  Liquefy   97 / 160 L    +1.6 L/h → ≈ 113 L at 08:00  ✅
Janis 18.6 in   −0.42 in/h → ≈ 14.4 in at 08:00   ✅
CMI   21.0 in   −0.35 in/h → ≈ 17.5 in at 08:00   ✅
Wei-He3  no fresh data (sheet last updated 14:05)

Last 24 h: 2 alerts (both resolved) · Guardian mode: off
```

Danger expected overnight:

```
⚠️ @Manager1 @Manager2 — expected to need attention before morning

🌙 Helium recovery · nightly status · Sat 11 Oct 22:00

Pump (MP Hub) · enabled
  Outlet 7.02 bar, rising +0.08 bar/h → reaches 7.5 bar at ≈ 04:00 ⚠️
  (storage nearly full; ATL1 is in Direct recovery, ATL2 stopped —
   switching one liquefier to Liquefy would use ≈ 0.2 bar/h)
ATL1  Direct recovery  22.5 / 80 L
ATL2  Stop             79.1 / 80 L   (dewar full)
ATL3  Liquefy          140 / 160 L   +1.5 L/h → full (≈ 152 L, 95 %) at ≈ 06:00 ⚠️
Janis  6.1 in, −0.45 in/h → 4 in (critical) at ≈ 02:40 ⚠️
CMI   21.0 in   −0.35 in/h → ≈ 17.5 in at 08:00   ✅

Guardian mode: off — enable it from the Internal tab to let the program
switch ATL1/ATL2 by outlet pressure overnight.
```

Uncertain trend:

```
Outlet 4.95 bar · trend unclear (transfer at 21:10, only 50 min of data since)
```

---

## 3) Guardian mode (overnight liquefier control by outlet pressure)

> **Disabled.** Not implemented for now; the design below is kept for later.

### 3.1 Idea

While Guardian mode is on, the program watches the pump **outlet pressure**
(the gas storage) and switches liquefiers between **Liquefy** and
**Direct recovery** with a hysteresis band:

```
outlet P
  8.0 ─ pump max outlet
  7.0 ─ ▲ switch to LIQUEFY when ≥ 7.0 for 10 min   (use gas, storage too full)
       │
       │  dead band: no changes
       │
  3.0 ─ ▼ switch to DIRECT RECOVERY when ≤ 3.0 for 10 min  (stop using stored gas)
  2.2 ─ outlet_low alert
```

(7.0 / 3.0 bar are placeholders *(?)*.)

### 3.2 Which unit does what *(needs your decision)*

| Unit | Outlet high → | Outlet low → |
|---|---|---|
| ATL1 (ATL80) | Liquefy at speed *Slow / Medium / Fast (?)* | Direct recovery |
| ATL2 (ATL80) | same, or keep as a second stage (switch only if ATL1 alone is not enough after 1 h) | Direct recovery |
| ATL3 (liquefy-only model) | it can only be set to Liquefy; leave it untouched? | — (no Direct recovery on this model) |

### 3.3 Safety rules

- **Off by default**, switched on per night from a button (Internal tab, and
  optionally also Transfer Control). Needs `HR_ATL_ALLOW_WRITES=1`.
- **Ends by itself** at a set time (e.g. 08:00) *(?)*, and always when the
  program restarts (it never comes back on by itself).
- **Only touches units in a mode it owns**: if a unit is in Stop, Purge,
  Precool or Transfer, or someone changes its mode by hand, Guardian leaves
  that unit alone for the rest of the night and posts that it did.
- **Minimum dwell**: at least 30 min *(?)* between two switches of the same
  unit; at most 4 switches per unit per night *(?)*; after that it only alerts.
- **Fresh data only**: no switch if the pump reading is older than 1 min or
  the ATL reading older than 2 min.
- **Dewar full**: never switch a unit to Liquefy if its dewar is ≥ 95 % full;
  post a warning instead (the gas then has nowhere to go → managers mentioned).
- **Every switch uses the existing write path** (single POST, then read-back of
  the mode); a failed read-back posts a `write_failed` alert and Guardian
  stops controlling that unit.
- **Every action is posted** to Slack and logged:

```
🛡️ Guardian · 01:42 · outlet 7.03 bar (≥ 7.0 for 10 min) → ATL1 switched to Liquefy (Slow), read-back OK
🛡️ Guardian · 05:10 · outlet 2.98 bar (≤ 3.0 for 10 min) → ATL1 switched to Direct recovery, read-back OK
🛡️ Guardian · 03:15 · ATL2 mode changed by hand (Stop) — Guardian no longer controls ATL2 tonight
🛡️ Guardian · 08:00 · ended. 2 switches tonight. Outlet 4.1 bar.
```

- The 10 PM report says whether Guardian is on and what it would do next.

---

## Open questions for you

1. Thresholds marked *(?)*: outlet very-low, oil temperature, dewar full / low
   %, dewar pressure range, diode temperature, Janis / CMI / Wei low levels.
2. Who are the managers to @mention (Slack user IDs), and should critical
   alerts at night *also* mention them, or only the 10 PM forecast?
3. Is one channel enough, or do you want critical alerts in a separate
   channel?
4. Forecast horizon: 08:00 next morning OK? Weekend different?
5. Guardian: liquefy speed for ATL1/ATL2, whether ATL2 is a second stage, what
   to do with ATL3, the two switching pressures, and when it should end.
6. Should Guardian be switchable from Slack (e.g. replying `guardian on`)? This
   needs a Slack app with event subscriptions; possible later, not in v1.
7. ATL status LED codes: is there a table of what the codes mean (the ATL
   manual), so `atl_status_led` can name the fault?
