"""Transfer Control: the Step 1 / Step 2 / Step 3 dialogs.

All operator texts are in `TEXT` below; edit them freely ({onset} and {seconds}
are filled in). The valve pictures are in ``ui/static/transfer/`` (see the
`*_VALVES` lists; a placeholder is shown for a missing file), each tagged with
what to do: OPEN or CLOSE.

Step 1  page 1: close the dewar valve, open the cryostat and pump bypass
        valves -> page 2: choose preset, live inlet pressure;
        Proceed writes the preset with the pump DISABLED and arms the auto
        re-enable; when the inlet pressure rises above the onset the pump is
        enabled (TransferController.on_pump_data), Proceed turns green and the
        dialog closes.
Step 2  delay in seconds + the cryostat pressure relief valve to open right
        after the pump stops -> large countdown -> pump disabled (current
        settings) -> reminder to open the relief valve now.
Step 3  page 1: restore preset 0 (normal operation, pump enabled) -> page 2:
        the Step 1 valves the other way round -> page 3: reconnect the ATL -> page 4:
        helium transfer log (pre-filled from the levels recorded when Step 1
        started; every field but Notes must be filled, or Skip to log nothing), uploaded to the
        "Helium Recovery System Operation" sheet (services/operation_log.py).

Opening Step 1 switches the system to the Transfer operation mode (back to the
previous mode if Step 1 is cancelled before writing); Step 3 ends it once
preset 0 is restored (core/modes.py).

With ``sim=True`` (Internal tab "Transfer simulation") the same dialogs run
against `SimTransfer`: nothing is written, the writes only wait briefly, and
the inlet-pressure jump is triggered by a button.

Every pump write still goes through PumpWorker.set_settings (validated, read
back, refused unless HR_PUMP_ALLOW_WRITES=1). The dialogs replace the separate
confirmation dialog: Proceed is the confirmation.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from collections.abc import Callable
from pathlib import Path

from nicegui import ui

from ..core.engine import Instrument
from ..core.runtime import Core
from ..drivers.base import DriverError
from ..services import operation_log as oplog
from . import theme
from .common import fmt
from .diagram import URL, _serve_static

log = logging.getLogger(__name__)

PICTURES = Path(__file__).parent / "static" / "transfer"
OPEN, CLOSE = "OPEN", "CLOSE"
# (picture, caption, action) per step
STEP1_VALVES = (("DewarValve.png", "Dewar valve", CLOSE),
                ("CryostatBypassValve.png", "Cryostat bypass valve", OPEN),
                ("PumpBypassValve.png", "Pump bypass valve", OPEN))
STEP3_VALVES = tuple((name, caption, OPEN if action == CLOSE else CLOSE) for name, caption, action in STEP1_VALVES)
STEP2_VALVES = (("CryostatPressureReliefValve.jpg", "Cryostat pressure relief valve", OPEN),)

TEXT = {
    # buttons and the descriptions under them on the Transfer Control tab
    "step1_button": "Transfer Step 1",
    "step1_desc": "Set the valves, choose the transfer preset and stop the pump; "
                  "it restarts by itself when the inlet pressure jumps.",
    "step2_button": "Transfer Step 2",
    "step2_desc": "Countdown, then disable the pump with its current settings.",
    "step3_button": "Transfer Step 3",
    "step3_desc": "Restore normal operation (preset 0, pump enabled).",
    # Step 1 dialog
    "step1_title": "Transfer Step 1",
    "step1_valves": "Please CLOSE the dewar valve and OPEN the cryostat bypass valve and the pump bypass valve, "
                    "then press Proceed.",
    "step1_preset": "Preset Pars",
    "step1_disable": "Proceed disables the pump with the chosen preset, until it detects a sharp increase "
                     "of the inlet pressure (above {onset} bar). Then the pump is enabled again with the same preset.",
    "step1_waiting": "Pump disabled. Waiting for the inlet pressure to jump above {onset} bar…",
    "step1_done": "Inlet pressure above {onset} bar: pump enabled.",
    "step1_cancelled": "Auto re-enable cancelled: the pump stays DISABLED. Use Step 3 or SET to enable it.",
    # Step 2 dialog
    "step2_title": "Transfer Step 2",
    "step2_delay": "Delay before the pump is disabled (s)",
    "step2_explain": "Proceed starts the countdown. At zero the pump's current settings are written again "
                     "with Enabled = False. Cancel during the countdown stops it without writing.",
    "step2_relief": "Right after the pump stops (countdown at zero), OPEN the cryostat pressure relief valve.",
    "step2_counting": "Pump will be disabled at zero",
    "step2_done": "Pump disabled. OPEN the cryostat pressure relief valve now.",
    "step2_relief_button": "Relief valve is open",
    # Step 3 dialog
    "step3_title": "Transfer Step 3",
    "step3_explain": "Proceed writes preset 0 (normal operation) and enables the pump.",
    "step3_done": "Normal operation restored, pump enabled.",
    "step3_valves": "Please OPEN the dewar valve and CLOSE the cryostat bypass valve and the pump bypass valve "
                    "before you continue.",
    "step3_valves_button": "Valves are set",
    "step3_atl": "Reconnect the ATL: disconnect the transfer line from the cryostat, connect the ATL "
                 "back to the recovery line and make all its connections as for normal operation.",
    "step3_atl_button": "ATL is connected",
    "step3_log": "Helium transfer log",
    "step3_log_hint": "Pre-filled from the levels recorded when Step 1 started and the levels now; "
                      "check them and fill in what is blank. Only Notes may stay empty.",
    "step3_log_no_start": "No Step 1 was recorded since the program started: fill in the start time and levels.",
    "step3_log_button": "Log transfer",
    "step3_logged": "Transfer logged in \"Helium Recovery System Operation\".",
    "step3_log_sim": "Simulation: the transfer was NOT logged. It would have been:",
    "step3_log_skip": "Skip",
    "step3_log_skipped": "Transfer not logged (skipped).",
    # simulation (Internal tab)
    "sim_title": "Transfer simulation",
    "sim_desc": "The same three dialogs, but nothing is written to the pump: for display and debugging.",
    "sim_banner": "SIMULATION · no pump settings are changed",
    "sim_jump": "Simulate inlet jump",
}

SIM_WRITE_S = 0.6  # how long a simulated write "takes"


class SimTransfer:
    """Stands in for TransferController in the dialogs; never touches the pump."""

    def __init__(self, real) -> None:
        self.limits, self.presets, self.step2_delay = real.limits, real.presets, real.step2_delay
        self.auto_enable_armed = False
        self.auto_enable_future: asyncio.Future | None = None

    async def _write(self, what: str) -> None:
        log.info("transfer simulation: %s (not written)", what)
        await asyncio.sleep(SIM_WRITE_S)

    async def step1_start(self, preset_index: int) -> None:
        self.auto_enable_armed, self.auto_enable_future = False, None
        await self._write(f"preset {preset_index}, pump disabled")
        self.auto_enable_armed = True

    def inlet_jump(self) -> None:
        if not self.auto_enable_armed:
            return
        self.auto_enable_armed = False
        self.auto_enable_future = asyncio.ensure_future(self._write("pump enabled"))

    def disarm(self) -> None:
        self.auto_enable_armed = False

    async def disable_now(self) -> None:
        await self._write("current settings, pump disabled")

    async def step3(self) -> None:
        await self._write("preset 0, pump enabled")


def _picture(name: str, caption: str, action: str | None = None) -> None:
    with ui.column().classes("items-center gap-1 grow basis-0"):
        if (PICTURES / name).exists():
            ui.image(f"{URL}/transfer/{name}").classes("w-full").style(
                "max-height:260px;object-fit:contain;border-radius:6px")
        else:
            with ui.column().classes("w-full items-center justify-center").style(
                    "height:200px;border:2px dashed var(--hr-line);border-radius:6px;background:var(--hr-tile)"):
                ui.icon("image").classes("text-4xl hr-muted")
                ui.label(f"ui/static/transfer/{name}").classes("hr-mono hr-note")
        with ui.row().classes("items-center gap-2 no-wrap"):
            if action:
                theme.Badge(action, "ok" if action == OPEN else "err")
            ui.label(caption).classes("text-[15px] font-medium text-center")


def _valves(valves) -> None:
    with ui.row().classes("w-full gap-4 no-wrap"):
        for name, caption, action in valves:
            _picture(name, caption, action)


def _say(label: ui.label, text: str, style: str = "color:var(--hr-text)") -> None:
    label.set_text(text)
    label.style(style)


def _set_kind(btn: ui.element, kind: str) -> None:
    btn.classes(remove="green red locked", add=kind)


def _dialog(title: str, sim: bool = False) -> tuple[ui.dialog, ui.column]:
    _serve_static()
    dlg = ui.dialog().props("persistent")
    with dlg, ui.card().classes("hr-card").style("min-width:min(680px,95vw);max-width:820px;padding:22px"):
        ui.label(title).classes("text-[21px]")
        if sim:
            theme.Badge(TEXT["sim_banner"], "warn")
        body = ui.column().classes("w-full gap-3")
    return dlg, body


async def _show(dlg: ui.dialog, tr, sim: bool, delete: bool = True) -> None:
    """Open and wait; real dialogs are counted so the "pump left disabled" alert waits meanwhile."""
    if not sim:
        tr.dialog_open += 1
    try:
        dlg.open()
        await dlg
    finally:
        if not sim:
            tr.dialog_open -= 1
    if delete:
        dlg.delete()


def _inlet(core: Core) -> str:
    p = core.engine.read().get(Instrument.PUMP_DATA)
    return fmt(p.inlet_pressure, 3) if p is not None else "—"


def _preset_label(core: Core, i: int) -> str:
    p = core.transfer.presets[i]  # SimTransfer shares the same presets
    kind = "Normal" if i == 0 else "Transfer"
    return (f"{i} · {kind} — inlet {p.inlet_pressure_setpoint:g}, comp on/off {p.comp_on_setpoint:g}/"
            f"{p.comp_off_setpoint:g}, setpoint {p.pressure_setpoint:g} mbar")


async def step1(core: Core, on_preset: Callable[[int], None], sim: bool = False) -> None:
    tr = SimTransfer(core.transfer) if sim else core.transfer
    onset = tr.limits.auto_enable_inlet_bar
    state = {"phase": "idle"}  # idle -> writing -> waiting -> done | failed
    dlg, body = _dialog(TEXT["step1_title"], sim)
    with body:
        page1 = ui.column().classes("w-full gap-3")
        page2 = ui.column().classes("w-full gap-3")
        page2.set_visibility(False)
        with page1:
            _valves(STEP1_VALVES)
            ui.label(TEXT["step1_valves"]).classes("text-[16px]")
            with ui.row().classes("w-full justify-end gap-2"):
                theme.button("Cancel", lambda: dlg.submit(None), kind="locked")
                theme.button("Proceed", lambda: (page1.set_visibility(False), page2.set_visibility(True)))
        with page2:
            default = 1 if len(tr.presets) > 1 else 0
            preset = ui.select({i: _preset_label(core, i) for i in range(len(tr.presets))}, value=default,
                               label=TEXT["step1_preset"]).props("outlined").classes("w-full")
            with ui.row().classes("items-baseline gap-3"):
                ui.label("Inlet pressure").classes("text-[15px] hr-muted")
                inlet = ui.label(_inlet(core)).classes("hr-mono text-[48px] font-medium").style("line-height:1.1")
                ui.label("bar").classes("text-[16px] hr-muted")
            ui.label(TEXT["step1_disable"].format(onset=onset)).classes("text-[15px]")
            status = ui.label("").classes("text-[15px] font-medium")
            with ui.row().classes("w-full justify-end gap-2"):
                if sim:
                    jump_btn = theme.button(TEXT["sim_jump"], lambda: tr.inlet_jump(), kind="locked",
                                            icon="trending_up")
                cancel_btn = theme.button("Cancel", lambda: cancel(), kind="locked")
                proceed_btn = theme.button("Proceed", lambda: proceed())

    async def proceed() -> None:
        if state["phase"] not in ("idle", "failed"):
            return
        state["phase"] = "writing"
        _set_kind(proceed_btn, "locked")
        preset.disable()
        _say(status, "Writing preset with the pump disabled…", "color:var(--hr-text)")
        try:
            await tr.step1_start(int(preset.value))
        except (DriverError, RuntimeError) as exc:
            state["phase"] = "failed"
            _say(status, f"Write failed: {exc}", "color:var(--hr-err)")
            _set_kind(proceed_btn, "")
            preset.enable()
            return
        on_preset(int(preset.value))
        _remember_start(core, sim)
        state["phase"] = "waiting"
        if sim:
            _set_kind(jump_btn, "")
        _say(status, TEXT["step1_waiting"].format(onset=onset), "color:var(--hr-warn)")

    def cancel() -> None:
        if state["phase"] == "waiting":
            tr.disarm()
            ui.notify(TEXT["step1_cancelled"], type="warning", multi_line=True)
        if state["phase"] != "writing":
            dlg.submit(None)

    def tick() -> None:
        inlet.set_text(_inlet(core))
        if state["phase"] != "waiting" or tr.auto_enable_armed:
            return
        fut = tr.auto_enable_future
        if fut is None:  # disarmed elsewhere
            state["phase"] = "failed"
            _say(status, TEXT["step1_cancelled"], "color:var(--hr-err)")
            return
        if not fut.done():
            _say(status, f"Inlet above {onset} bar: enabling the pump…")
            return
        if sim:
            _set_kind(jump_btn, "locked")
        if fut.exception() is not None:
            state["phase"] = "failed"
            _say(status, f"Enabling the pump failed: {fut.exception()}. The pump is DISABLED.", "color:var(--hr-err)")
            return
        state["phase"] = "done"
        _set_kind(proceed_btn, "green")
        _set_kind(cancel_btn, "locked")
        _say(status, TEXT["step1_done"].format(onset=onset), "color:var(--hr-ok)")
        ui.timer(1.5, lambda: dlg.submit("done"), once=True)

    with body:
        ui.timer(0.5, tick)
    entered = not sim and not core.modes.transfer
    if entered:  # opening Step 1 starts the Transfer operation mode
        core.modes.start_transfer()
    await _show(dlg, tr, sim)
    if entered and state["phase"] == "idle":  # cancelled before anything was written
        core.modes.end_transfer("Transfer Step 1 cancelled")


async def step2(core: Core, sim: bool = False) -> None:
    tr = SimTransfer(core.transfer) if sim else core.transfer
    state = {"phase": "idle"}  # idle -> counting -> writing -> done | failed | aborted
    dlg, body = _dialog(TEXT["step2_title"], sim)
    with body:
        page1 = ui.column().classes("w-full gap-3")
        page2 = ui.column().classes("w-full items-center gap-2")
        page2.set_visibility(False)
        with page1:
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label(TEXT["step2_delay"]).classes("text-[16px]")
                seconds = ui.number(value=tr.step2_delay, min=0, max=3600, step=1, format="%.0f",
                                    suffix="s").props("outlined dense").classes("hr-input w-36")
            ui.label(TEXT["step2_explain"]).classes("text-[15px]")
            with ui.row().classes("w-full justify-center"):
                with ui.column().classes("items-center").style("width:min(340px,100%)"):
                    _valves(STEP2_VALVES)
            ui.label(TEXT["step2_relief"]).classes("text-[16px] font-medium")
            with ui.row().classes("w-full justify-end gap-2"):
                theme.button("Cancel", lambda: dlg.submit(None), kind="locked")
                theme.button("Proceed", lambda: start())
        with page2:
            count = ui.label("").classes("hr-mono font-semibold").style("font-size:140px;line-height:1")
            status = ui.label(TEXT["step2_counting"]).classes("text-[17px]")
            relief = ui.column().classes("w-full items-center")
            relief.set_visibility(False)
            with relief, ui.column().classes("items-center").style("width:min(340px,100%)"):
                _valves(STEP2_VALVES)
            with ui.row().classes("w-full justify-end gap-2"):
                abort_btn = theme.button("Cancel", lambda: abort(), kind="red")
                relief_btn = theme.button(TEXT["step2_relief_button"], lambda: dlg.submit("done"))
                relief_btn.set_visibility(False)

    def abort() -> None:
        if state["phase"] == "counting":
            state["phase"] = "aborted"
            ui.notify("Step 2 cancelled before the pump was disabled", type="info")
            dlg.submit(None)
        elif state["phase"] in ("done", "failed"):
            dlg.submit(None)

    async def start() -> None:
        if state["phase"] != "idle":
            return
        delay = max(0.0, float(seconds.value or 0))
        tr.step2_delay = delay
        state["phase"] = "counting"
        page1.set_visibility(False)
        page2.set_visibility(True)
        loop = asyncio.get_running_loop()
        end = loop.time() + delay
        while (left := end - loop.time()) > 0:
            if state["phase"] != "counting":
                return
            count.set_text(f"{int(left) + 1}")
            await asyncio.sleep(0.1)
        if state["phase"] != "counting":
            return
        state["phase"] = "writing"
        count.set_text("0")
        _set_kind(abort_btn, "locked")
        _say(status, "Disabling the pump…")
        try:
            await tr.disable_now()
        except (DriverError, RuntimeError) as exc:
            state["phase"] = "failed"
            _say(status, f"Write failed: {exc}", "color:var(--hr-err)")
            abort_btn.classes(remove="locked")
            return
        state["phase"] = "done"
        _say(status, TEXT["step2_done"], "color:var(--hr-warn);font-size:20px;font-weight:600")
        count.set_visibility(False)
        relief.set_visibility(True)
        abort_btn.set_visibility(False)
        relief_btn.set_visibility(True)

    await _show(dlg, tr, sim, delete=False)
    if state["phase"] == "counting":  # closed some other way: never write afterwards
        state["phase"] = "aborted"
    dlg.delete()


def _remember_start(core: Core, sim: bool) -> None:
    """Levels when Step 1 starts, for the Step 3 transfer log."""
    start = oplog.TransferStart(datetime.now(), oplog.levels(core.engine.read()))
    if sim:
        core.transfer.sim_transfer_start = start
    else:
        core.transfer.transfer_start = start


def _next(page: ui.column, nxt: ui.column) -> None:
    page.set_visibility(False)
    nxt.set_visibility(True)


async def step3(core: Core, on_preset: Callable[[int], None], sim: bool = False) -> None:
    tr = SimTransfer(core.transfer) if sim else core.transfer
    dlg, body = _dialog(TEXT["step3_title"], sim)
    with body:
        page1, page2, page3, page4 = (ui.column().classes("w-full gap-3") for _ in range(4))
        for page in (page2, page3, page4):
            page.set_visibility(False)
        with page1:
            ui.label(TEXT["step3_explain"]).classes("text-[16px]")
            ui.label(_preset_label(core, 0)).classes("hr-mono text-[13px] hr-muted")
            status = ui.label("").classes("text-[15px] font-medium")
            with ui.row().classes("w-full justify-end gap-2"):
                theme.button("Cancel", lambda: dlg.submit(None), kind="locked")
                proceed_btn = theme.button("Proceed", lambda: proceed())
        with page2:
            _valves(STEP3_VALVES)
            ui.label(TEXT["step3_valves"]).classes("text-[17px] font-medium")
            with ui.row().classes("w-full justify-end gap-2"):
                theme.button(TEXT["step3_valves_button"], lambda: _next(page2, page3))
        with page3:
            ui.label(TEXT["step3_atl"]).classes("text-[17px]")
            with ui.row().classes("w-full justify-end gap-2"):
                theme.button(TEXT["step3_atl_button"], lambda: open_log())

    busy = {"on": False, "restored": False}

    async def proceed() -> None:
        if busy["on"]:
            return
        busy["on"] = True
        _set_kind(proceed_btn, "locked")
        _say(status, "Writing preset 0…", "color:var(--hr-text)")
        try:
            await tr.step3()
        except (DriverError, RuntimeError) as exc:
            _say(status, f"Write failed: {exc}", "color:var(--hr-err)")
            _set_kind(proceed_btn, "")
            busy["on"] = False
            return
        tr.disarm()
        busy["restored"] = True
        on_preset(0)
        _set_kind(proceed_btn, "green")
        _say(status, TEXT["step3_done"], "color:var(--hr-ok)")
        ui.timer(1.2, lambda: _next(page1, page2), once=True)

    async def open_log() -> None:
        _next(page3, page4)
        with page4:
            await _transfer_log_form(core, dlg, sim)

    await _show(dlg, tr, sim)
    if busy["restored"] and not sim:  # preset 0 is back: the Transfer operation mode ends
        core.modes.end_transfer()


LOG_LABELS = {
    "date": "Date (YYYY-MM-DD)", "start": "Start time (HH:MM)", "end": "End time (HH:MM)",
    "operator": "Operator", "atl": "From ATL", "atl_start": "ATL start level", "atl_end": "ATL end level",
    "cryostat": "To cryostat", "cryo_start": "Cryostat start level", "cryo_end": "Cryostat end level",
    "cryo_unit": "Cryostat level unit", "notes": "Other notes",
}
_NUMBERS = ("atl_start", "atl_end", "cryo_start", "cryo_end")


async def _transfer_log_form(core: Core, dlg: ui.dialog, sim: bool) -> None:
    """Page 4 of Step 3: pre-filled transfer record, uploaded once nothing required is blank."""
    start = core.transfer.sim_transfer_start if sim else core.transfer.transfer_start
    rec = oplog.draft(start, oplog.levels(core.engine.read()), datetime.now())
    operators = await core.operation_log.operators()

    ui.label(TEXT["step3_log"]).classes("text-[18px] font-medium")
    ui.label(TEXT["step3_log_hint"] if start else TEXT["step3_log_no_start"]).classes("text-[14px] hr-muted")
    w: dict[str, ui.element] = {}
    dense, L = "outlined dense", LOG_LABELS
    with ui.grid(columns=3).classes("w-full gap-2"):
        for key in ("date", "start", "end"):
            w[key] = ui.input(L[key], value=getattr(rec, key)).props(dense)
    with ui.row().classes("w-full items-center gap-2 no-wrap"):
        w["operator"] = ui.input(L["operator"], value=rec.operator, autocomplete=operators).props(dense).classes("grow")
        with ui.button(icon="history").props("flat dense").tooltip("Earlier operators"):
            with ui.menu():
                if not operators:
                    ui.menu_item("(no earlier transfers)")
                for name in operators:
                    ui.menu_item(name, lambda n=name: w["operator"].set_value(n))
    with ui.grid(columns=3).classes("w-full gap-2"):
        w["atl"] = ui.select(list(oplog.ATLS), value=rec.atl or None, label=L["atl"]).props(dense)
        w["atl_start"] = ui.number(L["atl_start"], value=rec.atl_start, suffix="L").props(dense)
        w["atl_end"] = ui.number(L["atl_end"], value=rec.atl_end, suffix="L").props(dense)
        w["cryostat"] = ui.select(list(oplog.CRYOSTATS), value=rec.cryostat or None, label=L["cryostat"],
                                  with_input=True, new_value_mode="add-unique").props(dense)
        w["cryo_start"] = ui.number(L["cryo_start"], value=rec.cryo_start).props(dense)
        w["cryo_end"] = ui.number(L["cryo_end"], value=rec.cryo_end).props(dense)
    w["cryo_unit"] = ui.input(L["cryo_unit"], value=rec.cryo_unit).props(dense).classes("w-48")
    w["notes"] = ui.textarea(L["notes"]).props("outlined autogrow").classes("w-full")
    status = ui.label("").classes("text-[15px] font-medium")
    def skip() -> None:
        if state["busy"]:
            return
        if not state["done"]:
            log.info("transfer log skipped%s", " (simulation)" if sim else "")
            ui.notify(TEXT["step3_log_skipped"], type="info")
        dlg.submit("skipped")

    with ui.row().classes("w-full justify-end gap-2"):
        skip_btn = theme.button(TEXT["step3_log_skip"], skip, kind="locked")
        close_btn = theme.button("Close", lambda: dlg.submit("done"), kind="locked")
        close_btn.set_visibility(False)
        log_btn = theme.button(TEXT["step3_log_button"], lambda: submit(), icon="save")

    def refill(kind: str) -> None:
        """A different ATL / cryostat chosen: take its recorded start level and its level now."""
        now_levels = oplog.levels(core.engine.read())
        if kind == "atl":
            rec.atl = w["atl"].value or ""
            oplog.fill_atl(rec, start, now_levels)
            w["atl_start"].set_value(rec.atl_start)
            w["atl_end"].set_value(rec.atl_end)
        else:
            rec.cryostat = w["cryostat"].value or ""
            oplog.fill_cryostat(rec, start, now_levels)
            w["cryo_start"].set_value(rec.cryo_start)
            w["cryo_end"].set_value(rec.cryo_end)
            w["cryo_unit"].set_value(rec.cryo_unit)

    w["atl"].on_value_change(lambda: refill("atl"))
    w["cryostat"].on_value_change(lambda: refill("cryostat"))
    state = {"busy": False, "done": False}

    def collect() -> oplog.TransferRecord:
        out = oplog.TransferRecord()
        for key, el in w.items():
            v = el.value
            if key in _NUMBERS:
                setattr(out, key, None if v is None else float(v))
            else:
                setattr(out, key, "" if v is None else str(v).strip())
        return out

    async def submit() -> None:
        if state["busy"] or state["done"]:
            return
        out = collect()
        missing = out.missing()
        for key, el in w.items():
            if key in missing:
                el.props("error")
            else:
                el.props(remove="error")
        if missing:
            _say(status, "Please fill in: " + ", ".join(L[k] for k in missing), "color:var(--hr-err)")
            return
        if sim:
            state["done"] = True
            shown = "; ".join(f"{k}={v}" for k, v in out.params() if v)
            _say(status, f"{TEXT['step3_log_sim']} {shown}", "color:var(--hr-warn)")
            skip_btn.set_visibility(False)
            _set_kind(log_btn, "locked")
            close_btn.set_visibility(True)
            return
        state["busy"] = True
        _set_kind(log_btn, "locked")
        _say(status, "Logging…")
        try:
            await core.operation_log.log_transfer(out)
        except Exception as exc:  # upload failed; the record is kept in the local CSV
            state["busy"] = False
            _set_kind(log_btn, "")
            close_btn.set_visibility(True)
            _say(status, f"Upload failed ({exc}). Saved in {core.operation_log.local_path}; "
                         "press Log transfer to try again, or Close.", "color:var(--hr-err)")
            return
        state.update(busy=False, done=True)
        skip_btn.set_visibility(False)
        _set_kind(log_btn, "green")
        _say(status, TEXT["step3_logged"], "color:var(--hr-ok)")
        ui.timer(1.5, lambda: dlg.submit("done"), once=True)
