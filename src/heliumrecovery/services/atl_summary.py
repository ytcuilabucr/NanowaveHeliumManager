"""Turn the raw ATL CSV log into Notion-ready rows.

- `minute_rows`: 1-minute averages per unit (the "ATL Log" database)
- `latest_rows`: newest reading per unit (the "ATL Live Status" database)

Row dicts use the Notion property names of those databases.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import fmean

from ..core.engine import Instrument
from ..core.workers import ATL_MODELS
from .csv_logger import ATL_FIELDS

# CSV field -> Notion property
NUMERIC = {
    "diode_temp": "Diode T (K)",
    "dewar_pressure": "Dewar P (psi)",
    "input_gas_flow": "Gas flow (L/min)",
    "liquid_volume": "Liquid vol (L)",
    "liquefaction_rate": "Liq. rate (L/day)",
    "heater_power": "Heater (%)",
}
WARM_K = 50.0  # diode above this: the unit is not cold


def read_log(folder: Path | str) -> list[dict]:
    """All rows of every atl_*.csv in `folder`, oldest first, with parsed values."""
    rows = []
    for path in sorted(Path(folder).glob("atl_*.csv")):
        with path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rec = {"time": datetime.fromisoformat(r["time_utc"]), "unit": r["unit"]}
                for k in ATL_FIELDS:
                    rec[k] = float(r[k]) if r.get(k) not in (None, "") else None
                rows.append(rec)
    rows.sort(key=lambda r: r["time"])
    return rows


def _round(v: float | None, nd: int = 3) -> float | None:
    return None if v is None else round(v, nd)


def minute_rows(rows: list[dict], *, include_last_minute: bool = False) -> list[dict]:
    """Average readings per (unit, minute). The newest, still-filling minute is dropped."""
    buckets: dict[tuple[str, datetime], list[dict]] = defaultdict(list)
    for r in rows:
        minute = r["time"].astimezone().replace(second=0, microsecond=0)
        buckets[(r["unit"], minute)].append(r)
    if buckets and not include_last_minute:
        last = max(m for _, m in buckets)
        buckets = {k: v for k, v in buckets.items() if k[1] != last}
    out = []
    for (unit, minute), group in sorted(buckets.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        row = {
            "Sample": f"{unit} {minute:%m-%d %H:%M}",
            "Unit": unit,
            "date:Time:start": minute.isoformat(),
            "date:Time:is_datetime": 1,
            "Minute": f"{minute:%m-%d %H:%M}",  # text x-axis for Notion charts; sorts correctly across days
            "Readings": len(group),
        }
        for field, prop in NUMERIC.items():
            vals = [g[field] for g in group if g[field] is not None]
            row[prop] = _round(fmean(vals)) if vals else None
        out.append(row)
    return out


def latest_rows(rows: list[dict]) -> list[dict]:
    latest: dict[str, dict] = {}
    for r in rows:
        latest[r["unit"]] = r
    out = []
    for unit in sorted(latest):
        r = latest[unit]
        mode = r["liquid_mode"]
        row = {
            "Unit": unit,
            "State": "Warm / off" if (r["diode_temp"] or 0) > WARM_K else "Cold",
            "Capacity (L)": r["dewar_capacity"],
            "Liquid mode": ATL_MODELS[Instrument(unit.lower())].mode_text(mode, r.get("transfer_mode"))
            if mode is not None else None,
            "date:Last update:start": r["time"].astimezone().isoformat(timespec="seconds"),
            "date:Last update:is_datetime": 1,
        }
        for field, prop in NUMERIC.items():
            row[prop] = _round(r[field])
        out.append(row)
    return out
