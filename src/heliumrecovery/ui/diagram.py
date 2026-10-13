"""Diagram View: the Figma "Editable process schematic" (node 19:683) with live values.

Geometry, pipes, arrows and equipment pictures are the Figma assets in
``static/diagram`` placed at their design coordinates on a 1856 x 664 canvas;
the canvas is scaled to the card width in the browser. Value panels are
NiceGUI labels updated from the data engine.
"""

from __future__ import annotations

import re
from pathlib import Path

from nicegui import app, ui

from ..core.engine import Instrument, Snapshot
from .common import fmt, liquid_mode_name
from .theme import set_title

STATIC = Path(__file__).parent / "static"
URL = "/hr-static"
W, H = 1856, 664

# (file, x, y, w, h) straight from the Figma schematic
EQUIPMENT = [
    ("teslatron.png", 1518, 339, 126, 189), ("janis.png", 431, 455, 126, 189),
    ("cmi.png", 203, 330, 114, 171), ("atl3.png", 1095, 472, 170, 170),
    ("atl80.png", 1096, 229, 132, 198), ("atl80.png", 1484, 68, 132, 198),
    ("ln2purifier.png", 851, 48, 138, 201), ("mphub.png", 623, 237, 158, 176),
    ("hestorage.png", 204, 45, 300, 200),
]
PIPES = [  # drawn below the equipment
    ("pipe_teslatron_return.svg", 600, 34, 1237, 380), ("pipe_source_recovery.svg", 110, 290, 510, 254),
    ("pipe_cmi_recovery.svg", 110, 372, 129, 0), ("pipe_compressor_to_storage.svg", 325, 218, 323, 36),
    ("pipe_storage_to_purifier.svg", 492, 128, 391, 0), ("pipe_purified_supply.svg", 961, 128, 539, 0),
    ("pipe_supply_manifold.svg", 1238, 128, 142, 424), ("pipe_atl2_branch.svg", 1207, 295, 173, 0),
    ("pipe_tank_sensing.svg", 202, 105, 28, 0), ("pipe_inlet_sensing.svg", 526, 290, 0, 18),
]
MARKS = [
    ("junction_recovery.svg", 106, 368), ("junction_supply.svg", 1376, 291), ("junction_supply.svg", 1376, 124),
    ("flow0.svg", 1305, 547), ("flow0.svg", 1285, 290), ("flow1.svg", 1375, 465), ("flow2.svg", 1445, 123),
    ("flow2.svg", 1050, 123), ("flow2.svg", 660, 123), ("flow3.svg", 500, 213), ("flow4.svg", 1145, 29),
    ("flow5.svg", 1832, 61), ("flow6.svg", 1735, 409), ("flow7.svg", 595, 266), ("flow6.svg", 445, 285),
    ("flow4.svg", 145, 367), ("flow5.svg", 105, 310), ("flow4.svg", 165, 539),
]
LABELS = [  # (text, x, y, size, color)
    ("Purified supply", 1050, 98, 13, "#20a444"), ("Compressed to storage", 335, 229, 13, "#d49a20"),
    ("Low-pressure recovery", 300, 258, 13, "#2564c7"), ("Janis", 474, 645, 13, "#192d40"),
    ("CMI", 238, 512, 15, "#192d40"), ("Teslatron", 1516, 536, 15, "#192d40"),
    ("ATL3 ATL", 1104, 645, 13, "#192d40"), ("ATL1 ATL80", 1101, 434, 15, "#192d40"),
    ("ATL2 · ATL80", 1492, 272, 15, "#192d40"), ("MPHUB compressor", 624, 420, 15, "#192d40"),
    ("LN2purifier", 854, 255, 15, "#192d40"), ("He storage", 274, 17, 15, "#192d40"),
]
LEGEND = [("#2564c7", "Low Pres. He gas (0.8–1.3 bar)"), ("#d49a20", "Mid Pres. He gas (2–8 bar)"),
          ("#20a444", "Supply He gas (20 psi)")]

_served = False


def _serve_static() -> None:
    global _served
    if not _served:
        app.add_static_files(URL, STATIC)
        _served = True


def _svg_size(name: str) -> tuple[float, float]:
    head = (STATIC / "diagram" / name).read_text(encoding="utf-8")[:300]
    w = float(re.search(r'width="([\d.]+)"', head).group(1))
    h = float(re.search(r'height="([\d.]+)"', head).group(1))
    return w, h


def _abs(x: float, y: float, w: float | None = None, h: float | None = None) -> str:
    s = f"position:absolute;left:{x}px;top:{y}px;"
    if w is not None:
        s += f"width:{w}px;"
    if h is not None:
        s += f"height:{h}px;"
    return s


class Diagram:
    """Builds the schematic; `update(snap, valve_open)` refreshes the live panels."""

    def __init__(self) -> None:
        _serve_static()
        with ui.column().classes("hr-card w-full gap-0").style("padding:0"):
            with ui.row().classes("w-full items-center justify-between no-wrap").style("padding:14px 20px"):
                ui.label("Recovery & liquefaction circuit").classes("text-[19px]")
                with ui.row().classes("gap-7 no-wrap"):
                    for color, text in LEGEND:
                        with ui.row().classes("items-center gap-2 no-wrap"):
                            ui.element("span").style(f"width:28px;height:3px;background:{color}")
                            ui.label(text).style(f"color:{color};font-size:13px")
            ui.element("div").classes("w-full").style("border-top:1px solid var(--hr-line)")
            outer = ui.element("div").classes("w-full hr-diagram-outer").style(
                f"position:relative;overflow:hidden;aspect-ratio:{W}/{H}")
            with outer:
                self.canvas = ui.element("div").classes("hr-diagram").style(
                    f"position:absolute;left:0;top:0;width:{W}px;height:{H}px;transform-origin:0 0;background:#fff")
                with self.canvas:
                    self._build()
            with ui.row().classes("w-full no-wrap hr-note").style(
                    "padding:10px 20px;border-top:1px solid var(--hr-line);font-size:13px"):
                ui.label("Reference routing · Live values (storage, purifier and Teslatron have no sensors)")
        # scale the fixed-size canvas to the card width; observe each container as Vue
        # mounts it (it has zero width while its tab is hidden, so measure on resize)
        ui.add_head_html("""<script>(() => {
            const ro = new ResizeObserver(es => es.forEach(e => {
                const c = e.target.querySelector('.hr-diagram');
                if (c && e.contentRect.width) c.style.transform = `scale(${e.contentRect.width / %d})`; }));
            const seen = new WeakSet();
            new MutationObserver(() => document.querySelectorAll('.hr-diagram-outer').forEach(o => {
                if (!seen.has(o)) { seen.add(o); ro.observe(o); } }))
              .observe(document.documentElement, {childList: true, subtree: true});
        })();</script>""" % W)

    def _img(self, name: str, style: str) -> None:
        ui.element("img").props(f'src="{URL}/diagram/{name}" alt="" draggable="false"').style(style)

    def _build(self) -> None:
        for name, x, y, w, h in PIPES:
            sw, sh = _svg_size(name)
            self._img(name, _abs(x - (sw - w) / 2, y - (sh - h) / 2, sw, sh))
        for name, x, y, w, h in EQUIPMENT:
            self._img(name, _abs(x, y, w, h) + "object-fit:cover")
        for name, x, y in MARKS:
            sw, sh = _svg_size(name)
            self._img(name, _abs(x, y, sw, sh))
        for text, x, y, size, color in LABELS:
            ui.label(text).style(_abs(x, y) + f"font-size:{size}px;font-weight:500;color:{color};white-space:nowrap")

        # pressure instruments
        self.tank_p = self._pressure("Tank Pres. (bar)", 26, 65)
        self.inlet_p = self._pressure("Inlet Pres. (bar)", 419, 308)
        self.outlet_p = self._pressure("Outlet Pres. (bar)", 639, 455)
        with ui.row().classes("items-center gap-2 no-wrap").style(_abs(26, 154, 140)):
            with ui.element("div").style("width:100px;height:8px;border-radius:4px;background:#d3dce5;position:relative"):
                self.tank_bar = ui.element("div").style(
                    "position:absolute;left:0;top:0;height:8px;border-radius:4px;background:#2466a2;width:0")
            ui.label("8 bar").classes("hr-mono").style("font-size:11px;color:#617387")

        # liquefier panels (ATL3 also shows its inlet valve)
        self.atl = {
            Instrument.ATL1: self._liquefier("ATL1", 883, 320, 207),
            Instrument.ATL2: self._liquefier("ATL2", 1602, 132, 230),
            Instrument.ATL3: self._liquefier("ATL3", 846, 509, 230, valve=True),
        }
        # source vessels read by this PC / from Google Sheets
        self.janis = self._monitor(559, 548)
        self.cmi = self._monitor(26, 559)

    def _pressure(self, label: str, x: int, y: int) -> ui.label:
        with ui.column().classes("gap-1").style(
                _abs(x, y, 176) + "background:#eaf2fa;border:1px solid #d3dce5;border-radius:5px;padding:12px"):
            ui.label(label).style("font-size:13px;color:#617387")
            with ui.row().classes("items-baseline gap-2 no-wrap"):
                v = ui.label("—").classes("hr-mono").style("font-size:36px;font-weight:500;color:#192d40;line-height:1.2")
                ui.label("bar").style("font-size:13px;color:#617387")
        return v

    def _liquefier(self, name: str, x: int, y: int, w: int, valve: bool = False) -> dict:
        out: dict = {}
        with ui.column().classes("gap-[10px]").style(
                _abs(x, y, w) + "background:#f6f8fa;border:1px solid #d3dce5;border-radius:5px;padding:12px") as panel:
            out["panel"] = panel
            with ui.row().classes("w-full justify-between items-baseline no-wrap"):
                out["title"] = ui.label(name).style("font-size:14px;font-weight:600;color:#2466a2")
                if valve:  # ATL3 inlet valve state on the title line
                    with ui.row().classes("items-baseline gap-[6px] no-wrap").props('title="ATL3 inlet valve"'):
                        out["valve"] = ui.label("—").style("font-size:13px;font-weight:600")
                        out["valve_no"] = ui.label("").classes("hr-mono").style("font-size:12px;color:#617387")
            for key, label, unit in (("liquid_volume", "Liq. volume", "L"), ("dewar_pressure", "Pressure", "psi"),
                                     ("input_gas_flow", "Gas flow", "l/min")):
                with ui.row().classes("w-full justify-between items-baseline no-wrap"):
                    ui.label(label).style("font-size:13px;color:#617387")
                    with ui.row().classes("items-baseline gap-[6px] no-wrap"):
                        out[key] = ui.label("—").classes("hr-mono").style(
                            "font-size:26px;font-weight:500;color:#192d40;line-height:1.1")
                        ui.label(unit).style("font-size:12px;color:#617387")
        return out

    def _monitor(self, x: int, y: int) -> dict:
        out: dict = {}
        out["box"] = ui.column().classes("gap-[5px]").style(
            _abs(x, y, 277) + "background:#f9f0f0;border-radius:5px;padding:10px")
        with out["box"]:
            out["title"] = ui.label("—").style("font-size:14px;font-weight:500;color:#b24242")
            out["reading"] = ui.label("—").classes("hr-mono").style("font-size:12px;color:#617387")
            out["updated"] = ui.label("").style("font-size:12px;color:#617387")
            out["error"] = ui.label("").style("font-size:12px;color:#b24242")
        return out

    # ------------------------------------------------------------------ updates
    def update(self, snap: Snapshot, valve_open: bool | None) -> None:
        p = snap.get(Instrument.PUMP_DATA)
        if p is not None:
            self.tank_p.set_text(fmt(p.tank_pressure, 3))
            self.inlet_p.set_text(fmt(p.inlet_pressure, 3))
            self.outlet_p.set_text(fmt(p.outlet_pressure, 3))
            frac = max(0.0, min(1.0, p.tank_pressure / 8.0))
            self.tank_bar.style(f"width:{frac * 100:.0f}px")
        for slot, out in self.atl.items():
            a, link = snap.get(slot), snap.link(slot)
            state = "" if link.connected else (" · error" if link.last_error else " · waiting")
            out["title"].set_text(f"{slot.value.upper()}{state}")
            if a is not None:
                set_title(out["title"], liquid_mode_name(slot, a.liquid_mode, a.transfer_mode))
            if a is not None:
                out["liquid_volume"].set_text(fmt(a.liquid_volume, 1))
                out["dewar_pressure"].set_text(fmt(a.dewar_pressure, 2))
                out["input_gas_flow"].set_text(fmt(a.input_gas_flow, 1))
        atl3 = self.atl[Instrument.ATL3]
        v = snap.get(Instrument.VALVE)
        atl3["valve"].set_text("valve " + {True: "OPEN", False: "CLOSED"}.get(valve_open, "—"))
        atl3["valve"].style(f"color:{'#1e7a46' if valve_open else '#c0392b' if valve_open is False else '#617387'}")
        atl3["valve_no"].set_text(f"· {fmt(v.status_number)}" if v is not None else "")

        j = snap.get(Instrument.JANIS)
        self._update_monitor(self.janis, "Janis", snap.link(Instrument.JANIS),
                             None if j is None else (j.lhe_level_in, j.lhe_volume_l, j.last_update))
        c = snap.get(Instrument.CMI)
        self._update_monitor(self.cmi, "CMI", snap.link(Instrument.CMI),
                             None if c is None else (c.level, c.volume_l, c.source_time or c.last_update))

    @staticmethod
    def _update_monitor(out: dict, name: str, link, values) -> None:
        ok = link.connected
        out["box"].style(f"background:{'#eaf5ef' if ok else '#f9f0f0'}")
        out["title"].style(f"color:{'#238265' if ok else '#b24242'}")
        out["title"].set_text(f"{name} · {'ok' if ok else 'error / stale'}")
        if values is not None:
            level, vol, t = values
            out["reading"].set_text(f"LHe {fmt(level, 1)} in · {fmt(vol, 2)} L")
            out["updated"].set_text(f"{'Last update' if ok else 'Last good update'} {fmt(t)}")
        out["error"].set_text("" if ok else (link.last_error or "no reading yet")[:60])
