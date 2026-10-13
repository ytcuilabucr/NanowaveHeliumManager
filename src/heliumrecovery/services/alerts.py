"""Slack alerts and the 10 PM report (docs/slack_alerts_and_guardian.md §1, §2).

Every scenario is a `Rule` with a key (e.g. ``outlet_high``). Rules are
evaluated every `period` seconds; each key has its own state:

    normal --(true for >= debounce)--> ACTIVE: alert sent once
    ACTIVE --(still true)--> reminders at 1 h, 3 h, then every 6 h
    ACTIVE --(clear past the hysteresis band for >= debounce)--> "resolved" sent once

A rule's check returns True / False, or None when it cannot tell (no fresh
data): None never starts or clears an alert. Hysteresis lives in the checks:
they get the current state and use the "clear" threshold while active.

One-off events (mode changed by hand, write failed, program started) are
sent through `event()`.

Messages are bundled (everything due within `bundle_s` goes out together),
capped at `max_per_hour` (the rest waits and goes out as one message), and
dropped while muted. Critical ones @mention the managers. Instruments switched
off on the Internal tab (core.modes, first default HR_ALERT_ARCHIVED) get no
rules and no events; an active alert of one is dropped silently. In the
Transfer operation mode, "ATL not responding" is not raised.

Without a Slack token everything still runs and the UI shows what would have
been sent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ..core.engine import Instrument, Snapshot
from ..core.workers import ATL_MODELS, ATL_SLOTS
from ..drivers.atl import LiquidMode
from ..drivers.base import now as utcnow
from .forecast import Forecast, forecast, theil_sen
from .slack import SlackNotifier

log = logging.getLogger(__name__)

CRITICAL, WARNING, INFO = "critical", "warning", "info"
ICON = {CRITICAL: "🔴", WARNING: "🟠", INFO: "🔵"}
REMINDERS_S = (3600.0, 3 * 3600.0, 6 * 3600.0)  # then every 6 h
# real dewar sizes; the units' own "dewar capacity" setting is not trusted (ATL1 reports 160 L)
ATL_CAPACITY_L = {Instrument.ATL1: 80.0, Instrument.ATL2: 80.0, Instrument.ATL3: 160.0}
STORAGE_L_PER_BAR = 3000 / 757  # liquid-equivalent litres per bar of outlet storage (status log formula)
RUNNING_MODES = {LiquidMode.RECOVERY, LiquidMode.SLOW, LiquidMode.MEDIUM, LiquidMode.FAST}
TRANSFER_MODE = 6  # ATL precool / transfer

Check = tuple[bool | None, str]
# device names used in HR_ALERT_ARCHIVED and on the Internal tab
DEVICE_NAMES = {"pump": "Pump", "atl1": "ATL1", "atl2": "ATL2", "atl3": "ATL3", "janis": "Janis", "cmi": "CMI",
                "wei": "Wei-He3", "valve": "ATL3 inlet valve", "sheets": "Sheets", "program": "Program"}


def device(slot: Instrument) -> str:
    return "wei" if slot is Instrument.WEI else slot.value


@dataclass(frozen=True)
class Rule:
    key: str
    device: str  # pump, atl1..3, janis, cmi, wei, valve, sheets
    severity: str
    debounce_s: float
    title: str
    check: Callable[[AlertManager, bool], Check]


@dataclass
class AlertState:
    active: bool = False
    pending_since: float | None = None  # condition true, waiting for the debounce
    clear_since: float | None = None  # condition clear, waiting for the debounce
    sent_at: float | None = None  # first alert or last reminder
    started_at: float | None = None
    reminders: int = 0
    detail: str = ""


@dataclass
class Outgoing:
    t: float
    severity: str
    text: str
    mention: bool = False


@dataclass(frozen=True)
class AlertConfig:
    managers: tuple[str, ...] = ()  # Slack user IDs
    archived: frozenset[str] = frozenset({"atl2", "wei"})  # inactive devices when there is no core.modes
    period_s: float = 10.0
    bundle_s: float = 60.0
    max_per_hour: int = 6
    report_at: dtime = dtime(22, 0)
    report_late_s: float = 1800.0  # still send the report if started up to 30 min late
    horizon: dtime = dtime(8, 0)
    state_path: Path | None = None


# --------------------------------------------------------------------- helpers
def _fresh(snap: Snapshot, slot: Instrument) -> Any:
    """The slot's value if its last read worked, else None."""
    return snap.get(slot) if snap.link(slot).connected else None


def _above(get: Callable[[AlertManager], float | None], on: float, off: float, fmt: str) -> Callable:
    def check(m: AlertManager, active: bool) -> Check:
        v = get(m)
        if v is None:
            return None, ""
        return (v > off if active else v >= on), fmt.format(v=v)
    return check


def _below(get: Callable[[AlertManager], float | None], on: float, off: float, fmt: str) -> Callable:
    def check(m: AlertManager, active: bool) -> Check:
        v = get(m)
        if v is None:
            return None, ""
        return (v < off if active else v <= on), fmt.format(v=v)
    return check


def _silent_for(m: AlertManager, last_ok: datetime | None, limit_s: float) -> float | None:
    """Seconds without a good reading, if more than `limit_s` (else None)."""
    ref = last_ok.timestamp() if last_ok else m.started
    gap = m.now - ref
    return gap if gap >= limit_s else None


def _ago(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds < 5400 else f"{seconds / 3600:.1f} h"


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts).astimezone()


# ----------------------------------------------------------------- rule checks
def _pump(m: AlertManager):
    return _fresh(m.snap, Instrument.PUMP_DATA)


def _outlet(m):
    p = _pump(m)
    return p.outlet_pressure if p else None


def _outlet_rising(m: AlertManager, active: bool) -> Check:
    pts = [p for p in m.trend.get("outlet_pressure") if p[0].timestamp() >= m.now - 1800] if m.trend else []
    if len(pts) < 20:
        return None, ""
    slope, _ = theil_sen(pts)
    return (slope > 0.3 if active else slope > 0.5), f"outlet rising {slope:+.2f} bar/h over the last 30 min"


def _pump_disabled(m: AlertManager, active: bool) -> Check:
    s = _fresh(m.snap, Instrument.PUMP_SETTINGS)
    if s is None:
        return None, ""
    if s.enabled:
        return False, ""
    since = m.pump_disabled_since
    busy = m.transfer.dialog_open > 0 or m.transfer.auto_enable_armed
    if since is None or busy:
        return False, ""
    gap = m.now - since
    return gap >= 600, f"pump disabled for {_ago(gap)}, no transfer dialog open"


def _step1_waiting(m: AlertManager, active: bool) -> Check:
    tr = m.transfer
    if not tr.auto_enable_armed or tr.armed_since is None:
        return False, ""
    gap = m.now - tr.armed_since
    return gap >= 1800, f"Step 1 waiting for the inlet jump for {_ago(gap)}; the pump is disabled"


def _atl(slot):
    return lambda m: _fresh(m.snap, slot)


def _atl_stopped(slot):
    def check(m: AlertManager, active: bool) -> Check:
        s = _fresh(m.snap, slot)
        if s is None:
            return None, ""
        if int(s.liquid_mode) != LiquidMode.STOP:
            return False, ""
        before = m.atl_before_stop.get(slot)
        if before is None:
            return None, ""  # stopped since start-up: unknown whether it was liquefying
        model = ATL_MODELS[slot]
        return before in RUNNING_MODES, f"{slot.value.upper()} stopped (was {model.mode_text(before)})"
    return check


def _atl_full(slot):
    def check(m: AlertManager, active: bool) -> Check:
        s = _fresh(m.snap, slot)
        if s is None:
            return None, ""
        cap = ATL_CAPACITY_L[slot]
        pct = 100 * s.liquid_volume / cap
        return (pct >= 83 if active else pct >= 85), \
            f"{slot.value.upper()} dewar {s.liquid_volume:.1f} / {cap:g} L ({pct:.0f} %)"
    return check


def _atl_offline(slot):
    def check(m: AlertManager, active: bool) -> Check:
        link = m.snap.link(slot)
        last = m.snap.get(slot)
        if last is not None and last.liquid_mode == TRANSFER_MODE:
            return None, ""  # helium transfer in progress at this unit
        if m.transfer.dialog_open or m.transfer.auto_enable_armed:
            return None, ""  # transfer sequence in progress
        if m.modes is not None and m.modes.transfer:
            return None, ""  # Transfer operation mode: ATLs are unplugged on purpose
        gap = _silent_for(m, link.last_ok, 600)
        return gap is not None, f"{slot.value.upper()} not readable for {_ago(gap or 0)}"
    return check


def _offline(slot: Instrument, limit_s: float, name: str, needs: str | None = None):
    def check(m: AlertManager, active: bool) -> Check:
        if needs and getattr(m.core, needs, None) is None:
            return None, ""
        gap = _silent_for(m, m.snap.link(slot).last_ok, limit_s)
        return gap is not None, f"{name} not readable for {_ago(gap or 0)}"
    return check


def _janis(m):
    j = _fresh(m.snap, Instrument.JANIS)
    return j.lhe_level_in if j else None


def _cryo(slot):
    def get(m: AlertManager) -> float | None:
        c = _fresh(m.snap, slot)  # a stale sheet is flagged "not connected", so it is skipped here
        return c.level if c else None
    return get


def _cryo_stale(slot):
    def check(m: AlertManager, active: bool) -> Check:
        c = m.snap.get(slot)
        if c is None or c.source_time is None:
            return None, ""
        age = m.now - c.source_time.timestamp()
        limit = m.core.config.cryo_stale_after
        return age > limit, f"{c.name or slot.value} sheet last updated {_local(c.source_time.timestamp()):%m-%d %H:%M}"
    return check


def _sheets_failing(m: AlertManager, active: bool) -> Check:
    svcs = [s for s in (m.core.sheets, m.core.status_sheet) if s is not None]
    if not svcs:
        return None, ""
    oks = [t for s in svcs for t in (getattr(s, "last_upload", None), getattr(s, "last_status", None)) if t]
    gap = _silent_for(m, max(oks) if oks else None, 1800)
    return gap is not None, f"no successful Sheets upload for {_ago(gap or 0)}"


def build_rules() -> list[Rule]:
    P = Instrument.PUMP_DATA
    rules = [
        Rule("outlet_high", "pump", CRITICAL, 120, "Outlet pressure high",
             _above(_outlet, 7.5, 7.3, "outlet {v:.2f} bar (≥ 7.5) — storage nearly full")),
        Rule("outlet_low", "pump", WARNING, 300, "Outlet pressure low",
             _below(_outlet, 2.3, 2.5, "outlet {v:.2f} bar (≤ 2.3) — little gas left for the liquefiers")),
        Rule("outlet_very_low", "pump", CRITICAL, 300, "Outlet pressure very low",
             _below(_outlet, 2.0, 2.2, "outlet {v:.2f} bar (≤ 2.0) — liquefiers will starve")),
        Rule("inlet_high", "pump", CRITICAL, 60, "Inlet pressure over limit",
             _above(lambda m: _pump(m) and _pump(m).inlet_pressure, 1.40, 1.38,
                    "inlet {v:.3f} bar (≥ 1.40) — recovery line over-pressure")),
        Rule("tank_high", "pump", CRITICAL, 60, "Tank pressure over limit",
             _above(lambda m: _pump(m) and _pump(m).tank_pressure, 1.35, 1.33,
                    "tank {v:.3f} bar (≥ 1.35) — recovery bag side over-pressure")),
        Rule("pump_disabled_long", "pump", CRITICAL, 300, "Pump left disabled", _pump_disabled),
        Rule("step1_waiting_long", "pump", WARNING, 0, "Transfer Step 1 left waiting", _step1_waiting),
        Rule("oil_temp_high", "pump", WARNING, 300, "Pump oil temperature high",
             _above(lambda m: _pump(m) and _pump(m).oil_temperature, 90, 88, "oil {v:.1f} °C (≥ 90)")),
        Rule("outlet_rising_fast", "pump", INFO, 1800, "Outlet pressure rising fast", _outlet_rising),
        Rule("pump_offline", "pump", CRITICAL, 0, "Pump not responding", _offline(P, 120, "Pump")),
        Rule("janis_low", "janis", WARNING, 600, "Janis LHe low",
             _below(_janis, 17, 17.5, "Janis {v:.1f} in (≤ 17)")),
        Rule("janis_very_low", "janis", CRITICAL, 600, "Janis LHe very low",
             _below(_janis, 15, 15.5, "Janis {v:.1f} in (≤ 15)")),
        Rule("cmi_low", "cmi", WARNING, 600, "CMI LHe low",
             _below(_cryo(Instrument.CMI), 10, 10.5, "CMI {v:.1f} in (≤ 10)")),
        Rule("cmi_very_low", "cmi", CRITICAL, 600, "CMI LHe very low",
             _below(_cryo(Instrument.CMI), 6, 6.5, "CMI {v:.1f} in (≤ 6)")),
        Rule("janis_offline", "janis", INFO, 0, "Janis not responding",
             _offline(Instrument.JANIS, 1800, "Janis", needs="janis")),
        Rule("valve_offline", "valve", INFO, 0, "ATL3 inlet valve not responding",
             _offline(Instrument.VALVE, 1800, "ATL3 inlet valve", needs="valve")),
        Rule("sheets_failing", "sheets", INFO, 0, "Sheets upload failing", _sheets_failing),
    ]
    for slot in (Instrument.CMI, Instrument.WEI):
        d = device(slot)
        rules.append(Rule(f"{d}_stale", d, INFO, 0, f"{DEVICE_NAMES[d]} sheet not updated", _cryo_stale(slot)))
    for slot in ATL_SLOTS:
        n, d = slot.value.upper(), slot.value
        rules += [
            Rule(f"{d}_stopped", d, WARNING, 120, f"{n} stopped", _atl_stopped(slot)),
            Rule(f"{d}_dewar_full", d, INFO, 600, f"{n} dewar full", _atl_full(slot)),
            Rule(f"{d}_offline", d, WARNING, 0, f"{n} not responding", _atl_offline(slot)),
        ]
    return rules


# --------------------------------------------------------------------- manager
class AlertManager:
    def __init__(self, core: Any, notifier: SlackNotifier | None, config: AlertConfig = AlertConfig(),
                 clock: Callable[[], float] | None = None) -> None:
        self.core, self.notifier, self.cfg = core, notifier, config
        self.engine, self.transfer, self.errors = core.engine, core.transfer, core.errors
        self.trend = getattr(core, "trend", None)
        self.clock = clock or (lambda: utcnow().timestamp())
        self.modes = getattr(core, "modes", None)  # operation mode + instrument activation
        self.all_rules = build_rules()
        self.states: dict[str, AlertState] = {r.key: AlertState() for r in self.all_rules}
        self.queue: list[Outgoing] = []
        self.sent_times: deque[float] = deque()
        self.recent: deque[tuple[float, str, str]] = deque(maxlen=30)  # (t, status, text) for the UI
        self.started_log: deque[tuple[float, str]] = deque()  # alerts started, for the report's 24 h count
        self.mute_until: float | None = None
        self.last_report_date: str | None = None
        self.last_event: dict[str, float] = {}
        # event tracking
        self.known_settings: dict | None = None
        self.pump_disabled_since: float | None = None
        self.atl_mode: dict[Instrument, int] = {}
        self.atl_before_stop: dict[Instrument, int] = {}
        self.now = self.started = self.clock()
        self.snap: Snapshot = self.engine.read()
        self._load()

    # ------------------------------------------------------------ activation
    @property
    def inactive(self) -> set[str] | frozenset[str]:
        """Devices switched off: no rules, no events."""
        return self.modes.inactive if self.modes is not None else self.cfg.archived

    @property
    def rules(self) -> list[Rule]:
        off = self.inactive
        return [r for r in self.all_rules if r.device not in off]

    @property
    def mode_title(self) -> str:
        return self.modes.mode.title if self.modes is not None else "Observer"

    # ------------------------------------------------------------ evaluation
    def evaluate(self) -> None:
        self.now = self.clock()
        self.snap = self.engine.read()
        self._track_events()
        off = self.inactive
        for rule in self.all_rules:  # switched off: forget its state, no "resolved" message
            if rule.device in off and self.states[rule.key] != AlertState():
                self.states[rule.key] = AlertState()
        for rule in self.rules:
            st = self.states[rule.key]
            try:
                res, detail = rule.check(self, st.active)
            except Exception:  # a broken rule must not stop the others
                log.exception("alert rule %s failed", rule.key)
                continue
            if detail:
                st.detail = detail
            self._step(rule, st, res)
        self._save()

    def _step(self, rule: Rule, st: AlertState, res: bool | None) -> None:
        now = self.now
        if res is None:
            st.pending_since = None if not st.active else st.pending_since
            st.clear_since = None
            return
        if not st.active:
            if not res:
                st.pending_since = None
                return
            st.pending_since = st.pending_since or now
            if now - st.pending_since >= rule.debounce_s:
                st.active, st.sent_at, st.started_at, st.reminders = True, now, now, 0
                st.pending_since = st.clear_since = None
                self.started_log.append((now, rule.key))
                self._queue(rule.severity, f"{ICON[rule.severity]} *{rule.title}* · {st.detail}",
                            mention=rule.severity == CRITICAL)
            return
        if res:
            st.clear_since = None
            due = REMINDERS_S[min(st.reminders, len(REMINDERS_S) - 1)]
            if st.sent_at is not None and now - st.sent_at >= due:
                st.reminders += 1
                st.sent_at = now
                since = _ago(now - (st.started_at or now))
                self._queue(rule.severity, f"{ICON[rule.severity]} *{rule.title}* (still, for {since}) · {st.detail}",
                            mention=rule.severity == CRITICAL)
            return
        st.clear_since = st.clear_since or now
        if now - st.clear_since >= rule.debounce_s:
            since = _ago(now - (st.started_at or now))
            self.states[rule.key] = AlertState(detail=st.detail)
            self._queue(INFO, f"✅ *{rule.title}* resolved after {since} · {st.detail}")

    def _track_events(self) -> None:
        snap, now = self.snap, self.now
        s = _fresh(snap, Instrument.PUMP_SETTINGS)
        if s is not None:
            if s.enabled:
                self.pump_disabled_since = None
            elif self.pump_disabled_since is None:
                self.pump_disabled_since = now
            current = s.model_dump()
            written = getattr(self.core.pump, "last_written", None)
            if self.known_settings is not None and current != self.known_settings:
                if written is not None and written.model_dump() == current:
                    pass  # our own write
                else:
                    diff = ", ".join(f"{k} {self.known_settings.get(k)}→{v}" for k, v in current.items()
                                     if self.known_settings.get(k) != v)
                    self.event("pump_settings_changed", "pump", INFO,
                               f"Pump settings changed on the PLC (not from this program): {diff}")
            self.known_settings = current
        for slot in ATL_SLOTS:
            st = _fresh(snap, slot)
            if st is None:
                continue
            mode = int(st.liquid_mode)
            old = self.atl_mode.get(slot)
            if old is not None and mode != old:
                if mode == LiquidMode.STOP:
                    self.atl_before_stop[slot] = old
                req = getattr(self.core.atl, "requested", {}).get(slot)
                ours = req is not None and int(req[0]) == mode and now - req[1] < 300
                if not ours:
                    model = ATL_MODELS[slot]
                    self.event(f"{slot.value}_mode_changed", slot.value, INFO,
                               f"{slot.value.upper()} mode changed at the unit: "
                               f"{model.mode_text(old)} → {model.mode_text(mode, st.transfer_mode)}")
            self.atl_mode[slot] = mode

    # ---------------------------------------------------------------- events
    def event(self, key: str, device: str, severity: str, text: str, min_gap_s: float = 60.0) -> None:
        """A one-off notification (no state, no "resolved")."""
        if device in self.inactive:
            return
        now = self.clock()
        if now - self.last_event.get(key, -1e18) < min_gap_s:
            log.info("alert event %s suppressed (repeat within %g s): %s", key, min_gap_s, text)
            return
        self.last_event[key] = now
        self._queue(severity, f"{ICON[severity]} {text}", mention=severity == CRITICAL)

    def write_failed(self, device: str, text: str) -> None:
        self.event(f"{device}_write_failed", device, WARNING, f"*Write failed* · {text}", min_gap_s=0)

    # -------------------------------------------------------------- sending
    def _queue(self, severity: str, text: str, mention: bool = False) -> None:
        log.warning("alert: %s", text)
        self.queue.append(Outgoing(self.clock(), severity, text, mention))

    @property
    def muted(self) -> bool:
        return self.mute_until is not None and self.clock() < self.mute_until

    def mute(self, seconds: float | None) -> None:
        if seconds is None:
            self.mute_until = None
            self.queue.append(Outgoing(self.clock(), INFO, "🔔 Alerts unmuted"))
        else:
            self.mute_until = self.clock() + seconds
            self.queue.append(Outgoing(self.clock(), INFO,
                                       f"🔕 Alerts muted until {_local(self.mute_until):%a %H:%M} (maintenance)"))
        self.queue[-1].t -= self.cfg.bundle_s  # mute / unmute notices go out at once, even while muted
        self._save()

    def _mention(self) -> str:
        return " ".join(f"<@{u}>" for u in self.cfg.managers)

    async def flush(self) -> None:
        if not self.queue:
            return
        now = self.clock()
        if now - min(o.t for o in self.queue) < self.cfg.bundle_s:
            return
        notices = [o for o in self.queue if o.text.startswith(("🔕", "🔔"))]
        if self.muted:
            for o in self.queue:
                if o not in notices:
                    self.recent.append((o.t, "muted", o.text))
            self.queue = []
            if not notices:
                return
            items = notices
        else:
            while self.sent_times and now - self.sent_times[0] > 3600:
                self.sent_times.popleft()
            if len(self.sent_times) >= self.cfg.max_per_hour:
                return  # held; goes out as one message when the hour allows
            items, self.queue = self.queue, []
        text = "\n".join(o.text for o in items)
        if any(o.mention for o in items) and self.cfg.managers:
            text = f"{self._mention()}\n{text}"
        self.queue = [o for o in self.queue if o not in items]
        await self._send(text, now)

    async def _send(self, text: str, now: float) -> bool:
        self.sent_times.append(now)
        if self.notifier is None:
            self.recent.append((now, "not sent (Slack not configured)", text))
            return False
        try:
            await self.notifier.send(text)
        except httpx.HTTPError as exc:
            from ..core.errors import ErrorKind
            self.errors.set(ErrorKind.HTTP_POST, f"slack: {exc}", source="slack")
            self.recent.append((now, f"failed: {exc}", text))
            return False
        self.recent.append((now, "sent", text))
        return True

    # ---------------------------------------------------------------- report
    def _horizon(self, now_dt: datetime) -> datetime:
        local = now_dt.astimezone()
        h = local.replace(hour=self.cfg.horizon.hour, minute=self.cfg.horizon.minute, second=0, microsecond=0)
        return h if h > local else h + timedelta(days=1)

    def _fc(self, name: str, now_dt: datetime, horizon: datetime, latest: float | None,
            min_step: float, tolerance: float) -> Forecast | None:
        if self.trend is None:
            return None
        return forecast(self.trend.get(name), now_dt, horizon, breaks=self.trend.breaks.get(name, ()),
                        min_step=min_step, tolerance=tolerance, latest=latest)

    def build_report(self) -> tuple[str, bool]:
        """(message text, danger) — danger means a critical limit before the horizon."""
        now_ts = self.clock()
        now_dt = datetime.fromtimestamp(now_ts).astimezone()
        horizon = self._horizon(now_dt)
        hz = f"{horizon:%H:%M}"
        snap = self.engine.read()
        danger: list[str] = []
        lines = [f"🌙 Helium recovery · nightly status · {now_dt:%a %d %b %H:%M}", ""]

        def trend_text(fc: Forecast | None, unit: str, digits: int, limits: list[tuple[float, str, bool]]) -> str:
            """limits: (threshold, label, critical); returns ' trend → value at HH:MM  mark'."""
            if fc is None:
                return "   no trend data yet"
            if fc.unclear:
                return f"   {fc.unclear}"
            mark = "✅"
            out = f"   {fc.slope_h:+.{digits}f} {unit}/h → ≈ {fc.at_horizon:.{digits}f} {unit} at {hz}"
            for thr, label, crit in limits:
                t = fc.crossing(thr, now_dt, horizon)
                already = (label.startswith("≥") and fc.value >= thr) or (label.startswith("≤") and fc.value <= thr)
                if already or t is not None:
                    when = "now" if already else f"≈ {t.astimezone():%H:%M}"
                    out += f"\n     reaches {label} {thr:g} {unit} at {when} ⚠️"
                    mark = "⚠️"
                    if crit:
                        danger.append(label)
            return out + ("" if mark == "⚠️" else f"   {mark}")

        # pump
        p, s = _fresh(snap, Instrument.PUMP_DATA), _fresh(snap, Instrument.PUMP_SETTINGS)
        state = "not readable" if p is None else ("enabled" if s is None or s.enabled else "DISABLED")
        lines.append(f"*Pump (MP Hub)* · {state}")
        if p is not None:
            stored = (p.outlet_pressure - 1) * STORAGE_L_PER_BAR
            lines.append(f"  Inlet {p.inlet_pressure:.3f} bar · Tank {p.tank_pressure:.3f} bar · "
                         f"Outlet {p.outlet_pressure:.2f} bar (≈ {stored:.1f} L liquid in storage)")
            fc = self._fc("outlet_pressure", now_dt, horizon, p.outlet_pressure, 0.3, 0.05)
            lines.append("  Outlet" + trend_text(fc, "bar", 2, [(7.5, "≥", True), (2.3, "≤", False),
                                                                (2.0, "≤", True)]).replace("   ", " ", 1))
            use = sum(st.liquefaction_rate for sl in ATL_SLOTS
                      if (st := _fresh(snap, sl)) is not None and int(st.liquid_mode) in RUNNING_MODES
                      and sl.value not in self.inactive)
            if use > 0:
                lines.append(f"  Liquefiers use ≈ {use / 24:.2f} L/h (≈ {use / 24 / STORAGE_L_PER_BAR:.2f} bar/h "
                             f"of storage, before recovered gas comes in)")
        # ATLs
        for slot in ATL_SLOTS:
            n = slot.value.upper()
            if slot.value in self.inactive:
                lines.append(f"{n}  inactive (notifications off)")
                continue
            st = _fresh(snap, slot)
            if st is None:
                lines.append(f"{n}  not readable")
                continue
            cap = ATL_CAPACITY_L[slot]
            fc = self._fc(f"{slot.value}_volume", now_dt, horizon, st.liquid_volume, 3.0, 0.5)
            lines.append(f"{n}  {ATL_MODELS[slot].mode_text(st.liquid_mode, st.transfer_mode)}  "
                         f"{st.liquid_volume:.1f} / {cap:g} L" + trend_text(fc, "L", 1, [(0.85 * cap, "≥", False)]))
        # cryostats
        j = _fresh(snap, Instrument.JANIS)
        if "janis" in self.inactive:
            lines.append("Janis  inactive (notifications off)")
        else:
            if j is None:
                lines.append("Janis  not readable")
            else:
                fc = self._fc("janis_level", now_dt, horizon, j.lhe_level_in, 1.0, 0.2)
                lines.append(f"Janis  {j.lhe_level_in:.1f} in" +
                             trend_text(fc, "in", 2, [(17, "≤", False), (15, "≤", True)]))
        for slot, low, very in ((Instrument.CMI, 10, 6), (Instrument.WEI, None, None)):
            name = "CMI" if slot is Instrument.CMI else "Wei-He3"
            if device(slot) in self.inactive:
                lines.append(f"{name}  inactive (notifications off)")
                continue
            c = snap.get(slot)
            if c is None or c.level is None:
                lines.append(f"{name}  no data")
            elif not snap.link(slot).connected:
                when = f"{c.source_time.astimezone():%m-%d %H:%M}" if c.source_time else "?"
                lines.append(f"{name}  no fresh data (sheet last updated {when})")
            else:
                fc = self._fc(f"{slot.value}_level", now_dt, horizon, c.level, 1.0, 0.2)
                limits = [(low, "≤", False), (very, "≤", True)] if low is not None else []
                lines.append(f"{name}  {c.level:.1f} {c.level_unit or 'in'}" +
                             trend_text(fc, c.level_unit or "in", 2, limits))
        # summary
        cutoff = now_ts - 86400
        started = [k for t, k in self.started_log if t >= cutoff]
        active = [r.title for r in self.rules if self.states[r.key].active]
        lines += ["", f"Last 24 h: {len(started)} alert(s)" +
                  (f" · still active: {', '.join(active)}" if active else " · none active") +
                  f" · Operation mode: {self.mode_title}"]
        if danger:
            head = "⚠️ " + (self._mention() + " — " if self.cfg.managers else "") + \
                   "expected to need attention before morning"
            lines = [head, ""] + lines
        return "\n".join(lines), bool(danger)

    async def send_report(self) -> bool:
        text, _ = self.build_report()
        return await self._send(text, self.clock())

    def _report_due(self) -> bool:
        local = datetime.fromtimestamp(self.clock()).astimezone()
        target = local.replace(hour=self.cfg.report_at.hour, minute=self.cfg.report_at.minute,
                               second=0, microsecond=0)
        late = (local - target).total_seconds()
        return 0 <= late <= self.cfg.report_late_s and self.last_report_date != f"{local:%Y-%m-%d}"

    # ------------------------------------------------------------ persistence
    def _save(self) -> None:
        path = self.cfg.state_path
        if path is None:
            return
        data = {
            "states": {k: asdict(v) for k, v in self.states.items()},
            "mute_until": self.mute_until,
            "last_report_date": self.last_report_date,
            "started_log": list(self.started_log),
            "known_settings": self.known_settings,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.warning("alert state save failed: %s", exc)

    def _load(self) -> None:
        path = self.cfg.state_path
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("alert state unreadable, starting fresh: %s", exc)
            return
        for k, v in data.get("states", {}).items():
            if k in self.states:
                self.states[k] = AlertState(**v)
        self.mute_until = data.get("mute_until")
        self.last_report_date = data.get("last_report_date")
        self.started_log = deque((t, k) for t, k in data.get("started_log", []) if t >= self.started - 86400)
        self.known_settings = data.get("known_settings")

    # -------------------------------------------------------------------- run
    async def run(self) -> None:
        self.event("program_started", "program", INFO, "Helium recovery program started", min_gap_s=0)
        while True:
            try:
                self.evaluate()
                if self._report_due():
                    self.last_report_date = f"{datetime.fromtimestamp(self.clock()).astimezone():%Y-%m-%d}"
                    self._save()
                    await self.send_report()
                await self.flush()
            except Exception:
                log.exception("alert loop error")
            await asyncio.sleep(self.cfg.period_s)

    # ------------------------------------------------------------------- UI
    def active(self) -> list[tuple[Rule, AlertState]]:
        return [(r, self.states[r.key]) for r in self.rules if self.states[r.key].active]

    def pending(self) -> list[tuple[Rule, AlertState]]:
        return [(r, self.states[r.key]) for r in self.rules
                if not self.states[r.key].active and self.states[r.key].pending_since is not None]
