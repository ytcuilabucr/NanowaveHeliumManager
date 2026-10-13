"""Rolling history of selected values for the console charts."""

from __future__ import annotations

from collections import deque
from datetime import datetime

from ..drivers.base import now
from .engine import DataEngine, Instrument, Update

Point = tuple[datetime, float]


class History:
    """Keeps the last `maxlen` points of each series; fed from engine updates."""

    def __init__(self, engine: DataEngine, maxlen: int = 3000, pump_every: int = 5) -> None:
        self.engine, self.maxlen = engine, maxlen
        self.pump_every = pump_every  # pump updates every 0.2 s; keep one in five (1 s)
        self._pump_count = 0
        self.series: dict[str, deque[Point]] = {}

    def add(self, name: str, value: float, t: datetime | None = None) -> None:
        self.series.setdefault(name, deque(maxlen=self.maxlen)).append((t or now(), value))

    def get(self, name: str) -> list[Point]:
        return list(self.series.get(name, ()))

    def on_update(self, u: Update) -> None:
        if not u.link.connected or u.value is None:
            return
        v = u.value
        match u.slot:
            case Instrument.PUMP_DATA:
                self._pump_count += 1
                if self._pump_count % self.pump_every:
                    return
                t = v.last_update
                self.add("inlet_pressure", v.inlet_pressure, t)
                self.add("tank_pressure", v.tank_pressure, t)
                self.add("outlet_pressure", v.outlet_pressure, t)
                settings = self.engine.read().get(Instrument.PUMP_SETTINGS)
                if settings is not None:
                    self.add("inlet_setpoint", settings.inlet_pressure_setpoint / 1000, t)
                for d in v.user_data:
                    self.add(f"user_{d.index}", d.value, t)
            case Instrument.ATL1 | Instrument.ATL2 | Instrument.ATL3:
                self.add(f"{u.slot}_liquid_volume", v.liquid_volume, v.last_update)
                self.add(f"{u.slot}_diode_temp", v.diode_temp, v.last_update)
            case Instrument.JANIS:
                self.add("janis_level", v.lhe_level_in, v.last_update)
                self.add("janis_volume", v.lhe_volume_l, v.last_update)
            case Instrument.CMI | Instrument.WEI:
                name, t = f"{u.slot}_volume", v.source_time or v.last_update
                last = self.series.get(name)
                if v.volume_l is not None and not (last and last[-1][0] == t):  # re-read, same sample
                    self.add(name, v.volume_l, t)

    async def run(self) -> None:
        async for u in self.engine.subscribe(maxsize=1000):
            self.on_update(u)
