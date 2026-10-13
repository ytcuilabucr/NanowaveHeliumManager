"""Labels and formatting used by the console UI (app.py, diagram.py)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from ..core.engine import Instrument
from ..core.workers import ATL_MODELS
from ..drivers.mphub import PumpSettings

SETTING_FIELDS = [
    ("inlet_pressure_setpoint", "Inlet Pressure Setpoint (mbar)"),
    ("comp_on_setpoint", "Comp on Setpoint (mbar)"),
    ("comp_off_setpoint", "Comp off Setpoint (mbar)"),
    ("max_outlet_pressure", "Max Outlet Pressure (mbar)"),
    ("pressure_setpoint", "Pressure Setpoint (mbar)"),
    ("hiflow_onset", "HiFlow Onset"),
    ("bp_valve_p_gain", "BP Valve P Gain"),
    ("bp_valve_i_gain", "BP Valve I Gain"),
    ("bp_valve_d_gain", "BP Valve D Gain"),
]


def fmt(v: Any, digits: int = 3) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "ON" if v else "OFF"
    if isinstance(v, float):
        return "NaN" if math.isnan(v) else f"{v:.{digits}f}"
    if isinstance(v, datetime):
        return v.astimezone().strftime("%H:%M:%S")
    return str(v)


def liquid_mode_name(slot: Instrument, mode: float, transfer_mode: float | None = None) -> str:
    """ATL liquefaction mode by that unit's model (ATL3: code 2 is "Liquefy")."""
    return ATL_MODELS[slot].mode_text(mode, transfer_mode)


def describe_settings(s: PumpSettings) -> str:
    lines = [f"Enabled: {'yes' if s.enabled else 'NO'}"]
    lines += [f"{title}: {getattr(s, key):g}" for key, title in SETTING_FIELDS]
    return "\n".join(lines)


def fmt_age(age: timedelta | None) -> str:
    """Compact age of a reading: 45 s, 12 min, 3.5 h, 36 d."""
    if age is None:
        return "—"
    s = age.total_seconds()
    if s < 90:
        return f"{s:.0f} s"
    if s < 90 * 60:
        return f"{s / 60:.0f} min"
    if s < 48 * 3600:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.0f} d"
