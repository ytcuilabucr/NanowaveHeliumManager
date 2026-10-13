"""Acquisition loops: one asyncio task per instrument loop.

- `PumpWorker`: the queue-driven pump loop (action queue with
  TCP connect / TCP disconnect / Read pump data / Read pump settings /
  Set pump settings; a dequeue timeout triggers a data read).
- `ATLPoller`: the ATL state machine, all three units read concurrently.
- `JanisPoller`: the Janis LHe level loop.

Loops never raise on instrument errors: they record them in the
`ErrorHandler`, flag the slot in the `DataEngine` and keep going.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..drivers.atl import (
    ATL80, ATL_LIQUEFY_ONLY, COMPRESSOR_COOLDOWN_S, ATLClient, ATLModel, ATLStatus, LiquidMode, read_all,
)
from ..drivers.base import DriverError, now
from ..drivers.janis import JanisClient
from ..drivers.mphub import MPHub, PumpSettings
from ..drivers.sheet_cryostat import SheetTabClient
from ..drivers.tuya_valve import TuyaValve, ValveStatus
from .engine import DataEngine, Instrument
from .errors import ErrorHandler, ErrorKind

log = logging.getLogger(__name__)


class PumpAction(StrEnum):
    """Actions the pump loop can be asked to do."""

    CONNECT = "TCP connect"
    DISCONNECT = "TCP disconnect"
    READ_DATA = "Read pump data"
    READ_SETTINGS = "Read pump settings"
    SET_SETTINGS = "Set pump settings"


@dataclass
class PumpCommand:
    action: PumpAction
    settings: PumpSettings | None = None
    done: asyncio.Future[Any] | None = field(default=None, repr=False)


class PumpWorker:
    """Owns the MP Hub connection; everything that talks to the pump goes through here."""

    def __init__(
        self,
        hub: MPHub,
        engine: DataEngine,
        errors: ErrorHandler,
        *,
        interval: float = 0.2,
        reconnect_delay: float = 5.0,
        user_indices: tuple[int, ...] = (),
        settings_interval: float = 60.0,
    ) -> None:
        self.hub, self.engine, self.errors = hub, engine, errors
        self.interval, self.reconnect_delay = interval, reconnect_delay
        self.settings_interval = settings_interval  # re-read settings so PLC-panel changes are seen
        self.last_written: PumpSettings | None = None  # read-back of this program's last write
        self.on_write_failed: Callable[[str, str], None] | None = None  # (device, text) -> Slack alert
        self._settings_read_at = 0.0
        self.user_indices = user_indices
        self.connected = False
        self.last_duration: float | None = None  # s, most recent READ_DATA exchange
        self._queue: asyncio.Queue[PumpCommand] = asyncio.Queue()

    def submit(self, action: PumpAction, settings: PumpSettings | None = None) -> asyncio.Future[Any]:
        """Queue a command; the returned future resolves with its result or error."""
        fut = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(PumpCommand(action, settings, fut))
        return fut

    async def set_settings(self, settings: PumpSettings) -> PumpSettings:
        """Write setpoints and return the read-back values (requires allow_writes)."""
        return await self.submit(PumpAction.SET_SETTINGS, settings)

    def submit_settings_nowait(self, settings: PumpSettings) -> asyncio.Future[Any]:
        """Queue a settings write from synchronous code; failures are logged."""
        fut = self.submit(PumpAction.SET_SETTINGS, settings)
        fut.add_done_callback(
            lambda f: f.cancelled() or f.exception() is None or log.error("queued write failed: %s", f.exception())
        )
        return fut

    async def run(self) -> None:
        self._queue.put_nowait(PumpCommand(PumpAction.CONNECT))
        try:
            while True:
                try:
                    cmd = await asyncio.wait_for(self._queue.get(), self.interval)
                except TimeoutError:
                    if not self.connected:
                        continue
                    t = asyncio.get_running_loop().time()
                    if t - self._settings_read_at >= self.settings_interval:
                        cmd = PumpCommand(PumpAction.READ_SETTINGS)
                    else:
                        cmd = PumpCommand(PumpAction.READ_DATA)
                await self._execute(cmd)
        finally:
            await self.hub.close()
            self.connected = False

    async def _execute(self, cmd: PumpCommand) -> None:
        t0 = time.perf_counter()
        try:
            result = await self._dispatch(cmd)
        except DriverError as exc:
            self._failed(cmd, exc)
            if cmd.done and not cmd.done.done():
                cmd.done.set_exception(exc)
        else:
            if cmd.done and not cmd.done.done():
                cmd.done.set_result(result)
        finally:
            if cmd.action is PumpAction.READ_DATA:
                self.last_duration = time.perf_counter() - t0

    async def _dispatch(self, cmd: PumpCommand) -> Any:
        match cmd.action:
            case PumpAction.CONNECT:
                await self.hub.connect()
                self.connected = True
                self._queue.put_nowait(PumpCommand(PumpAction.READ_SETTINGS))
            case PumpAction.DISCONNECT:
                await self.hub.close()
                self.connected = False
                self.engine.mark_disconnected(Instrument.PUMP_DATA)
                self.engine.mark_disconnected(Instrument.PUMP_SETTINGS)
            case PumpAction.READ_DATA:
                data = await self.hub.read_data(self.user_indices)
                self.engine.update(Instrument.PUMP_DATA, data)
                self.errors.clear(ErrorKind.TCP_READ)
                return data
            case PumpAction.READ_SETTINGS:
                self._settings_read_at = asyncio.get_running_loop().time()
                settings = await self.hub.read_settings()
                self.engine.update(Instrument.PUMP_SETTINGS, settings)
                self.errors.clear(ErrorKind.TCP_READ)
                return settings
            case PumpAction.SET_SETTINGS:
                if cmd.settings is None:
                    raise DriverError("SET_SETTINGS needs settings")
                readback = await self.hub.write_settings(cmd.settings)
                self.last_written = readback
                self.engine.update(Instrument.PUMP_SETTINGS, readback)
                log.info("pump settings written: %s", readback.model_dump())
                return readback

    def _failed(self, cmd: PumpCommand, exc: DriverError) -> None:
        log.warning("pump %s failed: %s", cmd.action, exc)
        self.errors.set(ErrorKind.TCP_READ, f"{cmd.action}: {exc}", source="pump")
        slot = Instrument.PUMP_SETTINGS if cmd.action is PumpAction.SET_SETTINGS else Instrument.PUMP_DATA
        self.engine.mark_error(slot, exc)
        if cmd.action is PumpAction.SET_SETTINGS and self.on_write_failed:
            self.on_write_failed("pump", f"pump settings: {exc}")
        if cmd.action is PumpAction.CONNECT:
            self.connected = False
            asyncio.get_running_loop().call_later(
                self.reconnect_delay, self._queue.put_nowait, PumpCommand(PumpAction.CONNECT)
            )


ATL_SLOTS = (Instrument.ATL1, Instrument.ATL2, Instrument.ATL3)
# Same web page on all three, different models: ATL3 only liquefies (code 2, shown as "Liquefy").
ATL_MODELS: dict[Instrument, ATLModel] = {
    Instrument.ATL1: ATL80, Instrument.ATL2: ATL80, Instrument.ATL3: ATL_LIQUEFY_ONLY,
}


class ATLPoller:
    """Reads all ATL units every `interval` seconds into slots ATL1..ATL3."""

    def __init__(
        self,
        clients: Mapping[Instrument, ATLClient],
        engine: DataEngine,
        errors: ErrorHandler,
        *,
        interval: float = 10.0,
    ) -> None:
        self.clients, self.engine, self.errors, self.interval = clients, engine, errors, interval
        self.last_duration: float | None = None  # s, most recent poll_once
        self._mode_locks = {slot: asyncio.Lock() for slot in clients}
        self.requested: dict[Instrument, tuple[int, float]] = {}  # slot -> (mode, UTC timestamp) asked by us
        self.on_write_failed: Callable[[str, str], None] | None = None
        self.cooldown_s = COMPRESSOR_COOLDOWN_S
        self.stopped_at: dict[Instrument, float] = {}  # slot -> monotonic time it was seen/set to Stop
        self.clock: Callable[[], float] = time.monotonic

    def cooldown_left(self, slot: Instrument) -> float:
        """Seconds until the unit's compressor may be started again after a stop (0 = now)."""
        t = self.stopped_at.get(slot)
        return 0.0 if t is None else max(0.0, self.cooldown_s - (self.clock() - t))

    def _note_mode(self, slot: Instrument, status: ATLStatus) -> None:
        """Start the cool-down when a unit goes from a running mode to Stop (by us or at the unit)."""
        prev = self.engine.read().get(slot)
        try:
            was, now_mode = int(prev.liquid_mode) if prev is not None else None, int(status.liquid_mode)
        except (TypeError, ValueError):
            return
        if now_mode == LiquidMode.STOP and was not in (None, LiquidMode.STOP):
            self.stopped_at[slot] = self.clock()

    async def set_mode(self, slot: Instrument, mode: LiquidMode | int) -> ATLStatus:
        """Switch one unit's liquefaction mode; returns the read-back status (raises on failure).

        One change per unit at a time; the read-back status goes into the engine.
        """
        if slot not in self.clients:
            raise DriverError(f"no ATL configured for {slot}")
        async with self._mode_locks[slot]:
            model = self.clients[slot].model
            left = self.cooldown_left(slot)
            if int(mode) != LiquidMode.STOP and left > 0:
                raise DriverError(f"{str(slot).upper()}: compressor still cooling after the stop, "
                                  f"wait {left:.0f} s before starting it again")
            log.warning("%s: liquid mode change to %s requested", slot, model.mode_text(int(mode)))
            self.requested[slot] = (int(mode), now().timestamp())
            try:
                status = await self.clients[slot].set_liquid_mode(mode)
            except DriverError as exc:
                self.errors.set(ErrorKind.HTTP_POST, f"{slot} mode: {exc}", source=str(slot))
                if self.on_write_failed:
                    self.on_write_failed(str(slot), f"{str(slot).upper()} mode → {model.mode_text(int(mode))}: {exc}")
                raise
            self._note_mode(slot, status)
            if int(mode) == LiquidMode.STOP:
                self.stopped_at[slot] = self.clock()
            self.engine.update(slot, status)
            log.warning("%s: liquid mode is now %s", slot, model.mode_text(status.liquid_mode))
            return status

    async def poll_once(self) -> None:
        """One read; its duration (success or failure) is kept in `last_duration`."""
        t0 = time.perf_counter()
        try:
            await self._poll()
        finally:
            self.last_duration = time.perf_counter() - t0

    async def _poll(self) -> None:
        results = await read_all({str(slot): c for slot, c in self.clients.items()})
        failed = False
        for slot in self.clients:
            res = results[str(slot)]
            if isinstance(res, DriverError):
                failed = True
                self.engine.mark_error(slot, res)
                self.errors.set(ErrorKind.HTTP_READ, f"{slot}: {res}", source=str(slot))
            else:
                self._note_mode(slot, res)
                self.engine.update(slot, res)
        if not failed:
            self.errors.clear(ErrorKind.HTTP_READ)

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.interval)


class JanisPoller:
    """Reads the Janis LHe level every `interval` seconds."""

    def __init__(
        self, client: JanisClient, engine: DataEngine, errors: ErrorHandler, *, interval: float = 60.0
    ) -> None:
        self.client, self.engine, self.errors, self.interval = client, engine, errors, interval
        self.last_duration: float | None = None  # s, most recent poll_once

    async def poll_once(self) -> None:
        """One read; its duration (success or failure) is kept in `last_duration`."""
        t0 = time.perf_counter()
        try:
            await self._poll()
        finally:
            self.last_duration = time.perf_counter() - t0

    async def _poll(self) -> None:
        try:
            data = await self.client.read()
        except DriverError as exc:
            self.engine.mark_error(Instrument.JANIS, exc)
            self.errors.set(ErrorKind.JANIS_READ, str(exc), source="janis")
        else:
            self.engine.update(Instrument.JANIS, data)
            self.errors.clear(ErrorKind.JANIS_READ)

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.interval)


class SheetCryostatPoller:
    """Reads externally logged cryostat levels (CMI, Wei) from Google Sheet tabs.

    A reading whose own "Last Update Time" is older than `stale_after` is kept
    but flagged (link not connected, error says how old), so the UI and the
    log do not present a weeks-old level as live.
    """

    def __init__(
        self,
        clients: Mapping[Instrument, SheetTabClient],
        engine: DataEngine,
        errors: ErrorHandler,
        *,
        interval: float = 180.0,
        stale_after: float = 3600.0,
    ) -> None:
        self.clients, self.engine, self.errors = clients, engine, errors
        self.interval, self.stale_after = interval, stale_after
        self.last_duration: float | None = None  # s, most recent poll_once

    async def poll_once(self) -> None:
        """One read; its duration (success or failure) is kept in `last_duration`."""
        t0 = time.perf_counter()
        try:
            await self._poll()
        finally:
            self.last_duration = time.perf_counter() - t0

    async def _poll(self) -> None:
        results = await asyncio.gather(*(c.read() for c in self.clients.values()), return_exceptions=True)
        failed = False
        for slot, res in zip(self.clients, results):
            if isinstance(res, DriverError):
                failed = True
                self.engine.mark_error(slot, res)
                self.errors.set(ErrorKind.SHEET_READ, f"{slot}: {res}", source=str(slot))
                continue
            if isinstance(res, BaseException):
                raise res
            self.engine.update(slot, res)
            age = res.age()
            if age is not None and age.total_seconds() > self.stale_after:
                self.engine.mark_error(slot, f"source not updated since {res.source_time:%Y-%m-%d %H:%M}")
        if not failed:
            self.errors.clear(ErrorKind.SHEET_READ)

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.interval)


class ValvePoller:
    """ATL3 inlet valve: polls its state and opens/closes it on request.

    `on_means_open` maps the valve's DP1 switch onto open/closed. After a switch
    the valve is polled every `fast_interval` s for `fast_for` s so the status
    number (DP101) can be followed while the valve moves.
    """

    def __init__(
        self,
        valve: TuyaValve,
        engine: DataEngine,
        errors: ErrorHandler,
        *,
        interval: float = 30.0,
        on_means_open: bool = True,
        fast_interval: float = 2.0,
        fast_for: float = 30.0,
    ) -> None:
        self.valve, self.engine, self.errors, self.interval = valve, engine, errors, interval
        self.last_duration: float | None = None  # s, most recent poll_once
        self.on_means_open = on_means_open
        self.fast_interval, self.fast_for = fast_interval, fast_for
        self._fast_until = 0.0
        self._wake = asyncio.Event()
        self.on_write_failed: Callable[[str, str], None] | None = None

    def is_open(self, switch_on: bool | None) -> bool | None:
        return None if switch_on is None else switch_on == self.on_means_open

    async def poll_once(self) -> None:
        """One read; its duration (success or failure) is kept in `last_duration`."""
        t0 = time.perf_counter()
        try:
            await self._poll()
        finally:
            self.last_duration = time.perf_counter() - t0

    async def _poll(self) -> None:
        try:
            status = await self.valve.read()
        except DriverError as exc:
            self.engine.mark_error(Instrument.VALVE, exc)
            self.errors.set(ErrorKind.VALVE_READ, str(exc), source="valve")
        else:
            self.engine.update(Instrument.VALVE, status)
            self.errors.clear(ErrorKind.VALVE_READ)

    async def set_open(self, open_: bool) -> ValveStatus:
        """Open or close the valve; returns the read-back state (raises on failure)."""
        action = "open" if open_ else "close"
        log.warning("ATL3 inlet valve: %s requested", action)
        try:
            status = await self.valve.set_switch(open_ == self.on_means_open)
        except DriverError as exc:
            log.error("ATL3 inlet valve: %s failed: %s", action, exc)
            self.errors.set(ErrorKind.VALVE_READ, f"{action} failed: {exc}", source="valve")
            if self.on_write_failed:
                self.on_write_failed("valve", f"ATL3 inlet valve {action}: {exc}")
            raise
        self.engine.update(Instrument.VALVE, status)
        log.warning("ATL3 inlet valve: now %s (DP1=%s, status %s)",
                    "OPEN" if self.is_open(status.switch_on) else "CLOSED", status.switch_on, status.status_number)
        self._fast_until = asyncio.get_running_loop().time() + self.fast_for
        self._wake.set()
        return status

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await self.poll_once()
            delay = self.fast_interval if loop.time() < self._fast_until else self.interval
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except TimeoutError:
                pass
