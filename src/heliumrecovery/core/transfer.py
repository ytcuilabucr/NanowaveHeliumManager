"""Transfer Control and pressure alarms.

- Preset selection: copy ``presets[idx]`` into the settings to set.
- SET: write the settings to set.
- Step 1: write the settings to set with Enabled = False and arm the auto re-enable.
- Step 2: after the countdown, re-write the pump's *current* settings with
  Enabled = False.
- Step 3: write ``presets[0]`` (normal operation).
- Auto re-enable: while armed, if inlet pressure > 1.38 bar, write the settings
  to set with Enabled = True and disarm.
- Over-limit indicators: inlet >= 1.4 bar, tank >= 1.35 bar.
- BP Valve Tuning "Set": write the BP-valve tuning settings.

The console's Step dialogs use `step1_start` (preset -> pump disabled -> armed),
`disable_now` (Step 2 after the on-screen countdown) and `step3`.

The auto re-enable check runs on every pump reading, and every write goes through
`PumpWorker.set_settings` so it is validated, read back, and refused unless
pump writes are enabled.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from ..drivers.mphub import PumpData, PumpSettings
from .engine import DataEngine, Instrument
from .workers import PumpWorker

log = logging.getLogger(__name__)


def _preset(enabled: bool, inlet: float, comp_on: float, comp_off: float, setpoint: float) -> PumpSettings:
    return PumpSettings(
        enabled=enabled,
        inlet_pressure_setpoint=inlet,
        comp_on_setpoint=comp_on,
        comp_off_setpoint=comp_off,
        max_outlet_pressure=8000,
        pressure_setpoint=setpoint,
        hiflow_onset=100,
        bp_valve_p_gain=1000,
        bp_valve_i_gain=20,
        bp_valve_d_gain=40,
    )


# Pump setting presets. Index 0 is the normal operating preset that Step 3 restores.
DEFAULT_PRESETS: tuple[PumpSettings, ...] = (
    _preset(True, 1300, 1270, 800, 790),
    _preset(False, 1250, 1100, 450, 350),
    _preset(False, 1250, 800, 450, 350),
)


@dataclass(frozen=True)
class Limits:
    inlet_overlimit_bar: float = 1.4
    tank_overlimit_bar: float = 1.35
    auto_enable_inlet_bar: float = 1.38


@dataclass(frozen=True)
class Alarms:
    inlet_overlimit: bool = False
    tank_overlimit: bool = False


def check_alarms(data: PumpData, limits: Limits = Limits()) -> Alarms:
    return Alarms(
        inlet_overlimit=data.inlet_pressure >= limits.inlet_overlimit_bar,
        tank_overlimit=data.tank_pressure >= limits.tank_overlimit_bar,
    )


class TransferController:
    """Holds the Transfer tab state and issues pump writes through the worker."""

    def __init__(
        self,
        pump: PumpWorker,
        engine: DataEngine,
        *,
        presets: tuple[PumpSettings, ...] = DEFAULT_PRESETS,
        limits: Limits = Limits(),
        step2_delay: float = 5.0,
    ) -> None:
        if not presets:
            raise ValueError("need at least one preset")
        self.pump, self.engine, self.limits = pump, engine, limits
        self.presets = list(presets)
        self.preset_index = 0
        self.to_set: PumpSettings = self.presets[0]  # "Pump Settings to set"
        self.step2_delay = step2_delay
        self.auto_enable_armed = False
        self.auto_enable_future: asyncio.Future | None = None  # the re-enable write, once fired
        self.armed_since: float | None = None  # UTC timestamp Step 1 started waiting (Slack alert)
        self.transfer_start = None  # operation_log.TransferStart of the last real Step 1 (Step 3 log form)
        self.sim_transfer_start = None  # same for the Internal-tab simulation
        self.dialog_open = 0  # real transfer dialogs open (pump_disabled_long alert waits meanwhile)
        self.alarms = Alarms()

    def select_preset(self, index: int) -> PumpSettings:
        self.preset_index = index
        self.to_set = self.presets[index]
        return self.to_set

    async def set_pars(self) -> PumpSettings:
        return await self.pump.set_settings(self.to_set)

    async def step1(self) -> PumpSettings:
        return await self.pump.set_settings(self.to_set.model_copy(update={"enabled": False}))

    async def step1_start(self, preset_index: int) -> PumpSettings:
        """Step 1 dialog: write preset `preset_index` with the pump disabled, then arm
        the auto re-enable (the same preset with Enabled=True once inlet > onset)."""
        self.auto_enable_armed = False
        self.auto_enable_future = None
        self.select_preset(preset_index)
        readback = await self.step1()
        self.auto_enable_armed = True
        self.armed_since = time.time()
        log.warning("transfer step 1: preset %d written disabled; armed for inlet > %.2f bar",
                    preset_index, self.limits.auto_enable_inlet_bar)
        return readback

    def disarm(self) -> None:
        if self.auto_enable_armed:
            log.warning("transfer: auto re-enable cancelled; the pump stays disabled")
        self.auto_enable_armed = False

    async def disable_now(self) -> PumpSettings:
        """Re-write the pump's *current* settings with Enabled = False (Step 2 action)."""
        current = self.engine.read().get(Instrument.PUMP_SETTINGS)
        if current is None:
            raise RuntimeError("pump settings not read yet")
        return await self.pump.set_settings(current.model_copy(update={"enabled": False}))

    async def step2(self) -> PumpSettings:
        await asyncio.sleep(self.step2_delay)
        return await self.disable_now()

    async def step3(self) -> PumpSettings:
        self.select_preset(0)
        return await self.pump.set_settings(self.presets[0])

    async def apply(self, settings: PumpSettings) -> PumpSettings:
        """BP Valve Tuning tab "Set"."""
        return await self.pump.set_settings(settings)

    def on_pump_data(self, data: PumpData) -> None:
        """Called for every new pump reading: alarms and armed auto re-enable."""
        self.alarms = check_alarms(data, self.limits)
        if self.auto_enable_armed and data.inlet_pressure > self.limits.auto_enable_inlet_bar:
            self.auto_enable_armed = False
            log.warning(
                "inlet %.3f bar > %.2f: auto re-enabling pump",
                data.inlet_pressure,
                self.limits.auto_enable_inlet_bar,
            )
            self.auto_enable_future = self.pump.submit_settings_nowait(
                self.to_set.model_copy(update={"enabled": True})
            )

    async def run(self) -> None:
        """Follow pump readings from the engine."""
        async for update in self.engine.subscribe():
            if update.slot is Instrument.PUMP_DATA and update.link.connected:
                self.on_pump_data(update.value)
