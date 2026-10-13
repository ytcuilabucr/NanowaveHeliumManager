"""Look of the operator console, taken from the Figma design.

Tokens: page #edf1f5, cards white with #d3dce5 border and 8 px radius, tiles
#f6f8fa, text #192d40 / muted #617387, accent #2466a2; state colors green
#238265 on #eaf5ef, error #b54242 on #fceeee, warning #936310 on #fff4de.
Type: Inter for text, Roboto Mono for values.

The helpers build the recurring pieces (cards, state badges, value tiles,
reading rows, page headings, notices) and return the labels that `refresh()`
updates, so app.py only deals with data.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from nicegui import ui

CSS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Roboto+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --hr-page: #edf1f5; --hr-card: #ffffff; --hr-line: #d3dce5; --hr-tile: #f6f8fa;
  --hr-text: #192d40; --hr-muted: #617387; --hr-accent: #2466a2; --hr-accent-bg: #eaf2fa;
  --hr-ok: #238265; --hr-ok-bg: #eaf5ef; --hr-err: #b54242; --hr-err-bg: #fceeee;
  --hr-warn: #936310; --hr-warn-bg: #fff4de; --hr-alert-yellow: #f2b705; --hr-atl3: #6e6bb8;
  --hr-mono: 'Roboto Mono', ui-monospace, Consolas, monospace;
}
body { background: var(--hr-page); color: var(--hr-text); font-family: Inter, system-ui, 'Segoe UI', sans-serif; }
.nicegui-content { padding: 0; gap: 0; }
.hr-mono { font-family: var(--hr-mono); }
.hr-muted { color: var(--hr-muted); }
.hr-text { color: var(--hr-text); }
.hr-accent { color: var(--hr-accent); }
.hr-err { color: var(--hr-err); }
.hr-ok { color: var(--hr-ok); }

/* header + navigation */
.hr-header { background: #fff; min-height: 74px; flex-wrap: wrap; gap: 8px 24px; padding: 0 32px; display: flex; align-items: center; justify-content: space-between; width: 100%; }
.hr-logo { background: var(--hr-accent); color: #fff; font-weight: 700; font-size: 20px; width: 36px; height: 36px; border-radius: 5px; display: flex; align-items: center; justify-content: center; }
.hr-nav { background: #fff; height: 54px; overflow-x: auto; scrollbar-width: none; padding: 0 32px; border-top: 1px solid var(--hr-line); border-bottom: 1px solid var(--hr-line); width: 100%; display: flex; align-items: stretch; justify-content: space-between; }
.hr-tab { padding: 0 22px; display: flex; align-items: center; font-size: 16px; font-weight: 500; color: var(--hr-muted); cursor: pointer; border-bottom: 3px solid transparent; user-select: none; white-space: nowrap; }
.hr-tab:hover { color: var(--hr-text); }
.hr-tab.active { background: var(--hr-accent-bg); color: var(--hr-accent); font-weight: 400; border-bottom-color: var(--hr-accent); }
.hr-internal-link { white-space: nowrap; display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 500; color: var(--hr-muted); cursor: pointer; }
.hr-internal-link.active { color: var(--hr-warn); }

/* page */
.hr-screen { padding: 8px 32px 24px 32px; width: 100%; gap: 12px; }
.hr-h1 { font-size: 30px; line-height: 1.35; }
.hr-sub { font-size: 16px; color: var(--hr-muted); }
.hr-footer { background: #fff; border-top: 1px solid var(--hr-line); height: 42px; padding: 0 32px; width: 100%; display: flex; align-items: center; justify-content: space-between; font-size: 13px; color: var(--hr-muted); }

/* cards */
.hr-card { background: var(--hr-card); border: 1px solid var(--hr-line); border-radius: 8px; padding: 12px 12px; gap: 8px; }
.hr-card.atl3 { border: 2px solid var(--hr-atl3); }
.hr-card.alert-warn { border: 2px solid var(--hr-alert-yellow); box-shadow: 0 0 0 3px var(--hr-warn-bg); }
.hr-card.alert-crit { border: 2px solid var(--hr-err); box-shadow: 0 0 0 3px var(--hr-err-bg); }
.hr-alert-mark { font-size: 13px; font-weight: 600; border-radius: 5px; padding: 3px 8px; line-height: 1.3; }
.hr-alert-mark.warn { background: var(--hr-alert-yellow); color: #3d2c00; }
.hr-alert-mark.crit { background: var(--hr-err); color: #ffffff; }
.hr-alert-mark.pending { background: transparent; border: 1.5px dashed currentColor; }
.hr-alert-mark.warn.pending { color: var(--hr-warn); }
.hr-alert-mark.crit.pending { color: var(--hr-err); }
.hr-alert-banner { border-radius: 8px; padding: 8px 14px; font-size: 15px; font-weight: 600; }
.hr-alert-banner.warn { background: var(--hr-warn-bg); color: var(--hr-warn); border: 1.5px solid var(--hr-alert-yellow); }
.hr-alert-banner.crit { background: var(--hr-err-bg); color: var(--hr-err); border: 1.5px solid var(--hr-err); }
.hr-card-title { font-size: 19px; }
.hr-section-title { font-size: 19px; }
.hr-note { font-size: 12px; color: var(--hr-muted); }
.hr-desc { font-size: 14px; color: var(--hr-muted); }

/* state badges */
.hr-badge { display: inline-flex; align-items: center; gap: 7px; padding: 6px 11px; border-radius: 5px; font-size: 14px; font-weight: 600; white-space: nowrap; }
.hr-badge .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.hr-badge.ok { background: var(--hr-ok-bg); color: var(--hr-ok); }
.hr-badge.err { background: var(--hr-err-bg); color: var(--hr-err); }
.hr-badge.warn { background: var(--hr-warn-bg); color: var(--hr-warn); }
.hr-badge.info { background: var(--hr-accent-bg); color: var(--hr-accent); }
.hr-badge.idle { background: var(--hr-tile); color: var(--hr-muted); }
.hr-badge.small { padding: 2px 7px; font-size: 12px; gap: 5px; }

/* value tiles + reading rows */
.hr-tile { background: var(--hr-tile); border-radius: 5px; padding: 10px 16px 12px 16px; gap: 2px; }
.hr-tile .lbl { font-size: 15px; color: var(--hr-muted); }
.hr-tile .val { font-family: var(--hr-mono); font-size: clamp(22px, 2vw, 38px); font-weight: 500; color: var(--hr-text); line-height: 1.25; }
.hr-tile .unit { font-size: 15px; color: var(--hr-muted); margin-left: 8px; }
.hr-tile.err .val { color: var(--hr-muted); }
.hr-row { display: grid; grid-template-columns: 1fr auto 48px; align-items: baseline; border-bottom: 1px solid var(--hr-line); padding: 3px 0; }
.hr-row .lbl { font-size: clamp(14px, 0.9vw, 17px); white-space: nowrap; color: var(--hr-muted); }
.hr-row .val { font-family: var(--hr-mono); font-size: clamp(16px, 1.25vw, 24px); font-weight: 500; color: var(--hr-text); text-align: right; }
.hr-row .val.mode { font-family: Inter, sans-serif; font-size: 17px; font-weight: 500; color: var(--hr-accent); }
.hr-row .unit { font-size: 14px; color: var(--hr-muted); padding-left: 12px; }
.hr-kv { display: flex; justify-content: space-between; align-items: baseline; font-size: 13px; }
.hr-kv .val { font-family: var(--hr-mono); font-size: 13px; color: var(--hr-text); }

/* register tables (Transfer Control / BP Valve Tuning) */
.hr-reg { width: 100%; border-collapse: collapse; }
.hr-reg th { font-size: 13px; font-weight: 400; color: var(--hr-muted); text-align: left; padding: 4px 0; letter-spacing: .02em; }
.hr-reg td { border-bottom: 1px solid var(--hr-line); padding: 1px 0; font-size: 16px; height: 38px; }
.hr-reg td.reg { font-family: var(--hr-mono); font-size: 13px; color: var(--hr-muted); }
.hr-reg td.unit { font-size: 14px; color: var(--hr-muted); }
.hr-reg td.v { font-family: var(--hr-mono); font-weight: 500; font-size: clamp(16px, 1.25vw, 24px); }
.hr-reg tr.gain td { background: #f2f6fb; }

/* notices */
.hr-notice { background: var(--hr-warn-bg); border: 1px solid #ecd9a6; border-radius: 6px; padding: 10px 16px; color: var(--hr-warn); gap: 14px; align-items: center; width: 100%; }
.hr-notice.ok { background: var(--hr-ok-bg); border-color: #bfe0cc; color: var(--hr-ok); }
.hr-monitor { background: #f9f0f0; border-radius: 5px; padding: 8px 10px; gap: 2px; }
.hr-monitor.ok { background: var(--hr-ok-bg); }
.hr-monitor .t { font-size: 14px; font-weight: 500; }
.hr-monitor .l { font-size: 12px; color: var(--hr-muted); }

/* buttons */
.hr-btn { border: 1.5px solid var(--hr-accent); color: var(--hr-accent); background: #fff; border-radius: 6px; padding: 5px 16px; font-size: 15px; font-weight: 500; cursor: pointer; white-space: nowrap; }
.hr-btn:hover { background: var(--hr-accent-bg); }
.hr-btn.green { border-color: #1e7a46; color: #1e7a46; }
.hr-btn.green:hover { background: var(--hr-ok-bg); }
.hr-btn.red { border-color: #c0392b; color: #c0392b; }
.hr-btn.red:hover { background: var(--hr-err-bg); }
.hr-btn.locked { border-color: var(--hr-line); color: var(--hr-muted); background: #eef1f4; }
.hr-seg { border: 1px solid var(--hr-line); border-radius: 5px; padding: 6px 0; text-align: center; font-size: 15px; color: var(--hr-muted); background: var(--hr-tile); cursor: pointer; flex: 1; }
.hr-seg.active { border: 1.5px solid var(--hr-accent); color: var(--hr-accent); background: var(--hr-accent-bg); font-weight: 500; }
.hr-seg { min-width: 64px; padding-left: 6px; padding-right: 6px; }
.hr-seg.stop { color: var(--hr-err); }
.hr-seg.stop.active { border-color: var(--hr-err); background: var(--hr-err-bg); }
.hr-seg.cooling { opacity: .45; cursor: not-allowed; }

/* inputs inside register tables */
.hr-input .q-field__control { height: 34px; min-height: 34px; border-radius: 5px; }
.hr-input .q-field__marginal { height: 34px; }
.hr-input .q-field__native { font-family: var(--hr-mono); font-size: clamp(15px, 1.2vw, 24px); font-weight: 500; color: var(--hr-accent); padding: 0; }
.hr-input.q-field--outlined .q-field__control:before { border-color: var(--hr-line); }

/* inlet valve row (ATL3) */
.hr-valve { background: #f4f7fa; border-radius: 6px; padding: 8px 10px; gap: 8px 10px; align-items: center; width: 100%; flex-wrap: wrap; }
.hr-valve > * { white-space: nowrap; }

/* vessel level indicator (Graphic View) */
.hr-vessel { width: 50px; height: 110px; border: 1.5px solid #c9d4df; border-radius: 9px; background: #f6f8fa; position: relative; overflow: hidden; flex: none; }
.hr-vessel.small { width: 36px; height: 76px; }
.hr-vessel .fill { position: absolute; left: 0; right: 0; bottom: 0; height: 0; background: #e3eef9; border-top: 2px solid var(--hr-accent); }
.hr-vessel .tick { position: absolute; right: 0; width: 8px; border-top: 1px solid #9aabbd; }
.hr-big { font-family: var(--hr-mono); font-size: clamp(18px, 1.6vw, 30px); font-weight: 500; line-height: 1.15; white-space: nowrap; }
.hr-big .u { font-size: inherit; margin-left: 4px; }

/* error list (Status) */
.hr-errtab { width: 100%; border-collapse: collapse; }
.hr-errtab th { font-size: 13px; font-weight: 400; color: var(--hr-muted); text-align: left; padding: 6px 10px; background: var(--hr-tile); }
.hr-errtab td { font-size: 15px; padding: 7px 10px; border-bottom: 1px solid var(--hr-line); }
.hr-errtab td.t, .hr-errtab td.s { font-family: var(--hr-mono); }
.hr-errtab tr.active td { background: #fdf8f8; }
.hr-errtab tr.active td.s { color: var(--hr-err); }
.hr-errtab td.st { font-size: 13px; }

/* selectable list (Internal / BP registers) */
.hr-opt { border: 1px solid var(--hr-line); border-radius: 4px; background: var(--hr-tile); padding: 6px 12px; font-size: 15px; cursor: pointer; width: 100%; display: flex; justify-content: space-between; }
.hr-opt.sel { border: 1.5px solid var(--hr-accent); background: var(--hr-accent-bg); color: var(--hr-accent); }
.hr-opt.locked { color: var(--hr-muted); background: #eef1f4; cursor: not-allowed; }
.hr-opt .r { font-size: 12px; color: var(--hr-muted); }

.hr-tile .lbl { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hr-graphic-left { flex: 3 1 540px; max-width: 840px; }
.hr-graphic-right { flex: 2 1 340px; min-width: 0; }
.hr-col { flex: 1 1 300px; min-width: 0; }
.hr-col.wide { flex: 1.2 1 330px; }
.hr-col.wide2 { flex: 2 1 480px; }
.hr-card-title { white-space: nowrap; }
.hr-inactive { opacity: .45; filter: grayscale(1); }
.hr-inactive .hr-seg, .hr-inactive .hr-btn, .hr-inactive .hr-mode-btn { pointer-events: none; }
.hr-mode { display: flex; align-items: center; gap: 6px; border: 1.5px solid var(--hr-line); border-radius: 999px; padding: 4px 12px 4px 10px; cursor: pointer; font-size: 15px; font-weight: 600; user-select: none; white-space: nowrap; }
.hr-mode.observer { color: var(--hr-muted); background: var(--hr-tile); }
.hr-mode.transfer { color: var(--hr-warn); border-color: var(--hr-alert-yellow); background: var(--hr-warn-bg); }
.hr-mode.agent { color: var(--hr-accent); border-color: var(--hr-accent); background: var(--hr-accent-bg); }
.hr-mode-opt { border: 1px solid var(--hr-line); border-radius: 8px; padding: 10px 12px; cursor: pointer; }
.hr-mode-opt:hover { background: var(--hr-tile); }
.hr-mode-opt.sel { border: 1.5px solid var(--hr-accent); background: var(--hr-accent-bg); }
.hr-mode-ico.observer { color: var(--hr-muted); }
.hr-mode-ico.transfer { color: var(--hr-warn); }
.hr-mode-ico.agent { color: var(--hr-accent); }
.hr-subblock { border: 1px solid var(--hr-line); border-radius: 6px; padding: 10px 12px; }
.hr-sub-title { font-size: 16px; font-weight: 600; }
.hr-toggle { display: flex; align-items: center; gap: 6px; border: 1px solid var(--hr-line); border-radius: 5px; padding: 6px 12px; cursor: pointer; font-size: 14px; color: var(--hr-muted); background: var(--hr-tile); user-select: none; }
.hr-toggle.on { border: 1.5px solid var(--hr-ok); color: var(--hr-ok); background: var(--hr-ok-bg); font-weight: 500; }
</style>
"""

BADGE_CLASSES = ("ok", "err", "warn", "info", "idle")


class Badge:
    """State badge: colored dot + text (connected / error / waiting ...)."""

    def __init__(self, text: str = "waiting", kind: str = "idle", small: bool = False) -> None:
        self.card: ui.element | None = None  # set by `card`
        self.el = ui.element("span").classes(f"hr-badge {kind}" + (" small" if small else ""))
        with self.el:
            ui.element("span").classes("dot")
            self.label = ui.label(text)
        self._kind, self._text = kind, text

    def set(self, text: str, kind: str, tooltip: str | None = None) -> None:
        if (text, kind) != (self._text, self._kind):
            self.el.classes(remove=" ".join(BADGE_CLASSES), add=kind)
            self.label.set_text(text)
            self._text, self._kind = text, kind
        set_title(self.el, tooltip)


def set_title(el: ui.element, text: str | None) -> None:
    """Hover tooltip from any text. Error messages contain quotes, so this sets the
    prop directly instead of through a props string (which NiceGUI parses)."""
    if el.props.get("title") != (text or ""):
        el.props["title"] = text or ""
        el.update()


def install() -> None:
    ui.add_head_html(CSS)


@contextmanager
def card(title: str | None = None, *, badge: bool = True, classes: str = "") -> Iterator[Badge | None]:
    """White card with a title row and (optionally) a state badge on the right."""
    b = None
    with ui.column().classes(f"hr-card {classes}") as col:
        if title is not None:
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label(title).classes("hr-card-title")
                if badge:
                    b = Badge()
                    b.card = col
        yield b


def tile(label: str, unit: str, classes: str = "") -> ui.label:
    """Value tile; ``value.caption`` is the label (for a " · stale" suffix)."""
    with ui.column().classes(f"hr-tile {classes}"):
        caption = ui.label(label).classes("lbl")
        with ui.row().classes("items-baseline gap-0 no-wrap"):
            value = ui.label("—").classes("val")
            ui.label(unit).classes("unit")
    value.caption = caption  # type: ignore[attr-defined]
    return value


def vessel(small: bool = False) -> ui.element:
    """Dewar outline with tick marks and a fill bar; set ``fill`` height in %."""
    with ui.element("div").classes("hr-vessel" + (" small" if small else "")) as v:
        for pct in (25, 45, 65, 85):
            ui.element("div").classes("tick").style(f"top:{pct}%")
        v.fill = ui.element("div").classes("fill")  # type: ignore[attr-defined]
    return v


def big_value(unit: str) -> ui.label:
    """Large mono value with its unit in the same size (Graphic View cards)."""
    with ui.row().classes("hr-big items-baseline gap-0 no-wrap"):
        v = ui.label("—")
        ui.label(unit).classes("u")
    return v


def reading_rows(rows: list[tuple[str, str, str]]) -> dict[str, ui.label]:
    """Label / value / unit rows separated by hairlines (ATL cards)."""
    out = {}
    with ui.column().classes("w-full gap-0"):
        for key, label, unit in rows:
            with ui.element("div").classes("hr-row w-full"):
                ui.label(label).classes("lbl")
                out[key] = ui.label("—").classes("val" + (" mode" if key == "liquid_mode" else ""))
                ui.label(unit).classes("unit")
    return out


def kv(label: str, value: str = "—", classes: str = "") -> ui.label:
    with ui.element("div").classes(f"hr-kv w-full {classes}"):
        ui.label(label).classes("hr-muted")
        v = ui.label(value).classes("val")
    return v


def page_heading(title: str, subtitle: str | None = None) -> ui.row:
    """Title (and optional subtitle) on the left; returns the right-hand slot."""
    with ui.row().classes("w-full items-center justify-between gap-y-2").style("min-height:58px"):
        with ui.column().classes("gap-0"):
            ui.label(title).classes("hr-h1 whitespace-nowrap")
            if subtitle:
                ui.label(subtitle).classes("hr-sub")
        right = ui.row().classes("items-center gap-3")
    return right


def notice(title: str, body: str, kind: str = "") -> tuple[ui.label, ui.label]:
    with ui.row().classes(f"hr-notice no-wrap {kind}"):
        ui.icon("shield").classes("text-2xl")
        with ui.column().classes("gap-0"):
            t = ui.label(title).classes("text-[15px]")
            b = ui.label(body).classes("text-[14px]")
    return t, b


def button(text: str, on_click, kind: str = "", icon: str | None = None) -> ui.element:
    """Outlined console button (kind: '' blue, 'green', 'red', 'locked')."""
    el = ui.element("button").classes(f"hr-btn {kind}").on("click", on_click)
    with el:
        with ui.row().classes("items-center gap-2 no-wrap"):
            if icon:
                ui.icon(icon).classes("text-base")
            ui.label(text)
    return el


def link_state(connected: bool, last_error: str | None) -> tuple[str, str]:
    if connected:
        return "connected", "ok"
    return ("error", "err") if last_error else ("waiting", "idle")
