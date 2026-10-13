"""Google Sheets logging via a Google Apps Script web app.

Every field of every instrument becomes one row (Instr, pName, pValue, unit), sent
as query parameters ``Instr{i}``, ``pName{i}``, ``pValue{i}``, ``unit{i}`` on a
POST with an empty body. Parameter names are the sheet's existing column labels
(including the "Diorde T" typo) so the existing sheet keeps working.

The script URL is a secret: set ``HR_SHEETS_URL``. Without it the uploader is off.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel

from ..core.engine import DataEngine, Instrument, Snapshot
from ..core.errors import ErrorHandler, ErrorKind

log = logging.getLogger(__name__)

# Python field -> sheet column label, in sheet column order.
SHEET_LABELS: dict[Instrument, dict[str, str]] = {
    Instrument.JANIS: {
        "lhe_level_in": "LHe Level",
        "lhe_volume_l": "LHe Volume",
        "last_update": "Last Update Time",
    },
    Instrument.ATL1: {
        "diode_temp": "Diorde T",
        "dewar_pressure": "Dewar Pres",
        "input_gas_flow": "Input Gas Flow",
        "liquid_vol": "Liquid Vol",
        "liquefaction_rate": "LiQ Rate",
        "status_machine": "Status Machine",
        "liquid_mode": "Liquid Mode",
        "transfer_mode": "Transfer Mode",
        "status_leds": "Status LEDs",
        "dewar_capacity": "Dewar Capacity",
        "liquid_volume": "Liquid Volume",
        "heater_power": "Heater Power",
        "last_update": "Last Update Time",
    },
    Instrument.PUMP_DATA: {
        "inlet_pressure": "Inlet Pressure",
        "tank_pressure": "Tank Pressure",
        "outlet_pressure": "Outlet Pressure",
        "outlet_tank_level": "Outlet Tank Level",
        "oil_temperature": "Oil Temperature",
        "helium_flow_in": "Helium Flow In",
        "user_data": "User Data",
        "last_update": "Last Update Time",
    },
    Instrument.PUMP_SETTINGS: {
        "enabled": "Enabled?",
        "inlet_pressure_setpoint": "Inlet Pressure Setpoint",
        "comp_on_setpoint": "Comp on Setpoint",
        "comp_off_setpoint": "Comp off Setpoint",
        "max_outlet_pressure": "Max Outlet Pressure",
        "pressure_setpoint": "Pressure Setpoint",
        "hiflow_onset": "HiFlow Onset",
        "bp_valve_p_gain": "BP Valve P Gain",
        "bp_valve_i_gain": "BP Valve I Gain",
        "bp_valve_d_gain": "BP Valve D Gain",
    },
}
SHEET_LABELS[Instrument.ATL2] = SHEET_LABELS[Instrument.ATL3] = SHEET_LABELS[Instrument.ATL1]

# Unit per label, checked against the headers in the production "CurrentData" sheet
# (e.g. "HiFlow Onset (Lpm)", "Status Machine").
UNITS: dict[str, str] = {
    "Diorde T": "K",
    "Oil Temperature": "deg C",
    "Dewar Pres": "psi",
    "Helium Flow In": "Lpm",
    "Input Gas Flow": "Lpm",
    "Dewar Capacity": "L",
    "Liquid Vol": "L",
    "Liquid Volume": "L",
    "LHe Volume": "L",
    "LiQ Rate": "L/Day",
    "Heater Power": "%",
    "Outlet Tank Level": "%",
    "Inlet Pressure": "bar",
    "Tank Pressure": "bar",
    "Outlet Pressure": "bar",
    "Inlet Pressure Setpoint": "mbar",
    "Comp on Setpoint": "mbar",
    "Comp off Setpoint": "mbar",
    "Max Outlet Pressure": "mbar",
    "Pressure Setpoint": "mbar",
    "HiFlow Onset": "Lpm",
    "Enabled?": "",
    "User Data": "",
    "Status Machine": "",
    "Liquid Mode": "",
    "Transfer Mode": "",
    "Status LEDs": "",
    "BP Valve P Gain": "",
    "BP Valve I Gain": "",
    "BP Valve D Gain": "",
    "LHe Level": "in",
    "Last Update Time": "",
}

# Instrument names used in the sheet, in upload order.
SHEET_NAMES: dict[Instrument, str] = {
    Instrument.JANIS: "Janis",
    Instrument.ATL1: "ATL1",
    Instrument.ATL2: "ATL2",
    Instrument.ATL3: "ATL3",
    Instrument.PUMP_DATA: "MPHUB",
    Instrument.PUMP_SETTINGS: "MPHUB",
}


@dataclass(frozen=True)
class Row:
    instr: str
    name: str
    unit: str
    value: str


def format_value(value: Any) -> str:
    """Value as text in the format the sheets expect."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.astimezone().strftime("%Y/%m/%d %H:%M:%S")
    if isinstance(value, float):
        return "NaN" if math.isnan(value) else format(value, ".6f")  # %f
    if isinstance(value, list):  # pump "User Data": 0 when empty
        return ";".join(f"{d.index}:{d.value:g}" for d in value) or "0"
    return str(value)


def model_rows(slot: Instrument, model: BaseModel) -> list[Row]:
    instr = SHEET_NAMES[slot]
    rows = []
    for field, label in SHEET_LABELS[slot].items():
        value = getattr(model, field)
        if value is None:
            continue
        rows.append(Row(instr, label, UNITS.get(label, "?"), format_value(value)))
    return rows


def snapshot_rows(snap: Snapshot) -> list[Row]:
    """Rows for every slot that currently has a value."""
    rows: list[Row] = []
    for slot in SHEET_NAMES:
        if (model := snap.get(slot)) is not None:
            rows.extend(model_rows(slot, model))
    return rows


def query_params(rows: list[Row]) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    for i, r in enumerate(rows):
        params += [(f"Instr{i}", r.instr), (f"pName{i}", r.name), (f"pValue{i}", r.value), (f"unit{i}", r.unit)]
    return params


class SheetsUploader:
    """Posts all current values to the Apps Script every `interval` seconds."""

    def __init__(
        self,
        url: str,
        engine: DataEngine,
        errors: ErrorHandler,
        *,
        client: httpx.AsyncClient,
        interval: float = 53.0,
        timeout: float = 30.0,
    ) -> None:
        self.url, self.engine, self.errors, self.client = url, engine, errors, client
        self.interval, self.timeout = interval, timeout
        self.last_upload: datetime | None = None
        self.uploading = False

    async def upload_once(self) -> bool:
        rows = snapshot_rows(self.engine.read())
        if len(rows) <= 1:  # arrays of size <= 1 are skipped
            return False
        self.uploading = True
        try:
            resp = await self.client.post(
                self.url, params=query_params(rows), timeout=self.timeout, follow_redirects=True
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            self.errors.set(ErrorKind.HTTP_POST, f"sheets upload: {exc}", source="sheets")
            log.warning("sheets upload failed: %s", exc)
            return False
        finally:
            self.uploading = False
        self.errors.clear(ErrorKind.HTTP_POST)
        self.last_upload = datetime.now().astimezone()
        return True

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self.upload_once()


EXTERNAL_SLOTS = (Instrument.CMI, Instrument.WEI)


def external_rows(snap: Snapshot) -> list[Row]:
    """CMI / Wei Cryostat rows, re-emitted with their source tab name and headers.

    Only for our own status_now mirror: production CurrentData already holds them.
    """
    rows: list[Row] = []
    for slot in EXTERNAL_SLOTS:
        model = snap.get(slot)
        if model is None:
            continue
        for header, value in model.raw.items():
            m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", header)
            name, unit = (m.group(1), m.group(2)) if m else (header, "")
            text = value if isinstance(value, str) else format_value(value)
            rows.append(Row(model.name, name, unit, text))
    return rows


def valve_rows(snap: Snapshot) -> list[Row]:
    """ATL3 inlet valve as two extra ATL3 columns (our status_now mirror only)."""
    v = snap.get(Instrument.VALVE)
    if v is None:
        return []
    number = v.status_number
    return [
        Row("ATL3", "Inlet Valve Switch", "", format_value(v.switch_on)),
        Row("ATL3", "Inlet Valve Status", "", "" if number is None else str(number)),
    ]
