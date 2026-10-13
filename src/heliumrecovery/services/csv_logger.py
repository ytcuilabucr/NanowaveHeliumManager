"""CSV data logger: one file per day, one row per instrument reading.

Writing into a Google Drive for desktop folder (e.g. ``G:\\My Drive\\...``)
makes the log land in Google Drive without any API credentials.
"""

from __future__ import annotations

import csv
import logging
import math
from datetime import datetime
from pathlib import Path

from ..core.engine import DataEngine, Instrument
from ..drivers.atl import ATLStatus

log = logging.getLogger(__name__)

ATL_SLOTS = (Instrument.ATL1, Instrument.ATL2, Instrument.ATL3)
ATL_FIELDS = [
    "diode_temp",
    "dewar_pressure",
    "input_gas_flow",
    "liquid_volume",
    "liquefaction_rate",
    "status_machine",
    "liquid_mode",
    "transfer_mode",
    "status_leds",
    "dewar_capacity",
    "heater_power",
]
ATL_HEADER = ["time_utc", "time_local", "unit", *ATL_FIELDS]


def atl_row(slot: Instrument, s: ATLStatus) -> list[str]:
    t = s.last_update or datetime.now().astimezone()
    values = ["" if math.isnan(v := getattr(s, f)) else f"{v:.6g}" for f in ATL_FIELDS]
    return [t.isoformat(timespec="seconds"), t.astimezone().strftime("%Y-%m-%d %H:%M:%S"), slot.value.upper(), *values]


class ATLCsvLogger:
    """Appends every successful ATL reading to ``<folder>/atl_<YYYY-MM-DD>.csv``."""

    def __init__(self, engine: DataEngine, folder: Path | str) -> None:
        self.engine, self.folder = engine, Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.rows_written = 0

    def path_for(self, day: datetime) -> Path:
        return self.folder / f"atl_{day.astimezone():%Y-%m-%d}.csv"

    def write(self, slot: Instrument, status: ATLStatus) -> Path:
        path = self.path_for(status.last_update or datetime.now().astimezone())
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(ATL_HEADER)
            w.writerow(atl_row(slot, status))
        self.rows_written += 1
        return path

    async def run(self) -> None:
        async for u in self.engine.subscribe(maxsize=1000):
            if u.slot in ATL_SLOTS and u.link.connected:
                try:
                    self.write(u.slot, u.value)
                except OSError as exc:  # Drive folder briefly locked/offline: skip, keep going
                    log.warning("csv write failed: %s", exc)
