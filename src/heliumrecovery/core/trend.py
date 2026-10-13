"""24 h of 1-minute averages for the overnight forecast, plus "break" times.

The chart history (core/history.py) only holds ~50 min, too short for a 3 h
trend. This keeps one averaged point per minute per series for 24 h and the
times at which a series' behaviour changed on purpose (ATL mode change, pump
enabled/disabled), so the forecast can start after them.

It is saved to a JSON file every few minutes and loaded at start-up, so a
restart at 21:30 still has the evening's data for the 22:00 report.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from ..drivers.base import now
from .engine import DataEngine, Instrument, Update

log = logging.getLogger(__name__)

Point = tuple[datetime, float]
KEEP = timedelta(hours=24)
ATL_SERIES = {Instrument.ATL1: "atl1_volume", Instrument.ATL2: "atl2_volume", Instrument.ATL3: "atl3_volume"}
PUMP_SERIES = ("outlet_pressure", "inlet_pressure", "tank_pressure")


class Trend:
    def __init__(self, engine: DataEngine, path: Path | None = None, save_every: float = 300.0) -> None:
        self.engine, self.path, self.save_every = engine, path, save_every
        self.series: dict[str, deque[Point]] = {}
        self.breaks: dict[str, list[datetime]] = {}
        self._acc: dict[str, tuple[datetime, float, int]] = {}  # minute -> running sum, count
        self._atl_mode: dict[Instrument, float] = {}
        self._pump_enabled: bool | None = None
        self._last_save: datetime | None = None

    # ------------------------------------------------------------------ data
    def add(self, name: str, value: float, t: datetime) -> None:
        minute = t.replace(second=0, microsecond=0)
        acc = self._acc.get(name)
        if acc is not None and acc[0] != minute:
            self._push(name, acc[0], acc[1] / acc[2])
            acc = None
        self._acc[name] = (minute, value, 1) if acc is None else (minute, acc[1] + value, acc[2] + 1)

    def _push(self, name: str, minute: datetime, value: float) -> None:
        q = self.series.setdefault(name, deque())
        q.append((minute, value))
        while q and q[0][0] < minute - KEEP:
            q.popleft()

    def mark_break(self, name: str, t: datetime) -> None:
        b = self.breaks.setdefault(name, [])
        b.append(t)
        cutoff = t - KEEP
        self.breaks[name] = [x for x in b if x >= cutoff]

    def get(self, name: str) -> list[Point]:
        out = list(self.series.get(name, ()))
        acc = self._acc.get(name)
        if acc is not None:  # include the minute in progress
            out.append((acc[0], acc[1] / acc[2]))
        return out

    def on_update(self, u: Update) -> None:
        if not u.link.connected or u.value is None:
            return
        v = u.value
        match u.slot:
            case Instrument.PUMP_DATA:
                for name in PUMP_SERIES:
                    self.add(name, getattr(v, name), v.last_update)
            case Instrument.PUMP_SETTINGS:
                if self._pump_enabled is not None and v.enabled != self._pump_enabled:
                    for name in PUMP_SERIES:
                        self.mark_break(name, now())
                self._pump_enabled = v.enabled
            case Instrument.ATL1 | Instrument.ATL2 | Instrument.ATL3:
                name = ATL_SERIES[u.slot]
                t = v.last_update or now()
                self.add(name, v.liquid_volume, t)
                old = self._atl_mode.get(u.slot)
                if old is not None and v.liquid_mode != old:
                    self.mark_break(name, t)
                self._atl_mode[u.slot] = v.liquid_mode
            case Instrument.JANIS:
                self.add("janis_level", v.lhe_level_in, v.last_update)
            case Instrument.CMI | Instrument.WEI:
                if v.level is not None:
                    self.add(f"{u.slot}_level", v.level, v.source_time or v.last_update)

    # ------------------------------------------------------------------ file
    def save(self) -> None:
        if self.path is None:
            return
        data = {
            "series": {k: [(t.isoformat(), round(x, 5)) for t, x in q] for k, q in self.series.items()},
            "breaks": {k: [t.isoformat() for t in b] for k, b in self.breaks.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("trend save failed: %s", exc)

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("trend file unreadable, starting empty: %s", exc)
            return
        cutoff = now() - KEEP
        for k, pts in data.get("series", {}).items():
            self.series[k] = deque((datetime.fromisoformat(t), x) for t, x in pts
                                   if datetime.fromisoformat(t) >= cutoff)
        for k, ts in data.get("breaks", {}).items():
            self.breaks[k] = [datetime.fromisoformat(t) for t in ts if datetime.fromisoformat(t) >= cutoff]

    async def run(self) -> None:
        self.load()
        try:
            async for u in self.engine.subscribe(maxsize=1000):
                self.on_update(u)
                t = now()
                if self._last_save is None or (t - self._last_save).total_seconds() >= self.save_every:
                    self._last_save = t
                    self.save()
        finally:
            self.save()
