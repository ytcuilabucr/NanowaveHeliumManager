"""Web UI (NiceGUI): the Helium Recovery operator console, plus a JSON API.

Layout: header,
tab bar (Table View, Graphic View, Diagram View, Transfer Control, BP Valve
Tuning, Status, and a hidden Internal page), page heading, cards, footer.
``GET /QueryMPHUBData`` returns the pump readings;
``GET /api/snapshot`` returns everything.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from nicegui import app, ui

from .. import service
from ..config import Config
from ..core.engine import Instrument, Snapshot
from ..core.errors import ErrorKind
from ..core.modes import DEVICES, OperationMode
from ..core.runtime import Core
from ..drivers.atl import COMPRESSOR_COOLDOWN_S, ATLModel, LiquidMode
from ..drivers.base import DriverError
from ..drivers.mphub import SETTINGS_MAP, PumpSettings
from ..services.alerts import CRITICAL, DEVICE_NAMES, WARNING
from . import theme, transfer_dialogs
from .common import describe_settings, liquid_mode_name
from .common import fmt as _fmt
from .common import fmt_age as _age
from .diagram import Diagram
from .theme import Badge, big_value, card, kv, link_state, notice, page_heading, reading_rows, tile, vessel

log = logging.getLogger(__name__)

UI_REFRESH_S = 0.5
CHART_REFRESH_S = 2.0
# Graphic View indicator ranges (display only)
ATL_SCALE_L = {Instrument.ATL1: 80.0, Instrument.ATL2: 80.0, Instrument.ATL3: 160.0}
CRYO_SCALE = {Instrument.JANIS: (29.0, "in"), Instrument.CMI: (32.0, "in"), Instrument.WEI: (100.0, "L")}
GAUGE_MAX_BAR = {"inlet_pressure": 1.5, "tank_pressure": 1.5, "outlet_pressure": 8.0}
# Graphic View alert markers: the Slack alert rules (services/alerts.py), yellow = warning,
# red = critical. Rules about one gauge or vessel mark that card; all are listed in the banner.
GV_GAUGE_RULES = {"outlet_high": "outlet_pressure", "outlet_low": "outlet_pressure",
                  "outlet_very_low": "outlet_pressure", "inlet_high": "inlet_pressure",
                  "tank_high": "tank_pressure"}
GV_DEVICE_SLOTS = {"atl1": Instrument.ATL1, "atl2": Instrument.ATL2, "atl3": Instrument.ATL3,
                   "janis": Instrument.JANIS, "cmi": Instrument.CMI, "wei": Instrument.WEI}
# Activation status (Internal tab): a device switched off is greyed out in every view
DEVICE_SLOTS = {"pump": (Instrument.PUMP_DATA, Instrument.PUMP_SETTINGS), **{d: (s,) for d, s in GV_DEVICE_SLOTS.items()},
                "valve": (Instrument.VALVE,)}
SLOT_DEVICE = {slot: d for d, slots in DEVICE_SLOTS.items() for slot in slots}
INACTIVE_TIP = "Switched off (Internal tab · Activation status): no alerts, controls disabled"
# operation mode indicator (header): icon, explanation
MODE_INFO = {
    OperationMode.OBSERVER: ("visibility", "Reads and shows everything. The program changes no settings by itself; "
                                           "operators still use the write controls as before."),
    OperationMode.TRANSFER: ("swap_vert", "Helium transfer in progress. Starts when Transfer Step 1 is opened and ends "
                                          "when Step 3 restores preset 0. \"ATL not responding\" alerts are ignored."),
    OperationMode.AGENT: ("smart_toy", "The program may change settings by itself (formerly Guardian mode). "
                                       "No automatic actions are implemented yet."),
}

core: Core | None = None

# Register table rows: (field, label, register, unit)
REGISTERS = [("enabled", "Enabled?", "MB11", "bool")] + [
    (f, label, f"MI{SETTINGS_MAP[f]}", unit)
    for f, label, unit in [
        ("inlet_pressure_setpoint", "Inlet Pressure Setpoint", "mbar"),
        ("comp_on_setpoint", "Comp on Setpoint", "mbar"),
        ("comp_off_setpoint", "Comp off Setpoint", "mbar"),
        ("max_outlet_pressure", "Max Outlet Pressure", "mbar"),
        ("pressure_setpoint", "Pressure Setpoint", "mbar"),
        ("hiflow_onset", "HiFlow Onset", "l/min"),
        ("bp_valve_p_gain", "BP Valve P Gain", "—"),
        ("bp_valve_i_gain", "BP Valve I Gain", "—"),
        ("bp_valve_d_gain", "BP Valve D Gain", "—"),
    ]
]
GAINS = {"bp_valve_p_gain", "bp_valve_i_gain", "bp_valve_d_gain"}
SERIES_COLORS = ["#2466a2", "#238a8a", "#6e6bb8"]
CRYO = [(Instrument.JANIS, "Janis"), (Instrument.CMI, "CMI"), (Instrument.WEI, "Wei-He3")]
SLOT_NAMES = {Instrument.CMI: "CMI", Instrument.WEI: "Wei-He3"}
COUNTER_NAMES = {
    ErrorKind.TCP_READ: "TCP read", ErrorKind.HTTP_READ: "HTTP read", ErrorKind.HTTP_POST: "HTTP post",
    ErrorKind.JANIS_READ: "Janis read", ErrorKind.SHEET_READ: "CMI / Wei-He3 read", ErrorKind.VALVE_READ: "Valve read",
}


# ------------------------------------------------------------------ JSON API
def _dump(model: Any) -> Any:
    return None if model is None else model.model_dump(mode="json")


def snapshot_json(snap: Snapshot) -> dict[str, Any]:
    return {
        "updated": snap.updated.isoformat() if snap.updated else None,
        "values": {slot.value: _dump(snap.get(slot)) for slot in Instrument},
        "links": {
            slot.value: {
                "connected": snap.link(slot).connected,
                "last_ok": snap.link(slot).last_ok.isoformat() if snap.link(slot).last_ok else None,
                "last_error": snap.link(slot).last_error,
            }
            for slot in Instrument
        },
    }


@app.get("/api/snapshot")
def api_snapshot() -> dict[str, Any]:
    assert core is not None
    return snapshot_json(core.engine.read())


@app.get("/QueryMPHUBData")
@app.get("/WebService/QueryMPHUBData")
def api_query_mphub() -> dict[str, Any]:
    """Pump readings as JSON for external tools."""
    assert core is not None
    d = core.engine.read().get(Instrument.PUMP_DATA)
    if d is None:
        return {}
    return {
        "Inlet Pressure (bar)": d.inlet_pressure,
        "Tank Pressure (bar)": d.tank_pressure,
        "Outlet Pressure (bar)": d.outlet_pressure,
        "Outlet Tank Level (% Full)": d.outlet_tank_level,
        "Oil Temperature (C)": d.oil_temperature,
        "Helium Flow In (l/min)": d.helium_flow_in,
    }


# ------------------------------------------------------------------ actions
def _inactive(slot: Instrument) -> bool:
    assert core is not None
    return not core.modes.active(SLOT_DEVICE.get(slot, slot.value))


def _refuse_inactive(slot: Instrument, what: str) -> bool:
    if _inactive(slot):
        ui.notify(f"{what} is switched off (Internal tab · Activation status)", type="warning")
        return True
    return False


async def _confirm(title: str, body: str, ok: str = "Write to pump") -> bool:
    with ui.dialog() as dialog, ui.card().classes("hr-card").style("min-width:380px;padding:20px"):
        ui.label(title).classes("text-[19px]")
        ui.label(body).classes("whitespace-pre-line hr-mono text-[14px]")
        with ui.row().classes("w-full justify-end gap-2"):
            theme.button("Cancel", lambda: dialog.submit(False), kind="locked")
            theme.button(ok, lambda: dialog.submit(True), kind="red")
    result = await dialog
    dialog.delete()
    return bool(result)


async def _pump_write(title: str, settings: PumpSettings | None, action: Callable[[], Awaitable[Any]]) -> None:
    assert core is not None
    if not core.config.pump_allow_writes:
        ui.notify("Pump writes are disabled (set HR_PUMP_ALLOW_WRITES=1)", type="warning")
        return
    if not await _confirm(title, describe_settings(settings) if settings else ""):
        return
    try:
        readback = await action()
    except (DriverError, RuntimeError) as exc:
        ui.notify(f"{title} failed: {exc}", type="negative", multi_line=True)
        return
    ui.notify(f"{title}: done (pump enabled={readback.enabled})", type="positive")


async def _valve_switch(open_: bool) -> None:
    """ATL3 inlet valve Open/Close buttons: guarded, confirmed, read back."""
    assert core is not None
    action = "Open" if open_ else "Close"
    if _refuse_inactive(Instrument.VALVE, "The ATL3 inlet valve"):
        return
    if core.valve is None:
        ui.notify(f"Valve not available: {core.valve_problem}", type="warning")
        return
    if not core.config.valve_allow_writes:
        ui.notify("Valve switching is disabled (set HR_VALVE_ALLOW_WRITES=1)", type="warning")
        return
    v = core.engine.read().get(Instrument.VALVE)
    now_state = {True: "OPEN", False: "CLOSED"}.get(core.valve.is_open(v.switch_on) if v else None, "unknown")
    if not await _confirm(f"{action} ATL3 inlet valve?", f"Current state: {now_state}", ok=f"{action} valve"):
        return
    try:
        status = await core.valve.set_open(open_)
    except DriverError as exc:
        ui.notify(f"{action} failed: {exc}", type="negative", multi_line=True)
        return
    state = "OPEN" if core.valve.is_open(status.switch_on) else "CLOSED"
    ui.notify(f"ATL3 inlet valve is {state} (status {status.status_number})", type="positive")


async def _atl_set_mode(slot: Instrument, mode: LiquidMode) -> bool:
    """ATL "Mode selection": guarded, confirmed, read back. Returns True when the mode changed."""
    assert core is not None
    model = core.atl.clients[slot].model
    name = model.mode_name(mode)
    unit = slot.value.upper()
    if _refuse_inactive(slot, unit):
        return False
    if not core.config.atl_allow_writes:
        ui.notify("ATL mode changes are disabled (set HR_ATL_ALLOW_WRITES=1)", type="warning")
        return False
    a = core.engine.read().get(slot)
    current = model.mode_text(a.liquid_mode, a.transfer_mode) if a is not None else "unknown"
    if a is not None and int(a.liquid_mode) == mode:
        ui.notify(f"{unit} is already in {name}", type="info")
        return False
    host = core.atl.clients[slot].host
    left = core.atl.cooldown_left(slot)
    if mode != LiquidMode.STOP and left > 0:
        ui.notify(f"{unit}: compressor still cooling after the stop, wait {left:.0f} s", type="warning")
        return False
    note = (f"\n\nThe compressor then needs {COMPRESSOR_COOLDOWN_S:.0f} s to cool down:\n"
            "no mode can be started on this unit until then." if mode == LiquidMode.STOP else "")
    if not await _confirm(f"Set {unit} to {name}?",
                          f"Current mode: {current}\nNew mode:     {name}\n\n"
                          f"Sent to {host} as on its own web page, then read back.{note}", ok=f"Set {name}"):
        return False
    try:
        status = await core.atl.set_mode(slot, mode)
    except DriverError as exc:
        ui.notify(f"{unit} mode change failed: {exc}", type="negative", multi_line=True)
        return False
    ui.notify(f"{unit} is now in {model.mode_text(status.liquid_mode, status.transfer_mode)}", type="positive")
    return True


def _mode_selector(slot: Instrument, model: ATLModel, writes: bool) -> dict[LiquidMode | str, ui.element]:
    """Two-stage control: [Mode selection] -> one button per mode the model allows
    (ATL80: Stop / Recovery / Slow / Medium / Fast; ATL3: Stop / Liquefy). Returns the
    mode buttons, plus "cooling": the label shown while the compressor cools after a stop."""
    segs: dict[LiquidMode | str, ui.element] = {}
    with ui.column().classes("hr-valve gap-2"):
        stage1 = ui.row().classes("w-full items-center justify-between no-wrap")
        stage2 = ui.column().classes("w-full gap-2")
        stage2.set_visibility(False)

        def open_(on: bool) -> None:
            if on and not writes:
                ui.notify("ATL mode changes are disabled (set HR_ATL_ALLOW_WRITES=1)", type="warning")
                return
            stage1.set_visibility(not on)
            stage2.set_visibility(on)

        with stage1:
            ui.label("Liquefaction mode").classes("text-[15px] font-medium")
            theme.button("Mode selection", lambda: open_(True), kind="" if writes else "locked",
                         icon=None if writes else "lock_outline")
        with stage2:
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                busy = ui.label("Select the new mode").classes("text-[14px] font-medium")
                ui.label("Cancel").classes("hr-accent text-[13px] cursor-pointer").on("click", lambda: open_(False))

            async def choose(mode: LiquidMode) -> None:
                busy.set_text(f"Setting {model.mode_name(mode)}… (waiting for read-back)")
                try:
                    if await _atl_set_mode(slot, mode):
                        open_(False)
                finally:
                    busy.set_text("Select the new mode")

            with ui.row().classes("w-full gap-2"):  # wraps when 5 buttons do not fit
                for m in model.settable:
                    segs[m] = ui.element("div").classes("hr-seg" + (" stop" if m == LiquidMode.STOP else "")).on(
                        "click", lambda m=m: choose(m))
                    with segs[m]:
                        ui.label(model.mode_name(m))
        segs["cooling"] = ui.label("").classes("text-[13px] font-medium").style("color:var(--hr-warn)")
        segs["cooling"].set_visibility(False)
    return segs


# ------------------------------------------------------------------ building blocks
def _alert_marker() -> ui.label:
    """Hidden marker at the top of a Graphic View card; `_show_alerts` fills it."""
    m = ui.label("").classes("hr-alert-mark")
    m.set_visibility(False)
    return m


def _gv_target(rule) -> str | Instrument | None:
    """Graphic View card a rule belongs to (gauge key or vessel slot); None = banner only."""
    return GV_GAUGE_RULES.get(rule.key) or GV_DEVICE_SLOTS.get(rule.device)


def _show_alerts(marker: ui.label, alerts: list[tuple[str, str, bool]]) -> None:
    """alerts: (severity, text, confirmed). Red if any critical, else yellow; dashed while
    the condition still waits for its debounce (not yet sent to Slack)."""
    card_el = marker.parent_slot.parent if marker.parent_slot else None
    if card_el is not None:
        card_el.classes(remove="alert-warn alert-crit")
    if not alerts:
        marker.set_visibility(False)
        return
    crit = any(sev == CRITICAL for sev, _, _ in alerts)
    confirmed = any(ok for _, _, ok in alerts)
    marker.set_text(" · ".join(("▲ " if sev == CRITICAL else "● ") + text + ("" if ok else " (confirming)")
                               for sev, text, ok in alerts))
    marker.classes(remove="warn crit pending", add=("crit" if crit else "warn") + ("" if confirmed else " pending"))
    marker.set_visibility(True)
    if card_el is not None:
        card_el.classes(add="alert-crit" if crit else "alert-warn")


def _short_error(msg: str | None, n: int = 60) -> str:
    """Last error shortened for a card line (the badge tooltip has the full text)."""
    if not msg:
        return "no reading yet"
    return msg if len(msg) <= n else msg[: n - 1].rstrip() + "…"


def _line_chart(series: list[tuple[str, str]], unit: str, height: str = "h-64", y: dict | None = None,
                dashed: tuple[int, ...] = (), colors: list[str] | None = None) -> ui.echart:
    return ui.echart(
        {
            "animation": False,
            "color": colors or SERIES_COLORS,
            "textStyle": {"fontFamily": "Inter"},
            "tooltip": {"trigger": "axis"},
            "legend": {"data": [t for _, t in series], "left": 0, "top": 0, "icon": "rect",
                       "itemWidth": 18, "itemHeight": 3, "textStyle": {"color": "#617387", "fontSize": 13}},
            "xAxis": {"type": "time", "axisLabel": {"hideOverlap": True, "color": "#617387",
                                                    "fontFamily": "Roboto Mono", "fontSize": 11},
                      "axisLine": {"lineStyle": {"color": "#d3dce5"}}, "splitLine": {"show": False}},
            "yAxis": {"type": "value", "scale": True,
                      "axisLabel": {"color": "#617387", "fontFamily": "Roboto Mono", "fontSize": 11},
                      "splitLine": {"lineStyle": {"color": "#e3e9ef"}}, **(y or {})},
            "series": [{"name": t, "type": "line", "showSymbol": False,
                        "lineStyle": {"width": 2, "type": "dashed" if i in dashed else "solid"}, "data": []}
                       for i, (_, t) in enumerate(series)],
            "grid": {"left": 48, "right": 16, "top": 40, "bottom": 28},
        }
    ).classes(f"w-full {height}")


def _gauge(maximum: float) -> ui.echart:
    return ui.echart(
        {
            "series": [{
                "type": "gauge", "min": 0, "max": maximum, "startAngle": 180, "endAngle": 0, "radius": "95%",
                "center": ["50%", "80%"], "splitNumber": 1,
                "axisLine": {"lineStyle": {"width": 10, "color": [[1, "#d3dce5"]]}},
                "pointer": {"length": "85%", "width": 3, "itemStyle": {"color": "#2466a2"}},
                "anchor": {"show": True, "size": 8, "itemStyle": {"color": "#2466a2"}},
                "axisTick": {"show": False}, "splitLine": {"show": False},
                "axisLabel": {"distance": -8, "color": "#617387", "fontFamily": "Roboto Mono", "fontSize": 10},
                "detail": {"show": False},
                "data": [{"value": 0}],
            }]
        }
    ).classes("w-full h-32")


def _settings_table(editable: bool, initial: PumpSettings | None = None, registers: bool = True) -> dict[str, Any]:
    """Register table with either read-back labels or editable inputs."""
    cells: dict[str, Any] = {}
    heads = [("FIELD", "auto")] + ([("REGISTER", "17%")] if registers else []) + [
        ("UNIT", "13%"), ("TO SET" if editable else "READ-BACK", "26%")]
    with ui.element("table").classes("hr-reg"):
        with ui.element("tr"):
            for h, w in heads:
                with ui.element("th").style(f"width:{w}"):
                    ui.label(h)
        for field, label, reg, unit in REGISTERS:
            with ui.element("tr").classes("gain" if field in GAINS and registers else ""):
                with ui.element("td"):
                    ui.label(label)
                if registers:
                    with ui.element("td").classes("reg"):
                        ui.label(reg)
                with ui.element("td").classes("unit"):
                    ui.label(unit)
                with ui.element("td").classes("v"):
                    if not editable:
                        cells[field] = ui.label("—")
                    elif field == "enabled":
                        cells[field] = ui.select({True: "True", False: "False"},
                                                 value=initial.enabled if initial else True).props(
                            "outlined dense options-dense").classes("hr-input w-full")
                    else:
                        cells[field] = ui.number(value=getattr(initial, field) if initial else 0, min=0,
                                                 max=32767, step=1, format="%.0f").props(
                            "outlined dense").classes("hr-input w-full")
    return cells


def _compare_table(initial: PumpSettings) -> tuple[dict[str, ui.label], dict[str, Any], dict[str, ui.element]]:
    """One table: current PLC read-back next to the editable values to set.

    Returns (read-back labels, editor inputs, rows) by field.
    """
    read: dict[str, ui.label] = {}
    edit: dict[str, Any] = {}
    rows: dict[str, ui.element] = {}
    with ui.element("table").classes("hr-reg"):
        with ui.element("tr"):
            for h, w in (("FIELD", "auto"), ("UNIT", "11%"), ("CURRENT PLC", "22%"), ("TO SET", "26%")):
                with ui.element("th").style(f"width:{w}"):
                    ui.label(h)
        for field, label, reg, unit in REGISTERS:
            with ui.element("tr").props(f'title="{reg}"') as row:
                rows[field] = row
                with ui.element("td"):
                    ui.label(label)
                with ui.element("td").classes("unit"):
                    ui.label(unit)
                with ui.element("td").classes("v"):
                    read[field] = ui.label("—")
                with ui.element("td").classes("v"):
                    if field == "enabled":
                        edit[field] = ui.select({True: "True", False: "False"}, value=initial.enabled).props(
                            "outlined dense options-dense").classes("hr-input w-full")
                    else:
                        edit[field] = ui.number(value=getattr(initial, field), min=0, max=32767, step=1,
                                                format="%.0f").props("outlined dense").classes("hr-input w-full")
    return read, edit, rows


def _read_editor(cells: dict[str, Any]) -> PumpSettings:
    return PumpSettings(enabled=bool(cells["enabled"].value),
                        **{f: float(cells[f].value or 0) for f in cells if f != "enabled"})


def _fill_editor(cells: dict[str, Any], s: PumpSettings) -> None:
    for f, el in cells.items():
        el.value = s.enabled if f == "enabled" else getattr(s, f)


def _monitor(name: str) -> dict[str, Any]:
    """Compact cryostat monitor (Janis / CMI / Wei-He3), red when not live."""
    out: dict[str, Any] = {}
    out["box"] = ui.column().classes("hr-monitor w-full")
    with out["box"]:
        out["title"] = ui.label(name).classes("t")
        out["line"] = ui.label("—").classes("l").style("font-size:14px")
        out["updated"] = ui.label("").classes("l").style("font-size:12px")
    return out


def _update_monitor(out: dict, name: str, snap: Snapshot, slot: Instrument) -> None:
    link, v = snap.link(slot), snap.get(slot)
    ok = link.connected
    out["box"].classes(remove="ok", add="ok" if ok else "")
    out["title"].set_text(f"{name} · {'live' if ok else 'error / stale'}")
    out["title"].style(f"color:{'var(--hr-ok)' if ok else 'var(--hr-err)'}")
    if v is not None:
        if slot is Instrument.JANIS:
            out["line"].set_text(f"LHe Level {_fmt(v.lhe_level_in, 1)} in · LHe Volume {_fmt(v.lhe_volume_l, 2)} L")
            t = v.last_update
        else:
            out["line"].set_text(f"He Level {_fmt(v.level, 1)} {v.level_unit} · He Volume {_fmt(v.volume_l, 2)} L")
            t = v.source_time or v.last_update
        out["updated"].set_text(f"Updated {_fmt(t)}" + ("" if ok else f" · {_short_error(link.last_error, 44)}"))
    elif not ok:
        out["updated"].set_text(_short_error(link.last_error))


def _cryo_values(slot: Instrument, v: Any) -> tuple[float | None, float | None, datetime | None]:
    """(level, volume L, time of reading) for a Janis or sheet cryostat reading."""
    if slot is Instrument.JANIS:
        return v.lhe_level_in, v.lhe_volume_l, v.last_update
    return v.level, v.volume_l, v.source_time or v.last_update


def _set_link_badge(badge: Badge, snap: Snapshot, slot: Instrument) -> None:
    if _inactive(slot):
        badge.set("inactive", "idle", INACTIVE_TIP)
        return
    link = snap.link(slot)
    text, kind = link_state(link.connected, link.last_error)
    badge.set(text, kind, link.last_error)


def _fill(el: ui.element, frac: float | None) -> None:
    f = 0.0 if frac is None or frac != frac else max(0.0, min(1.0, frac))
    el.fill.style(f"height:{f * 100:.0f}%")


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.2f} s"


def _interval_of(target: Any, attr: str) -> float:
    return target[attr].interval if isinstance(target, dict) else getattr(target, attr)


# ------------------------------------------------------------------ page
@ui.page("/")
def index() -> None:
    assert core is not None
    c = core
    cfg = c.config
    theme.install()
    ui.page_title("Helium Recovery")

    # ---------------- header ----------------
    with ui.element("div").classes("hr-header"):
        with ui.row().classes("items-center gap-[14px] no-wrap"):
            ui.label("He").classes("hr-logo")
            ui.label("Helium Recovery").classes("text-[24px]")
            ui.label("OPERATOR CONSOLE").classes("text-[13px] hr-muted").style("letter-spacing:.04em")
        with ui.row().classes("items-center gap-4 no-wrap"):
            mode_ind = ui.element("div").classes("hr-mode").on("click", lambda: choose_mode())
            theme.set_title(mode_ind, "Operation mode · click to change")
            with mode_ind:
                mode_icon = ui.icon("visibility").classes("text-[18px]")
                mode_text = ui.label("Observer")
                ui.icon("expand_more").classes("text-[16px]").style("opacity:.6")
            clock = ui.label().classes("hr-mono text-[16px]")

    async def choose_mode() -> None:
        cur = c.modes.mode
        with ui.dialog() as d, ui.card().classes("hr-card").style("width:min(580px,95vw);padding:20px"):
            ui.label("Operation mode").classes("text-[19px]")
            for m in OperationMode:
                icon, desc = MODE_INFO[m]
                opt = ui.row().classes("hr-mode-opt w-full no-wrap items-start gap-3" + (" sel" if m == cur else ""))
                opt.on("click", lambda m=m: d.submit(m))
                with opt:
                    ui.icon(icon).classes(f"text-2xl hr-mode-ico {m.value}")
                    with ui.column().classes("gap-0"):
                        ui.label(m.title + ("  · current" if m == cur else "")).classes("text-[16px] font-semibold")
                        ui.label(desc).classes("hr-desc")
            with ui.row().classes("w-full justify-end"):
                theme.button("Cancel", lambda: d.submit(None), kind="locked")
        new = await d
        d.delete()
        if new is None or new == cur:
            return
        if cur == OperationMode.TRANSFER and (tr.auto_enable_armed or tr.dialog_open):
            if not await _confirm("Leave Transfer mode?", "A transfer step is still in progress (Step 1 waiting or "
                                  "a transfer dialog open).\nATL \"not responding\" alerts start again.",
                                  ok=f"Switch to {new.title}"):
                return
        if new == OperationMode.AGENT and not await _confirm(
                "Switch to Agent mode?", "The program will be allowed to change settings by itself.\n"
                "(No automatic actions are implemented yet.)\nAgent mode ends when the program restarts.",
                ok="Agent mode"):
            return
        c.modes.set(new, "set by hand from the header")
        ui.notify(f"Operation mode: {new.title}", type="info")
        paint_mode()

    def paint_mode() -> None:
        m = c.modes.mode
        if mode_shown.get("m") == m:
            return
        mode_shown["m"] = m
        mode_ind.classes(remove=" ".join(x.value for x in OperationMode), add=m.value)
        mode_icon.props(f"name={MODE_INFO[m][0]}")
        mode_text.set_text(m.title)

    mode_shown: dict[str, Any] = {}
    grey: dict[str, list[ui.element]] = {d: [] for d in DEVICES}  # elements greyed out when a device is off

    # ---------------- navigation ----------------
    pages = [("table", "Table View"), ("graphic", "Graphic View"), ("diagram", "Diagram View"),
             ("transfer", "Transfer Control"), ("bp", "BP Valve Tuning"), ("status", "Status")]
    tab_els: dict[str, ui.element] = {}

    def show(name: str) -> None:
        panels.set_value(name)
        for n, el in tab_els.items():
            el.classes(remove="active", add="active" if n == name else "")
        internal_link.classes(remove="active", add="active" if name == "internal" else "")

    with ui.element("div").classes("hr-nav"):
        with ui.row().classes("gap-[10px] no-wrap items-stretch"):
            for name, label in pages:
                tab_els[name] = ui.element("div").classes("hr-tab" + (" active" if name == "table" else "")).on(
                    "click", lambda n=name: show(n))
                with tab_els[name]:
                    ui.label(label)
        internal_link = ui.row().classes("hr-internal-link no-wrap").on("click", lambda: show("internal"))
        with internal_link:
            ui.icon("lock_outline").classes("text-base")
            ui.label("Internal · Maintenance only")

    def write_notice() -> None:
        if cfg.pump_allow_writes:
            notice("Pump writes enabled", "HR_PUMP_ALLOW_WRITES=1 · Confirmation · PLC read-back", kind="ok")
        else:
            notice("Pump writes locked", "HR_PUMP_ALLOW_WRITES=1 · Confirmation · PLC read-back")

    def monitors(right: ui.row) -> dict:
        out = {}
        with right:
            for slot, name in CRYO:
                with ui.element("div").style("width:330px;max-width:100%"):
                    out[slot] = (_monitor(name), name)
        return out

    tr = c.transfer
    lock = None if cfg.pump_allow_writes else "lock_outline"
    wkind = "" if cfg.pump_allow_writes else "locked"
    panels = ui.tab_panels(value="table", animated=False).classes("w-full").style("background:transparent")
    with panels:
        # ======================= Table View =======================
        with ui.tab_panel("table").classes("hr-screen"):
            page_heading("Table View")
            with card("MP Hub pump", classes="w-full") as pump_badge:
                grey["pump"].append(pump_badge.card)
                with ui.grid(columns=6).classes("w-full gap-3"):
                    pump_tiles = {
                        "inlet_pressure": tile("Inlet Pressure", "bar"),
                        "tank_pressure": tile("Tank Pressure", "bar"),
                        "outlet_pressure": tile("Outlet Pressure", "bar"),
                        "outlet_tank_level": tile("Tank Level", "%"),
                        "oil_temperature": tile("Oil Temperature", "°C"),
                        "helium_flow_in": tile("Helium Flow", "l/min"),
                    }
                with ui.row().classes("w-full justify-between no-wrap hr-mono text-[13px]"):
                    ui.label("Source: PUMP_DATA").props(f'title="MP Hub PLC {cfg.pump_host}:{cfg.pump_port}"')
                    with ui.row().classes("gap-6 no-wrap"):
                        ui.label("Last Update Time")
                        pump_time = ui.label("—")

            atl_rows = [("diode_temp", "Diode Temp", "K"), ("liquid_mode", "Liq. Mode", ""),
                        ("dewar_pressure", "Dewar Pres", "psi"), ("input_gas_flow", "Gas Flow in", "l/min"),
                        ("liquid_volume", "Liq. Vol", "L"), ("liquefaction_rate", "Liq. Rate", "L/day"),
                        ("heater_power", "Heater Power", "%")]
            atl_badges, atl_labels, atl_times, atl_modes = {}, {}, {}, {}
            shown_mode: dict[Instrument, tuple] = {}
            cryo_badges, cryo_tiles, cryo_times, cryo_err = {}, {}, {}, {}
            with ui.grid(columns=4).classes("w-full gap-4 items-stretch").style(
                    "grid-template-columns:repeat(3,minmax(0,1fr)) minmax(0,1.05fr)"):
                for i, slot in enumerate((Instrument.ATL1, Instrument.ATL2, Instrument.ATL3)):
                    host = cfg.atl_hosts[i] if i < len(cfg.atl_hosts) else "?"
                    with card(slot.value.upper()) as b:
                        atl_badges[slot] = b
                        grey[slot.value].append(b.card)
                        b.el.props(f'title="HTTP {host}:{cfg.atl_port}"')
                        atl_labels[slot] = reading_rows(atl_rows)
                        with ui.row().classes("w-full justify-between no-wrap text-[13px]"):
                            ui.label("Last Update Time").classes("hr-muted")
                            atl_times[slot] = ui.label("—").classes("hr-mono")
                        atl_modes[slot] = _mode_selector(slot, c.atl.clients[slot].model, cfg.atl_allow_writes)
                        if slot is Instrument.ATL3:
                            with ui.column().classes("hr-valve gap-2").props(
                                    'title="NEXGEN smart valve (Tuya)"') as valve_box:
                                grey["valve"].append(valve_box)
                                with ui.row().classes("items-center gap-x-3 gap-y-1"):
                                    ui.label("Inlet valve").classes("text-[15px] font-medium")
                                    valve_badge = Badge("—", "idle")
                                    ui.label("status").classes("hr-muted text-[13px]")
                                    valve_number = ui.label("—").classes("hr-mono text-[22px] font-medium")
                                with ui.row().classes("gap-2 no-wrap"):
                                    theme.button("Open", lambda: _valve_switch(True), kind="green")
                                    theme.button("Close", lambda: _valve_switch(False), kind="red")
                with ui.column().classes("gap-4"):
                    for slot, name in CRYO:
                        with card(f"{name} LHe" if slot is Instrument.JANIS else name, classes="w-full grow") as b:
                            cryo_badges[slot] = b
                            grey[SLOT_DEVICE[slot]].append(b.card)
                            with ui.grid(columns=2).classes("w-full gap-3"):
                                if slot is Instrument.WEI:  # its sheet logs litres only
                                    ui.element("div")
                                    cryo_tiles[slot] = (None, tile("LHe Volume", "L"))
                                else:
                                    cryo_tiles[slot] = (tile("LHe Level", "in"), tile("LHe Volume", "L"))
                            with ui.row().classes("w-full justify-between no-wrap text-[13px]"):
                                src = "JANIS" if slot is Instrument.JANIS else name
                                ui.label(f"{src} • Last Update Time").classes("hr-muted")
                                cryo_times[slot] = ui.label("—").classes("hr-mono")
                            cryo_err[slot] = ui.label("").classes("hr-err text-[13px]")

        # ======================= Graphic View =======================
        with ui.tab_panel("graphic").classes("hr-screen"):
            page_heading("Graphic View")
            gv_banner = ui.row().classes("w-full items-start gap-2 no-wrap hr-alert-banner")
            with gv_banner:
                gv_banner_icon = ui.icon("warning").classes("text-xl shrink-0")
                gv_banner_text = ui.label("").classes("grow")
            gv_banner.set_visibility(False)
            gv_marks: dict[str | Instrument, ui.label] = {}
            inlet_series = [("inlet_pressure", "Inlet"), ("inlet_setpoint", "Setpoint")]
            tank_series = [("tank_pressure", "Tank")]
            gv_atl, gv_gauge, gv_cryo = {}, {}, {}
            gv_shown: dict[str, Any] = {}
            with ui.row().classes("w-full gap-6 items-stretch"):
                with ui.grid(columns=3).classes("gap-4 hr-graphic-left"):
                    for slot in (Instrument.ATL1, Instrument.ATL2, Instrument.ATL3):
                        with card(slot.value.upper()) as b:
                            gv_marks[slot] = _alert_marker()
                            with ui.row().classes("w-full gap-3 no-wrap items-start"):
                                ves = vessel().props(f'title="0–{ATL_SCALE_L[slot]:g} L"')
                                with ui.column().classes("gap-0"):
                                    vals = {}
                                    for key, lab, unit in (("liquid_volume", "Liq. Vol", "L"),
                                                           ("dewar_pressure", "Pres.", "psi"),
                                                           ("input_gas_flow", "Gas Flow", "L/min")):
                                        ui.label(lab).classes("hr-muted").style("font-size:12px")
                                        vals[key] = big_value(unit)
                        gv_atl[slot] = (b, ves, vals)
                        grey[slot.value].append(b.card)
                    for key, title in (("inlet_pressure", "Inlet pressure"), ("tank_pressure", "Tank pressure"),
                                       ("outlet_pressure", "Outlet pressure")):
                        with card(title, badge=False):
                            gv_marks[key] = _alert_marker()
                            g = _gauge(GAUGE_MAX_BAR[key])
                            gv_gauge[key] = (g, big_value("bar"))
                    for slot, name in CRYO:
                        with card(name) as b:
                            gv_marks[slot] = _alert_marker()
                            top, unit = CRYO_SCALE[slot]
                            with ui.row().classes("w-full gap-4 no-wrap items-start"):
                                ves = vessel(small=True).props(f'title="0–{top:g} {unit}"')
                                with ui.column().classes("gap-0"):
                                    ui.label("LHe Level" if unit == "in" else "Liq. Vol").classes(
                                        "hr-muted").style("font-size:11px")
                                    vol = big_value(unit)
                            note = ui.label("").classes("text-[13px] font-medium")
                        gv_cryo[slot] = (b, ves, vol, note)
                        grey[SLOT_DEVICE[slot]].append(b.card)
                with ui.column().classes("gap-4 hr-graphic-right"):
                    with card("Inlet pressure & setpoint", badge=False, classes="w-full grow"):
                        inlet_chart = _line_chart(inlet_series, "bar", "h-[300px]", dashed=(1,),
                                                  colors=["#2466a2", "#6e6bb8"])
                    with card("Tank pressure", badge=False, classes="w-full grow"):
                        tank_chart = _line_chart(tank_series, "bar", "h-[300px]", colors=["#238a8a"])

        # ======================= Diagram View =======================
        with ui.tab_panel("diagram").classes("hr-screen"):
            page_heading("Diagram View")
            diagram = Diagram()
            for slot, out in diagram.atl.items():
                grey[slot.value].append(out["panel"])
            grey["janis"].append(diagram.janis["box"])
            grey["cmi"].append(diagram.cmi["box"])

        # ======================= Transfer Control =======================
        with ui.tab_panel("transfer").classes("hr-screen"):
            page_heading("Transfer Control")
            tc_shown_diff: dict[str, bool] = {}
            with ui.row().classes("w-full gap-4 items-stretch"):
                with card("Pump settings", classes="hr-col wide2") as tc_read_badge:
                    tc_read_time = ui.label("PLC read-back updated —").classes("hr-desc")
                    tc_read, tc_cells, tc_rows = _compare_table(tr.to_set)
                    ui.element("div").classes("grow")
                    with ui.row().classes("w-full items-center justify-between no-wrap").style(
                            "border-top:1px solid var(--hr-line);padding-top:10px"):
                        tc_diff = ui.label("").classes("hr-desc")
                        theme.button("SET", lambda: run_step("Set pump settings", sync_to_set(), tr.set_pars),
                                     kind=wkind, icon=lock)
                with card("Transfer sequence", classes="hr-col wide") as seq_badge:
                    seq_badge.set("Idle", "idle")

                    def show_preset(i: int) -> None:  # a step wrote preset i: show it in the editor
                        _fill_editor(tc_cells, tr.presets[i])

                    def sync_to_set() -> PumpSettings:
                        tr.to_set = _read_editor(tc_cells)
                        return tr.to_set

                    async def run_step(title: str, settings: PumpSettings | None,
                                       action: Callable[[], Awaitable[Any]]) -> None:
                        seq_badge.set(f"Writing · {title}", "warn")
                        try:
                            await _pump_write(title, settings, action)
                        finally:
                            seq_badge.set("Idle", "idle")

                    def open_step(dialog: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
                        async def go() -> None:
                            if not cfg.pump_allow_writes:
                                ui.notify("Pump writes are disabled (set HR_PUMP_ALLOW_WRITES=1)", type="warning")
                                return
                            await dialog()
                        return go

                    T = transfer_dialogs.TEXT
                    for key, dialog in (("step1", lambda: transfer_dialogs.step1(c, show_preset)),
                                        ("step2", lambda: transfer_dialogs.step2(c)),
                                        ("step3", lambda: transfer_dialogs.step3(c, show_preset))):
                        theme.button(T[f"{key}_button"], open_step(dialog), kind=wkind, icon=lock)
                        ui.label(T[f"{key}_desc"]).classes("hr-desc")
                    ui.element("div").classes("grow")
                    with ui.row().classes("w-full items-start gap-5 no-wrap"):
                        with ui.column().classes("gap-0"):
                            ui.label("Current inlet P (bar)").classes("hr-note")
                            tc_inlet = ui.label("—").classes("hr-mono text-[38px] font-medium")
                        with ui.column().classes("gap-1"):
                            inlet_alarm = Badge("Inlet P_Overlimit: OFF", "ok", small=True)
                            tank_alarm = Badge("Tank P_Overlimit: OFF", "ok", small=True)
                with ui.column().classes("hr-col gap-4"):
                    tc_charts = []
                    for title, series, color in (("Inlet P Chart", [("inlet_pressure", "Inlet Pressure"),
                                                                    ("inlet_setpoint", "Pressure Setpoint")],
                                                  ["#2466a2", "#6e6bb8"]),
                                                 ("Tank P Chart", [("tank_pressure", "Tank pressure")], ["#238a8a"])):
                        with card(None, badge=False, classes="w-full grow"):
                            with ui.row().classes("w-full justify-between items-baseline no-wrap"):
                                ui.label(title).classes("text-[17px] font-semibold")
                                now_lbl = ui.label("—").classes("hr-mono text-[13px] hr-muted")
                            ch = _line_chart(series, "bar", "h-[260px]", colors=color)
                        tc_charts.append((ch, series, now_lbl))

        # ======================= BP Valve Tuning =======================
        with ui.tab_panel("bp").classes("hr-screen"):
            bp_mon = monitors(page_heading("BP Valve Tuning"))
            for slot, (mon, _) in bp_mon.items():
                grey[SLOT_DEVICE[slot]].append(mon["box"])
            write_notice()
            with ui.row().classes("w-full gap-4 no-wrap items-stretch"):
                with ui.column().classes("grow basis-0 gap-4"):
                    with ui.row().classes("w-full gap-4 no-wrap items-stretch"):
                        with card("Current PLC settings", classes="grow basis-0") as bp_read_badge:
                            bp_read_time = ui.label("Updated —").classes("hr-desc")
                            bp_read = _settings_table(False)
                        with card("Pump Settings to set", classes="grow basis-0") as b:
                            b.set("Editable draft", "info")
                            bp_cells = _settings_table(True, tr.presets[0])
                            ui.label("Inlet limit 0–32767 mbar · Integers").classes("hr-note")
                    with card(None, badge=False, classes="w-full"):
                        with ui.row().classes("w-full items-center justify-between no-wrap"):
                            with ui.column().classes("gap-0"):
                                ui.label("Back-pressure valve gain context").classes("text-[17px]")
                                bp_gain_text = ui.label("P / I / D  ·  — / — / —").classes(
                                    "text-[26px] hr-muted")

                            def copy_current() -> None:
                                s = c.engine.read().get(Instrument.PUMP_SETTINGS)
                                if s is not None:
                                    _fill_editor(bp_cells, s)

                            async def bp_set() -> None:
                                s = _read_editor(bp_cells)
                                await _pump_write("BP Valve Tuning: Set", s, lambda: tr.apply(s))

                            with ui.row().classes("gap-2 no-wrap"):
                                theme.button("Copy current", copy_current)
                                theme.button("Set", bp_set, kind=wkind, icon=lock)
                with card("MI register diagnostics", classes="basis-[33%]") as b:
                    b.set("Live samples", "info")
                    with ui.row().classes("w-full justify-between items-baseline no-wrap"):
                        ui.label("PumpUserData_idx").classes("font-semibold text-[16px]")
                        ui.label(f"Sample every pump read · {cfg.pump_interval * 1000:g} ms").classes("hr-desc")
                    reg_box = ui.column().classes("w-full gap-0")
                    reg_samples: dict[int, ui.label] = {}
                    selected: dict[str, int | None] = {"i": None}

                    def set_reg(i: int, value: Any) -> None:
                        try:
                            reg = int(float(value))
                        except (TypeError, ValueError):
                            return
                        regs = list(c.pump.user_indices)
                        if i < len(regs) and reg != regs[i] and reg not in regs:
                            regs[i] = reg
                            c.pump.user_indices = tuple(regs)
                            render_regs()

                    def select(i: int) -> None:
                        selected["i"] = i
                        render_regs()

                    def render_regs() -> None:
                        reg_box.clear()
                        reg_samples.clear()
                        with reg_box:
                            with ui.row().classes("w-full no-wrap hr-note"):
                                ui.label("INDEX").classes("w-9")
                                ui.label("MI REGISTER NUMBER").classes("grow")
                                ui.label("LAST SAMPLE").classes("w-24 text-right")
                            for i, reg in enumerate(c.pump.user_indices):
                                sel = selected["i"] == i
                                row = ui.row().classes("w-full no-wrap items-center gap-3").style(
                                    "border-bottom:1px solid var(--hr-line);padding:5px 4px;cursor:pointer;" +
                                    ("background:var(--hr-accent-bg)" if sel else ""))
                                row.on("click", lambda i=i: select(i))
                                with row:
                                    ui.label(str(i)).classes("w-9 hr-mono hr-muted")
                                    num = ui.number(value=reg, min=0, max=65535, step=1, format="%.0f").props(
                                        "outlined dense").classes("hr-input grow")
                                    for ev in ("blur", "keydown.enter"):
                                        num.on(ev, lambda _, i=i, n=num: set_reg(i, n.value))
                                    reg_samples[reg] = ui.label("—").classes(
                                        "w-24 text-right hr-mono text-[26px] font-medium")

                    def add_reg() -> None:
                        regs = list(c.pump.user_indices)
                        c.pump.user_indices = (*regs, (max(regs) + 1) if regs else 1900)
                        render_regs()

                    def remove_selected() -> None:
                        i = selected["i"]
                        regs = list(c.pump.user_indices)
                        if i is None or i >= len(regs):
                            ui.notify("Click a register row first", type="warning")
                            return
                        del regs[i]
                        c.pump.user_indices = tuple(regs)
                        selected["i"] = None
                        render_regs()

                    render_regs()
                    with ui.row().classes("gap-2 no-wrap"):
                        theme.button("Add register", add_reg)
                        theme.button("Remove selected", remove_selected)
                    ui.separator()
                    bp_chart_title = ui.label("BPValve_chart").classes("font-semibold text-[14px]")
                    bp_chart = _line_chart([], "raw", "h-56")
                    ui.label("Diagnostics never write to the pump.").classes("hr-note")

        # ======================= Status =======================
        with ui.tab_panel("status").classes("hr-screen"):
            page_heading("Status")
            with card("Data-engine connections", classes="w-full") as b:
                b.set(f"{len(Instrument)} instrument slots", "idle")
                conn = {}
                with ui.grid(columns=len(Instrument)).classes("w-full gap-2"):
                    for slot in Instrument:
                        box = ui.column().classes("gap-1").style(
                            "background:#f6f8fa;border-radius:5px;padding:8px 10px")
                        with box:
                            ui.label(SLOT_NAMES.get(slot, slot.value)).classes(
                                "hr-mono text-[14px] font-semibold w-full truncate")
                            b = Badge(small=True)
                            t = ui.label("—").classes("hr-mono hr-muted text-[11px]")
                            e = ui.label("").classes("hr-err text-[11px]")
                        conn[slot] = (box, b, t, e)
                        grey[SLOT_DEVICE[slot]].append(box)
            with ui.row().classes("w-full gap-4 no-wrap items-stretch"):
                with card("Error List", classes="grow basis-2/3") as err_count:
                    err_box = ui.column().classes("w-full gap-0")
                    ui.label("Consecutive failure counters").classes("font-semibold text-[17px]")
                    counters = {}
                    with ui.grid(columns=len(c.errors.list())).classes("w-full gap-2"):
                        for e in c.errors.list():
                            box = ui.column().classes("gap-0").style(
                                "background:#f6f8fa;border-radius:5px;padding:8px 10px")
                            with box:
                                ui.label(COUNTER_NAMES.get(e.kind, e.kind.value)).classes("text-[13px] hr-muted")
                                n = ui.label("0").classes("hr-mono text-[36px] font-semibold").style(
                                    "line-height:1.2")
                                ui.label("Last failure").classes("text-[12px] hr-muted")
                                t = ui.label("–").classes("hr-mono text-[13px]")
                            counters[e.kind] = (box, n, t)
                with ui.column().classes("grow basis-1/3 gap-4"):
                    with card("Loop status", classes="w-full") as b:
                        b.set("Acquisition loops", "info")
                        with ui.row().classes("gap-2"):
                            loop_atl = Badge("HTTP", "idle")
                            loop_pump = Badge("Pump", "idle")
                            loop_tcp = Badge("TCP", "idle")
                            loop_valve = Badge("Valve", "idle")
                        atl_loop = kv("ATL Loop Status", "—")
                        pump_action = kv("Pump Loop Status · last action", "—")
                    with card("Optional services", badge=False, classes="w-full grow"):
                        services = {}
                        for key, label in (("pump_writes", "pump writes"), ("janis", "Janis"),
                                           ("cmi", "CMI"), ("wei", "Wei-He3"), ("sheets", "Sheets"),
                                           ("status_sheet", "status sheet"), ("valve", "Valve"),
                                           ("valve_writes", "Valve writes"), ("slack", "Slack")):
                            services[key] = kv(label, "—")
                        ui.separator()
                        gdrive_on = kv("Updating GDrive", "—")
                        gdrive_log = kv("G Drive Log Last updated", "—")

        # ======================= Internal (hidden) =======================
        with ui.tab_panel("internal").classes("hr-screen"):
            right = page_heading("Internal")
            with right:
                Badge("Restricted · Hidden from operators", "warn")
            write_notice()
            ui_timer: dict[str, ui.timer] = {}
            interval_inputs: list[tuple[Any, Any, str, float]] = []
            duration_labels: list[tuple[Any, Any]] = []
            alert_cache: dict[str, Any] = {}
            with ui.row().classes("w-full gap-4 no-wrap items-start"):
                with ui.column().classes("grow basis-3/5 gap-4 min-w-0"):
                    with card("Loop intervals", classes="w-full") as b:
                        b.set("Maintenance only", "warn")
                        # (label, target, attr, unit, scale, minimum, source, default in s)
                        loops = [
                            ("Janis Reading Interval", c.janis, "interval", "s", 1, 10,
                             "HR_JANIS_INTERVAL", Config.janis_interval),
                            ("CMI / Wei-He3 Reading Interval", c.cryostats, "interval", "s", 1, 10,
                             "HR_CRYO_INTERVAL", Config.cryo_interval),
                            ("ATL Reading Interval", c.atl, "interval", "s", 1, 2,
                             "HR_ATL_INTERVAL", Config.atl_interval),
                            ("Pump Reading Interval", c.pump, "interval", "ms", 1000, 50,
                             "HR_PUMP_INTERVAL", Config.pump_interval),
                            ("Valve Reading Interval", c.valve, "interval", "s", 1, 5,
                             "HR_VALVE_INTERVAL", Config.valve_interval),
                            ("UI updating Interval", ui_timer, "refresh", "ms", 1000, 100,
                             "ui/app.py", UI_REFRESH_S),
                        ]

                        def set_interval(label: str, target: Any, attr: str, scale: float, minimum: float,
                                         env: str, value: Any) -> None:
                            try:
                                v = float(value)
                            except (TypeError, ValueError):
                                return
                            if abs(v / scale - _interval_of(target, attr)) < 1e-9:
                                return
                            if v < minimum:
                                ui.notify(f"{label}: minimum is {minimum:g}", type="warning")
                                return
                            if isinstance(target, dict):
                                target[attr].interval = v / scale
                            else:
                                setattr(target, attr, v / scale)
                            ui.notify(f"{label} = {v:g} for this session (set {env} in .env to keep it)",
                                      type="info")

                        with ui.element("table").classes("hr-reg"):
                            with ui.element("tr"):
                                for h, w in (("CONTROL", "auto"), ("VALUE / UNIT", "20%"),
                                             ("LAST READ TOOK", "20%"), ("DEFAULT", "12%")):
                                    with ui.element("th").style(f"width:{w}"):
                                        ui.label(h)
                            for label, target, attr, unit, scale, minimum, env, default in loops:
                                with ui.element("tr"):
                                    with ui.element("td"):
                                        ui.label(label).classes("text-[17px]")
                                    with ui.element("td").classes("v"):
                                        if target is None:
                                            ui.label("not running").classes("hr-muted text-[14px]")
                                        else:
                                            num = ui.number(min=minimum, step=1, format="%g", suffix=unit).props(
                                                "outlined dense").classes("hr-input w-36")
                                            for ev in ("blur", "keydown.enter"):
                                                num.on(ev, lambda _, n=num, a=(label, target, attr, scale, minimum,
                                                                              env): set_interval(*a, n.value))
                                            interval_inputs.append((num, target, attr, scale))
                                    with ui.element("td").classes("hr-mono text-[15px]"):
                                        d_lbl = ui.label("—")
                                        if target is not None and not isinstance(target, dict):
                                            theme.set_title(d_lbl, "Time the most recent read took, "
                                                                   "including a failed one")
                                            duration_labels.append((d_lbl, target))
                                    with ui.element("td").classes("hr-mono text-[15px]"):
                                        ui.label(f"{default * scale:g} {unit}")
                        ui.label("Changes apply from the next cycle and last until the service restarts."
                                 ).classes("hr-note")
                    with card(None, badge=False, classes="w-full"):
                        with ui.row().classes("w-full items-center justify-between no-wrap"):
                            ui.label("Most recent readings").classes("hr-card-title")

                            busy = {"on": False}

                            async def refresh_all() -> None:
                                if busy["on"]:
                                    return
                                busy["on"] = True
                                refresh_btn.classes(add="locked")
                                refresh_note.set_text("Reading all instruments…")
                                try:
                                    await c.refresh_all()
                                finally:
                                    busy["on"] = False
                                    refresh_btn.classes(remove="locked")
                                snap = c.engine.read()
                                bad = [SLOT_NAMES.get(sl, sl.value) for sl in Instrument
                                       if not snap.link(sl).connected]
                                refresh_note.set_text(f"Manual refresh at {datetime.now():%H:%M:%S} · " +
                                                      (f"not read: {', '.join(bad)}" if bad else "all read"))
                                ui.notify("Refresh done" + (f" · not read: {', '.join(bad)}" if bad else ""),
                                          type="warning" if bad else "positive")

                            refresh_btn = theme.button("Refresh", refresh_all, icon="refresh")
                        ui.label("Reads every instrument once now (reads only, no writes)").classes("hr-desc")
                        reading_rows_ = {}
                        with ui.element("table").classes("hr-reg"):
                            with ui.element("tr"):
                                for h, w in (("INSTRUMENT", "auto"), ("LAST READING", "24%"), ("AGE", "18%"),
                                             ("STATE", "20%")):
                                    with ui.element("th").style(f"width:{w}"):
                                        ui.label(h)
                            for slot in Instrument:
                                with ui.element("tr"):
                                    with ui.element("td"):
                                        ui.label(SLOT_NAMES.get(slot, slot.value)).classes("hr-mono text-[15px]")
                                    with ui.element("td").classes("hr-mono text-[15px]"):
                                        t_lbl = ui.label("—")
                                    with ui.element("td").classes("hr-mono text-[14px] hr-muted"):
                                        age_lbl = ui.label("—")
                                    with ui.element("td"):
                                        st = Badge(small=True)
                                reading_rows_[slot] = (t_lbl, age_lbl, st)
                        refresh_note = ui.label("").classes("hr-note")
                    amgr = c.alerts
                    with card("Message communication system", classes="w-full") as slack_badge:
                        if amgr.notifier is None:
                            slack_badge.set("Slack not configured · shown here only", "warn")
                        else:
                            slack_badge.set("Slack connected", "ok")
                        ui.label(f"Slack messages for the helium system · managers @mentioned on critical: "
                                 f"{len(amgr.cfg.managers) or 'none set'}").classes("hr-desc")
                        with ui.column().classes("hr-subblock w-full gap-2"):
                            with ui.row().classes("w-full items-baseline justify-between gap-x-3"):
                                ui.label("Activation status").classes("hr-sub-title")
                                ui.label("Off: no warning / critical messages, controls greyed out in every view"
                                         ).classes("hr-note")
                            act_btns: dict[str, ui.element] = {}

                            def toggle_device(d: str) -> None:
                                on = not c.modes.active(d)
                                c.modes.set_active(d, on)
                                ui.notify(f"{DEVICE_NAMES[d]} {'activated' if on else 'switched off: no alerts'}",
                                          type="info")
                                paint_activation()

                            with ui.row().classes("w-full gap-2"):
                                for d in DEVICES:
                                    act_btns[d] = ui.element("div").classes("hr-toggle").on(
                                        "click", lambda d=d: toggle_device(d))
                                    with act_btns[d]:
                                        ui.icon("power_settings_new").classes("text-base")
                                        ui.label(DEVICE_NAMES[d])
                        with ui.column().classes("hr-subblock w-full gap-2"):
                            ui.label("Alerts").classes("hr-sub-title")
                            ui.label(f"Warning / critical alerts and reminders · report {amgr.cfg.report_at:%H:%M}, "
                                     f"forecast to {amgr.cfg.horizon:%H:%M}").classes("hr-desc")
                            mute_lbl = ui.label("").classes("text-[15px] font-medium")
                            with ui.row().classes("w-full gap-2"):
                                def until_morning() -> float:
                                    t = datetime.now().astimezone()
                                    m = t.replace(hour=amgr.cfg.horizon.hour, minute=amgr.cfg.horizon.minute,
                                                  second=0, microsecond=0)
                                    return ((m if m > t else m + timedelta(days=1)) - t).total_seconds()

                                theme.button("Mute 1 h", lambda: amgr.mute(3600), kind="locked", icon="notifications_off")
                                theme.button(f"Mute until {amgr.cfg.horizon:%H:%M}", lambda: amgr.mute(until_morning()),
                                             kind="locked", icon="bedtime")
                                theme.button("Unmute", lambda: amgr.mute(None), kind="locked", icon="notifications")

                                async def preview_report() -> None:
                                    text, danger = amgr.build_report()
                                    with ui.dialog() as d, ui.card().classes("hr-card").style(
                                            "min-width:min(720px,95vw);padding:20px"):
                                        ui.label("10 PM report · preview (not sent)").classes("text-[19px]")
                                        ui.label(text).classes("whitespace-pre-wrap hr-mono text-[13px]")
                                        with ui.row().classes("w-full justify-end"):
                                            theme.button("Close", d.close, kind="locked")
                                    d.open()

                                async def send_report() -> None:
                                    if not await _confirm("Send report now", "Post the nightly report to Slack now?",
                                                          ok="Send"):
                                        return
                                    ok = await amgr.send_report()
                                    ui.notify("Report sent" if ok else "Report not sent (see Recent messages)",
                                              type="positive" if ok else "warning")

                                theme.button("Preview report", preview_report, icon="preview")
                                theme.button("Send report now", send_report, icon="send")
                            ui.label("Active alerts").classes("font-semibold text-[15px]")
                            active_col = ui.column().classes("w-full gap-1")
                            ui.label("Recent messages").classes("font-semibold text-[15px]")
                            recent_col = ui.column().classes("w-full gap-1")
                with ui.column().classes("grow basis-2/5 gap-4 min-w-0"):
                    with card("Service", classes="w-full") as svc_badge:
                        as_service = service.under_nssm()
                        svc_badge.set("Windows service" if as_service else "Not running as the service",
                                      "ok" if as_service else "idle")
                        ui.label(
                            "Restart the HeliumRecovery service, e.g. after a code or .env change: the program "
                            f"stops and Windows starts it again after about {service.RESTART_DELAY_S} s. "
                            "Monitoring and this page pause meanwhile. No pump, valve or ATL settings are written."
                            if as_service else
                            "This program was started by hand (desktop / serve), not by the Windows service, "
                            "so nothing would start it again: restart it where it was started."
                        ).classes("hr-desc")

                        async def restart_service() -> None:
                            tr = c.transfer
                            if tr.auto_enable_armed or tr.dialog_open:
                                ui.notify("A transfer is in progress (Step 1 waiting or a transfer dialog open): "
                                          "finish it before restarting.", type="warning", multi_line=True)
                                return
                            if not await _confirm("Restart service",
                                                  "Stop the program now and let the Windows service start it again?\n"
                                                  f"Monitoring pauses for about {service.RESTART_DELAY_S + 5} s.",
                                                  ok="Restart"):
                                return
                            log.warning("service restart requested from the Internal tab")
                            ui.notify("Restarting… this page reconnects by itself.", type="info")
                            ui.timer(1.0, app.shutdown, once=True)

                        restart_btn = theme.button("Restart service", restart_service if as_service else None,
                                                   icon="restart_alt", kind="" if as_service else "locked")
                        if not as_service:
                            restart_btn.props("disabled")
                    with card(transfer_dialogs.TEXT["sim_title"], classes="w-full") as b:
                        b.set("No pump writes", "idle")
                        ui.label(transfer_dialogs.TEXT["sim_desc"]).classes("hr-desc")
                        T = transfer_dialogs.TEXT
                        with ui.column().classes("w-full gap-3"):
                            for key, dialog in (
                                    ("step1", lambda: transfer_dialogs.step1(c, lambda i: None, sim=True)),
                                    ("step2", lambda: transfer_dialogs.step2(c, sim=True)),
                                    ("step3", lambda: transfer_dialogs.step3(c, lambda i: None, sim=True))):
                                with ui.row().classes("w-full items-center gap-3 no-wrap"):
                                    theme.button(T[f"{key}_button"], dialog, icon="science").classes("shrink-0")
                                    ui.label(T[f"{key}_desc"]).classes("hr-desc")
                    with card("Python configuration", classes="w-full") as b:
                        b.set("Reference only", "idle")
                        ui.label("Python configuration: .env / environment only").classes("text-[15px]")
                        with ui.grid(columns=3).classes("w-full gap-3"):
                            for lbl, val, src in (("Pump read", f"{cfg.pump_interval:g} s", "HR_PUMP_INTERVAL"),
                                                  ("UI refresh", f"{UI_REFRESH_S:g} s", "Python refresh"),
                                                  ("Charts", f"{CHART_REFRESH_S:g} s", "Python chart refresh")):
                                with ui.column().classes("hr-tile gap-0"):
                                    ui.label(lbl).classes("lbl")
                                    ui.label(val).classes("val")
                                    ui.label(src).classes("hr-mono hr-note")

    # ---------------- footer ----------------
    with ui.element("div").classes("hr-footer"):
        ui.label(f"Live · lab PC  /  pump {cfg.pump_host}  ·  ATL {', '.join(cfg.atl_hosts)}")
        footer_right = ui.label("").classes("hr-mono")

    # ---------------- refresh ----------------
    err_shown: dict[str, Any] = {"key": None}
    act_shown: dict[str, Any] = {}

    def paint_activation() -> None:
        off = frozenset(c.modes.inactive)
        if act_shown.get("off") == off:
            return
        act_shown["off"] = off
        for d, els in grey.items():
            for el in els:
                el.classes(remove="hr-inactive", add="hr-inactive" if d in off else "")
        for d, btn in act_btns.items():
            btn.classes(remove="on", add="" if d in off else "on")
            theme.set_title(btn, f"{DEVICE_NAMES[d]}: {'off' if d in off else 'active'} · click to switch "
                                 f"{'on' if d in off else 'off'}")

    def render_errors(snap: Snapshot) -> None:
        entries = list(reversed(c.errors.log()))
        slots_by_source = {s.value: s for s in Instrument}
        slots_by_source.update({"pump": Instrument.PUMP_DATA, "janis": Instrument.JANIS})
        rows = []
        for entry in entries[:12]:
            slot = slots_by_source.get(entry.source)
            rows.append((entry, slot is not None and not snap.link(slot).connected))
        err_count.set(f"{len(entries)} / {c.errors._log.maxlen}", "idle")
        key = tuple((r.time, r.message, a) for r, a in rows)
        if key == err_shown["key"]:
            return
        err_shown["key"] = key
        err_box.clear()
        with err_box:
            with ui.element("table").classes("hr-errtab"):
                with ui.element("tr"):
                    for h, w in (("TIME", "12%"), ("SOURCE", "16%"), ("MESSAGE · NEWEST FIRST", "auto"),
                                 ("STATUS", "11%")):
                        with ui.element("th").style(f"width:{w}"):
                            ui.label(h)
                if not rows:
                    with ui.element("tr"):
                        with ui.element("td").props('colspan="4"'):
                            ui.label("No errors since start").classes("hr-muted")
                for entry, active in rows:
                    with ui.element("tr").classes("active" if active else ""):
                        with ui.element("td").classes("t"):
                            ui.label(entry.time.astimezone().strftime("%H:%M:%S"))
                        with ui.element("td").classes("s"):
                            ui.label(entry.source)
                        with ui.element("td"):
                            ui.label(entry.message[:140])
                        with ui.element("td").classes("st"):
                            ui.label("Active" if active else "Recovered").classes(
                                "hr-err" if active else "hr-ok")

    def refresh() -> None:
        snap = c.engine.read()
        now = datetime.now().astimezone()
        off = now.strftime("%z")
        tz = f"UTC{off[:3]}:{off[3:]}"
        clock.set_text(f"{now:%Y-%m-%d %H:%M:%S} ({tz})")
        paint_mode()
        paint_activation()
        footer_right.set_text(f"{tz}  ·  {'Pump writes enabled' if cfg.pump_allow_writes else 'Read-only'}")

        # Table View
        _set_link_badge(pump_badge, snap, Instrument.PUMP_DATA)
        if (p := snap.get(Instrument.PUMP_DATA)) is not None:
            for k, lbl in pump_tiles.items():
                lbl.set_text(_fmt(getattr(p, k), 1 if k in ("outlet_tank_level", "oil_temperature",
                                                            "helium_flow_in") else 3))
            pump_time.set_text(_fmt(p.last_update))
        for slot, labels in atl_labels.items():
            _set_link_badge(atl_badges[slot], snap, slot)
            if (a := snap.get(slot)) is not None:
                for k, lbl in labels.items():
                    lbl.set_text(liquid_mode_name(slot, a.liquid_mode, a.transfer_mode) if k == "liquid_mode"
                                 else _fmt(getattr(a, k), 2))
                atl_times[slot].set_text(_fmt(a.last_update))
                left = c.atl.cooldown_left(slot)
                if shown_mode.get(slot) != (a.liquid_mode, int(left)):  # highlight mode, cool-down countdown
                    shown_mode[slot] = (a.liquid_mode, int(left))
                    for m, seg in atl_modes[slot].items():
                        if m == "cooling":
                            seg.set_text(f"Compressor cooling · start in {left:.0f} s")
                            seg.set_visibility(left > 0)
                            continue
                        seg.classes(remove="active cooling",
                                    add=("active " if a.liquid_mode == m else "")
                                    + ("cooling" if left > 0 and m != LiquidMode.STOP else ""))
        v = snap.get(Instrument.VALVE)
        is_open = c.valve.is_open(v.switch_on) if (c.valve and v) else None
        vlink = snap.link(Instrument.VALVE)
        if v is None:
            valve_badge.set("—" if not vlink.last_error else "error", "idle" if not vlink.last_error else "err",
                            vlink.last_error)
        else:
            valve_badge.set({True: "OPEN", False: "CLOSED"}[is_open] + ("" if vlink.connected else " · stale"),
                            ("ok" if is_open else "err") if vlink.connected else "warn", vlink.last_error)
            valve_number.set_text(_fmt(v.status_number))
        for slot, _ in CRYO:
            _set_link_badge(cryo_badges[slot], snap, slot)
            link, val = snap.link(slot), snap.get(slot)
            lvl, vol = cryo_tiles[slot]
            stale = "" if link.connected else " · stale"
            for el, lab in ((lvl, "LHe Level"), (vol, "LHe Volume")):
                if el is not None:
                    el.caption.set_text(lab + stale)
            if val is not None:
                level, volume, t = _cryo_values(slot, val)
                if lvl is not None:
                    lvl.set_text(_fmt(level, 1))
                vol.set_text(_fmt(volume, 2))
                cryo_times[slot].set_text(_fmt(t) if slot is Instrument.JANIS else
                                          (f"{t:%m-%d %H:%M} ({_age(val.age())})" if t else "—"))
            cryo_err[slot].set_text("" if link.connected else _short_error(link.last_error))

        # Graphic View
        for slot, (b, ves, vals) in gv_atl.items():
            _set_link_badge(b, snap, slot)
            if (a := snap.get(slot)) is not None:
                vals["liquid_volume"].set_text(_fmt(a.liquid_volume, 1))
                vals["dewar_pressure"].set_text(_fmt(a.dewar_pressure, 2))
                vals["input_gas_flow"].set_text(_fmt(a.input_gas_flow, 1))
                _fill(ves, (a.liquid_volume or 0) / ATL_SCALE_L[slot])
        if p is not None:
            for key, (g, lbl) in gv_gauge.items():
                value = getattr(p, key)
                g.options["series"][0]["data"][0]["value"] = round(value, 3)
                g.update()
                lbl.set_text(_fmt(value, 3))
        for slot, (b, ves, vol, note) in gv_cryo.items():
            link, val = snap.link(slot), snap.get(slot)
            if _inactive(slot):
                b.set("inactive", "idle", INACTIVE_TIP)
            else:
                b.set("live" if link.connected else "error / stale", "ok" if link.connected else "err",
                      link.last_error)
            if val is not None:
                level, volume, t = _cryo_values(slot, val)
                top, unit = CRYO_SCALE[slot]
                shown = level if unit == "in" else volume
                vol.set_text(_fmt(shown, 1 if unit == "in" else 2))
                _fill(ves, (shown or 0) / top)
                note.set_text("" if link.connected else f"Stale · {_fmt(t)}")
                note.style("color:var(--hr-accent)")
            elif not link.connected:
                note.set_text(_short_error(link.last_error, 40))
                note.style("color:var(--hr-err)")

        # Graphic View alert markers (same rules and thresholds as the Slack alerts)
        found = [(r, st, True) for r, st in c.alerts.active()] + [(r, st, False) for r, st in c.alerts.pending()]
        found = [(r, st, ok) for r, st, ok in found if r.severity in (CRITICAL, WARNING)]
        gv_sig = tuple(sorted((r.key, ok, st.detail) for r, st, ok in found))
        if gv_sig != gv_shown.get("sig"):
            gv_shown["sig"] = gv_sig
            per_card: dict[str | Instrument, list[tuple[str, str, bool]]] = {k: [] for k in gv_marks}
            for r, st, ok in found:
                if (target := _gv_target(r)) in per_card:
                    per_card[target].append((r.severity, st.detail or r.title, ok))
            for target, marker in gv_marks.items():
                _show_alerts(marker, per_card[target])
            gv_banner.set_visibility(bool(found))
            if found:
                crit = any(r.severity == CRITICAL for r, _, _ in found)
                gv_banner.classes(remove="warn crit", add="crit" if crit else "warn")
                gv_banner_icon.props(f'name={"error" if crit else "warning"}')
                gv_banner_text.set_text("  ·  ".join(
                    f"{'CRITICAL' if r.severity == CRITICAL else 'WARNING'}: {r.title}"
                    + ("" if ok else " (confirming)") for r, _, ok in
                    sorted(found, key=lambda f: (f[0].severity != CRITICAL, not f[2]))))

        # Diagram
        diagram.update(snap, is_open)

        # Transfer Control / BP Valve Tuning
        s = snap.get(Instrument.PUMP_SETTINGS)
        slink = snap.link(Instrument.PUMP_SETTINGS)
        for badge, cells, time_lbl in ((tc_read_badge, tc_read, tc_read_time), (bp_read_badge, bp_read, bp_read_time)):
            ok_text = "PLC connected" if badge is tc_read_badge else "Read only"
            badge.set(ok_text if slink.connected else "stale", "ok" if slink.connected and badge is tc_read_badge
                      else "idle" if slink.connected else "warn", slink.last_error)
            if s is not None:
                for f, lbl in cells.items():
                    lbl.set_text(("True" if s.enabled else "False") if f == "enabled" else _fmt(getattr(s, f), 0))
                time_lbl.set_text(f"PLC read-back updated {_fmt(slink.last_ok)}")
        if s is not None:  # Transfer Control: rows where the editor differs from the PLC
            changed = []
            for f, row in tc_rows.items():
                plc = s.enabled if f == "enabled" else getattr(s, f)
                new = tc_cells[f].value
                differs = (bool(new) != plc) if f == "enabled" else (new is None or float(new) != plc)
                if differs:
                    changed.append(f)
                if differs != tc_shown_diff.get(f):
                    tc_shown_diff[f] = differs
                    row.style("background:#fff4de" if differs else "background:transparent")
            tc_diff.set_text(f"{len(changed)} value(s) differ from the PLC" if changed
                             else "Editor matches the PLC")
            bp_gain_text.set_text(f"P / I / D  ·  {s.bp_valve_p_gain:g} / {s.bp_valve_i_gain:g} / "
                                  f"{s.bp_valve_d_gain:g}")
        for mons in (bp_mon,):
            for slot, (mon, name) in mons.items():
                _update_monitor(mon, name, snap, slot)
        al = tr.alarms
        inlet_alarm.set(f"Inlet P_Overlimit: {'ON' if al.inlet_overlimit else 'OFF'} "
                        f"≥{tr.limits.inlet_overlimit_bar} bar", "err" if al.inlet_overlimit else "ok")
        tank_alarm.set(f"Tank P_Overlimit: {'ON' if al.tank_overlimit else 'OFF'} "
                       f"≥{tr.limits.tank_overlimit_bar} bar", "err" if al.tank_overlimit else "ok")
        if not seq_badge.label.text.startswith("Writing"):  # Step 1 armed: waiting for the inlet jump
            if tr.auto_enable_armed:
                seq_badge.set(f"Armed · pump on at inlet > {tr.limits.auto_enable_inlet_bar} bar", "warn")
            else:
                seq_badge.set("Idle", "idle")
        if p is not None:
            tc_inlet.set_text(_fmt(p.inlet_pressure, 3))
            sampled = {d.index: d.value for d in p.user_data}
            for reg, lbl in reg_samples.items():
                if reg in sampled:
                    lbl.set_text(f"{sampled[reg]:g}")

        # Status
        for slot, (box, b, t, e) in conn.items():
            link = snap.link(slot)
            text, kind = link_state(link.connected, link.last_error)
            b.set(*(("inactive", "idle", INACTIVE_TIP) if _inactive(slot) else (text, kind, link.last_error)))
            t.set_text((_fmt(link.last_ok) + ("" if link.connected else " · stale")) if link.last_ok else "—")
            e.set_text("" if link.connected or not link.last_error else _short_error(link.last_error, 40))
            box.style(f"background:{'#f6f8fa' if link.connected or not link.last_error else '#fceeee'}")
        for e in c.errors.list():
            box, n, t = counters[e.kind]
            n.set_text(str(e.count))
            t.set_text(_fmt(e.time) if e.time else "–")
            n.style(f"color:{'var(--hr-err)' if e.count else 'var(--hr-text)'}")
            box.style(f"background:{'#fceeee' if e.count else '#f6f8fa'}")
        render_errors(snap)
        alert_sig = (tuple((r.key, st.detail) for r, st in amgr.active()), tuple(t for t, *_ in amgr.recent),
                     amgr.muted)
        if alert_sig != alert_cache.get("sig"):
            alert_cache["sig"] = alert_sig
            mute_lbl.set_text(f"🔕 Muted until {datetime.fromtimestamp(amgr.mute_until):%a %H:%M}" if amgr.muted else "")
            active_col.clear()
            with active_col:
                rows = amgr.active()
                if not rows:
                    ui.label("none").classes("hr-muted text-[14px]")
                for r, st in rows:
                    since = datetime.fromtimestamp(st.started_at or 0)
                    ui.label(f"{r.severity.upper()} · {r.title} · {st.detail} · since {since:%m-%d %H:%M}"
                             ).classes("text-[14px]").style(
                        f"color:{'var(--hr-err)' if r.severity == 'critical' else 'var(--hr-text)'}")
            recent_col.clear()
            with recent_col:
                if not amgr.recent:
                    ui.label("none yet").classes("hr-muted text-[14px]")
                for i, (t, status, text) in enumerate(list(amgr.recent)[-8:][::-1]):
                    if i:  # divider between message threads
                        ui.label("------------").classes("hr-mono text-[12px] hr-muted")
                    ui.label(f"{datetime.fromtimestamp(t):%m-%d %H:%M} · {status}\n{text}").classes(
                        "whitespace-pre-wrap hr-mono text-[12px]")
        for d_lbl, target in duration_labels:
            d_lbl.set_text(_duration(getattr(target, "last_duration", None)))
        for slot, (t_lbl, age_lbl, st) in reading_rows_.items():
            link = snap.link(slot)
            t_lbl.set_text(f"{link.last_ok.astimezone():%m-%d %H:%M:%S}" if link.last_ok else "never")
            age_lbl.set_text(_age(now - link.last_ok) if link.last_ok else "—")
            text, kind = link_state(link.connected, link.last_error)
            st.set(text, kind, link.last_error)
        atl_ok = any(snap.link(sl).connected for sl in (Instrument.ATL1, Instrument.ATL2, Instrument.ATL3))
        loop_atl.set("HTTP connected" if atl_ok else "HTTP down", "ok" if atl_ok else "err")
        pl = snap.link(Instrument.PUMP_DATA)
        loop_pump.set("Pump connected" if pl.connected else "Pump not reading", "ok" if pl.connected else "err")
        loop_tcp.set("TCP connected" if c.pump.connected else "TCP disconnected", "ok" if c.pump.connected else "err")
        vl = snap.link(Instrument.VALVE)
        loop_valve.set("Valve connected" if vl.connected else "Valve not connected", "ok" if vl.connected else "idle")
        atl_loop.set_text(f"polling · every {c.atl.interval:g} s")
        pump_action.set_text("Read pump data" if c.pump.connected else "TCP connect (retrying)")
        feats = c.features()
        for key, lbl in services.items():
            slot = {"janis": Instrument.JANIS, "cmi": Instrument.CMI, "wei": Instrument.WEI}.get(key)
            on = c.cryostats is not None if key in ("cmi", "wei") else feats.get(key, False)
            note = ""
            if not on and key == "pump_writes":
                note = " · disabled"
            elif on and slot is not None and not snap.link(slot).connected:
                note = " · not live"
                theme.set_title(lbl, snap.link(slot).last_error)
            lbl.set_text(f"{on}{note}")
            lbl.style(f"color:{'var(--hr-err)' if note and on else 'var(--hr-text)'}")
        gdrive_on.set_text(str(c.status_sheet is not None))
        if c.status_sheet is not None:
            gdrive_log.set_text(_fmt(c.status_sheet.last_log) if c.status_sheet.last_log else "not yet")

    def _series_data(key: str) -> list:
        return [[t.isoformat(), v] for t, v in c.history.get(key)[-3000:]]

    def _update(chart: ui.echart, series: list[tuple[str, str]], with_value: bool = False) -> None:
        names = []
        for i, (key, title) in enumerate(series):
            data = _series_data(key)
            chart.options["series"][i]["data"] = data
            name = f"{title} · {data[-1][1]:.3f} bar" if with_value and data else title
            chart.options["series"][i]["name"] = name
            names.append(name)
        chart.options["legend"]["data"] = names
        chart.update()

    def refresh_charts() -> None:
        if panels.value == "graphic":
            _update(inlet_chart, inlet_series, with_value=True)
            _update(tank_chart, tank_series, with_value=True)
        elif panels.value == "transfer":
            for ch, series, now_lbl in tc_charts:
                _update(ch, series)
                last = c.history.get(series[0][0])
                now_lbl.set_text(f"{last[-1][1]:.3f} bar" if last else "—")
        elif panels.value == "bp":
            bp_series = [(f"user_{i}", f"Index {k} · MI{i}") for k, i in enumerate(c.pump.user_indices)]
            bp_chart_title.set_text(f"BPValve_chart {len(bp_series)}")
            bp_chart.options["series"] = [{"name": t, "type": "line", "showSymbol": False, "data": []}
                                          for _, t in bp_series]
            _update(bp_chart, bp_series)

    ui_timer["refresh"] = ui.timer(UI_REFRESH_S, refresh)
    ui.timer(CHART_REFRESH_S, refresh_charts)
    panels.on_value_change(lambda _: refresh_charts())
    for num, target, attr, scale in interval_inputs:  # show the running values
        num.value = _interval_of(target, attr) * scale


_core_task: asyncio.Task[None] | None = None


async def _start() -> None:
    global _core_task
    assert core is not None
    _core_task = asyncio.create_task(core.run())


async def _stop() -> None:
    if _core_task is not None:
        _core_task.cancel()
        await asyncio.gather(_core_task, return_exceptions=True)


def serve(the_core: Core, host: str, port: int, show: bool = False,
          native: bool = False, window_size: tuple[int, int] = (1600, 960)) -> None:
    """Run the acquisition core and the web UI until stopped.

    ``native=True`` opens the console in a desktop window (pywebview) instead of
    a browser; closing that window stops the program. The server still listens
    on ``host:port``, so browsers and ``/QueryMPHUBData`` keep working meanwhile.
    """
    global core
    core = the_core
    app.on_startup(_start)
    app.on_shutdown(_stop)
    window = {"native": True, "window_size": window_size} if native else {"show": show}
    ui.run(host=host, port=port, title="Helium Recovery", reload=False, show_welcome_message=False,
           favicon="🧪", **window)
