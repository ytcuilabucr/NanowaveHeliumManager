"""MP Hub helium recovery pump driver (Unitronics PLC over PCOM ASCII/TCP).

Reads live data and settings, writes settings and the enable flag.

Register map
------------
Live data: one read of ML5..ML15 (operand ML, start 5, quantity 11).

=================  ======  ==========  =======  ======
Field              Reg     Array idx   Scale    Unit
=================  ======  ==========  =======  ======
tank_pressure      ML5     0           / 1e5    bar
outlet_pressure    ML7     2           / 1e5    bar
oil_temperature    ML9     4           / 10     (as PLC)
outlet_tank_level  ML10    5           / 10     (as PLC)
helium_flow_in     ML12    7           / 10     (as PLC)
inlet_pressure     ML15    10          / 1e5    bar
user data          MI<n>   -           raw      -
=================  ======  ==========  =======  ======

Settings (each MI read/written one register at a time, in this order; then MB11):

=========================  ======  =================
Field                      Reg     Default
=========================  ======  =================
inlet_pressure_setpoint    MI1900  1270 mbar
comp_on_setpoint           MI18    1320 mbar
comp_off_setpoint          MI17    800 mbar
max_outlet_pressure        MI37    8000 mbar
pressure_setpoint          MI1967  790 mbar
hiflow_onset               MI1969  100
bp_valve_p_gain            MI1902  1000
bp_valve_i_gain            MI1903  20
bp_valve_d_gain            MI1904  40
enabled                    MB11    (0/1)
=========================  ======  =================
"""

from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .base import DriverError, now
from .pcom import Operand, PcomClient

LIVE_START = 5
LIVE_QTY = 11
# field -> (index into the ML5..ML15 block, divisor)
LIVE_MAP: dict[str, tuple[int, float]] = {
    "tank_pressure": (0, 1e5),
    "outlet_pressure": (2, 1e5),
    "oil_temperature": (4, 10.0),
    "outlet_tank_level": (5, 10.0),
    "helium_flow_in": (7, 10.0),
    "inlet_pressure": (10, 1e5),
}
ENABLE_BIT = 11
# field -> MI address, in write order
SETTINGS_MAP: dict[str, int] = {
    "inlet_pressure_setpoint": 1900,
    "comp_on_setpoint": 18,
    "comp_off_setpoint": 17,
    "max_outlet_pressure": 37,
    "pressure_setpoint": 1967,
    "hiflow_onset": 1969,
    "bp_valve_p_gain": 1902,
    "bp_valve_i_gain": 1903,
    "bp_valve_d_gain": 1904,
}
MI_WRITE_MAX = 0x7FFF  # MI is a signed 16-bit register; only non-negative values are allowed


class UserDatum(BaseModel):
    index: int
    value: float


class PumpData(BaseModel):
    """Live pump readings (pressures in bar)."""

    inlet_pressure: float
    tank_pressure: float
    outlet_pressure: float
    outlet_tank_level: float
    oil_temperature: float
    helium_flow_in: float
    user_data: list[UserDatum] = Field(default_factory=list)
    last_update: datetime


class PumpSettings(BaseModel):
    """Pump setpoints (raw PLC integer units; pressures in mbar)."""

    model_config = ConfigDict(allow_inf_nan=False)

    enabled: bool
    inlet_pressure_setpoint: float = Field(ge=0)
    comp_on_setpoint: float = Field(ge=0)
    comp_off_setpoint: float = Field(ge=0)
    max_outlet_pressure: float = Field(ge=0)
    pressure_setpoint: float = Field(ge=0)
    hiflow_onset: float = Field(ge=0)
    bp_valve_p_gain: float = Field(ge=0)
    bp_valve_i_gain: float = Field(ge=0)
    bp_valve_d_gain: float = Field(ge=0)


def settings_to_registers(settings: PumpSettings) -> list[tuple[int, int]]:
    """Validate and round settings to (MI address, value) pairs in write order.

    Rounding is round-half-to-even.
    """
    out: list[tuple[int, int]] = []
    for name, addr in SETTINGS_MAP.items():
        raw = getattr(settings, name)
        if not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raise DriverError(f"{name}={raw!r} is not a finite number")
        value = round(raw)
        if not 0 <= value <= MI_WRITE_MAX:
            raise DriverError(f"{name}={raw!r} out of range 0..{MI_WRITE_MAX}")
        out.append((addr, value))
    return out


class MPHub:
    """MP Hub helium recovery pump. Writes require ``allow_writes=True``."""

    def __init__(
        self,
        host: str,
        port: int = 502,
        unit_id: int = 0,
        allow_writes: bool = False,
        timeout: float = 1.0,
        *,
        uppercase: bool = True,
        mod255: bool = False,
    ) -> None:
        self.allow_writes = allow_writes
        self.client = PcomClient(
            host, port, unit_id=unit_id, timeout=timeout, uppercase=uppercase, mod255=mod255
        )

    async def connect(self) -> None:
        await self.client.connect()

    async def close(self) -> None:
        await self.client.close()

    async def __aenter__(self) -> MPHub:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def read_data(self, user_indices: tuple[int, ...] | list[int] = ()) -> PumpData:
        """Read live data (GetPumpData) plus optional raw MI user registers."""
        raw = await self.client.read(Operand.ML, LIVE_START, LIVE_QTY)
        fields = {name: raw[idx] / div for name, (idx, div) in LIVE_MAP.items()}
        user = []
        for idx in user_indices:
            (value,) = await self.client.read(Operand.MI, idx, 1)
            user.append(UserDatum(index=idx, value=value))
        return PumpData(**fields, user_data=user, last_update=now())

    async def read_settings(self) -> PumpSettings:
        """Read MB11 and the setpoint MIs (GetPumpSettings)."""
        (enabled,) = await self.client.read(Operand.MB, ENABLE_BIT, 1)
        values = {}
        for name, addr in SETTINGS_MAP.items():
            (values[name],) = await self.client.read(Operand.MI, addr, 1)
        return PumpSettings(enabled=enabled != 0, **values)

    def _require_writes(self) -> None:
        if not self.allow_writes:
            raise DriverError("MPHub writes are disabled; construct with allow_writes=True")

    async def write_settings(self, settings: PumpSettings) -> PumpSettings:
        """Write all setpoints then MB11 (SetPumpPars); return the read-back settings."""
        self._require_writes()
        registers = settings_to_registers(settings)
        for addr, value in registers:
            await self.client.write(Operand.MI, addr, [value])
        await self.client.write(Operand.MB, ENABLE_BIT, [1 if settings.enabled else 0])
        return await self.read_settings()

    async def set_enabled(self, enabled: bool) -> None:
        """Write MB11 only (Enablepump)."""
        self._require_writes()
        await self.client.write(Operand.MB, ENABLE_BIT, [1 if enabled else 0])
