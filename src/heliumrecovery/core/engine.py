"""Data engine: the single store of latest instrument values.

One slot per instrument (`Instrument`). All mutation happens on the asyncio event loop, and each
method is synchronous, so updates are atomic without a lock. Readers get an
immutable `Snapshot`; subscribers get every update pushed to a queue.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from ..drivers.atl import ATLStatus
from ..drivers.base import now
from ..drivers.janis import JanisData
from ..drivers.sheet_cryostat import CryostatLevel
from ..drivers.tuya_valve import ValveStatus
from ..drivers.mphub import PumpData, PumpSettings


class Instrument(StrEnum):
    """Data slots, one per instrument reading."""

    PUMP_DATA = "pump_data"
    PUMP_SETTINGS = "pump_settings"
    ATL1 = "atl1"
    ATL2 = "atl2"
    ATL3 = "atl3"
    JANIS = "janis"
    CMI = "cmi"  # level logged by another system into a Google Sheet
    WEI = "wei_cryostat"  # ditto
    VALVE = "valve"  # NEXGEN smart valve (Tuya, read-only)


SLOT_TYPES: dict[Instrument, type] = {
    Instrument.PUMP_DATA: PumpData,
    Instrument.PUMP_SETTINGS: PumpSettings,
    Instrument.ATL1: ATLStatus,
    Instrument.ATL2: ATLStatus,
    Instrument.ATL3: ATLStatus,
    Instrument.JANIS: JanisData,
    Instrument.CMI: CryostatLevel,
    Instrument.WEI: CryostatLevel,
    Instrument.VALVE: ValveStatus,
}


@dataclass(frozen=True)
class Link:
    """Connection health of one data source."""

    connected: bool = False
    last_ok: datetime | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class Snapshot:
    """Immutable view of everything the engine knows ."""

    values: dict[Instrument, Any] = field(default_factory=dict)
    links: dict[Instrument, Link] = field(default_factory=dict)
    updated: datetime | None = None

    def get(self, slot: Instrument) -> Any:
        return self.values.get(slot)

    def link(self, slot: Instrument) -> Link:
        return self.links.get(slot, Link())


@dataclass(frozen=True)
class Update:
    """One change pushed to subscribers."""

    slot: Instrument
    value: Any
    link: Link


class DataEngine:
    """Latest-value store with change notification."""

    def __init__(self) -> None:
        self._snap = Snapshot()
        self._subscribers: set[asyncio.Queue[Update]] = set()

    def init(self) -> None:
        """Clear every slot."""
        self._snap = Snapshot()

    def read(self) -> Snapshot:
        """Current snapshot."""
        return self._snap

    def update(self, slot: Instrument, value: Any) -> None:
        """Store a fresh value and mark its source healthy."""
        expected = SLOT_TYPES[slot]
        if not isinstance(value, expected):
            raise TypeError(f"{slot} expects {expected.__name__}, got {type(value).__name__}")
        link = Link(connected=True, last_ok=now())
        self._commit(slot, link, value=value)

    def mark_error(self, slot: Instrument, error: BaseException | str) -> None:
        """Record a failed read; the last good value is kept but flagged stale."""
        old = self._snap.link(slot)
        self._commit(slot, replace(old, connected=False, last_error=str(error)))

    def mark_disconnected(self, slot: Instrument) -> None:
        self._commit(slot, replace(self._snap.link(slot), connected=False))

    def _commit(self, slot: Instrument, link: Link, **kw: Any) -> None:
        values = dict(self._snap.values)
        if "value" in kw:
            values[slot] = kw["value"]
        links = {**self._snap.links, slot: link}
        self._snap = Snapshot(values=values, links=links, updated=now())
        update = Update(slot, values.get(slot), link)
        for q in list(self._subscribers):
            if q.full():  # slow consumer: drop its oldest update rather than block producers
                q.get_nowait()
            q.put_nowait(update)

    @contextmanager
    def _subscription(self, maxsize: int):
        q: asyncio.Queue[Update] = asyncio.Queue(maxsize)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)

    async def subscribe(self, maxsize: int = 100) -> AsyncIterator[Update]:
        """Yield every update from now on (UI, loggers, uploaders)."""
        with self._subscription(maxsize) as q:
            while True:
                yield await q.get()
