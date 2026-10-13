"""Core.refresh_all (Internal tab "Refresh"): one read of every instrument, reads only."""

import asyncio

from heliumrecovery.config import Config
from heliumrecovery.core.runtime import Core
from heliumrecovery.core.workers import PumpAction
from heliumrecovery.drivers.base import DriverError


def make_core() -> Core:
    return Core(Config(pump_host="127.0.0.1", atl_hosts=("127.0.0.1",) * 3))


async def test_refresh_reads_pump_and_pollers_only() -> None:
    core = make_core()
    actions: list[PumpAction] = []
    polled: list[str] = []

    async def submit(action, settings=None):
        actions.append(action)

    async def atl_poll():
        polled.append("atl")

    core.pump.submit = submit
    core.atl.poll_once = atl_poll
    result = await core.refresh_all()
    assert actions == [PumpAction.READ_DATA, PumpAction.READ_SETTINGS]  # never SET_SETTINGS
    assert polled == ["atl"]
    assert result["pump"] is None and result["atl"] is None


async def test_refresh_reports_errors_and_timeouts() -> None:
    core = make_core()

    async def submit(action, settings=None):
        raise DriverError("pump not connected")

    async def hang():
        await asyncio.sleep(10)

    core.pump.submit = submit
    core.atl.poll_once = hang
    result = await core.refresh_all(timeout=0.05)
    assert result["pump"] == "pump not connected"
    assert "no answer" in result["atl"]


async def test_poll_duration_is_recorded_even_on_failure() -> None:
    core = make_core()

    async def slow_fail():
        await asyncio.sleep(0.02)
        raise DriverError("down")

    core.atl._poll = slow_fail
    assert core.atl.last_duration is None
    try:
        await core.atl.poll_once()
    except DriverError:
        pass
    assert core.atl.last_duration >= 0.02
