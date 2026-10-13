"""Acquisition core: data engine, error handler and loops (offline only)."""

import asyncio
from contextlib import aclosing
from pathlib import Path

import httpx
import pytest

from fixtures.pcom_fake import FakePcomServer
from heliumrecovery.core.engine import DataEngine, Instrument
from heliumrecovery.core.errors import ErrorHandler, ErrorKind
from heliumrecovery.core.workers import ATL_SLOTS, ATLPoller, JanisPoller, PumpAction, PumpWorker
from heliumrecovery.drivers.atl import ATLClient, parse_varlist
from heliumrecovery.drivers.base import DriverError, now
from heliumrecovery.drivers.janis import JanisClient, JanisData
from heliumrecovery.drivers.mphub import MPHub, PumpSettings

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = PumpSettings(
    enabled=True,
    inlet_pressure_setpoint=1270,
    comp_on_setpoint=1320,
    comp_off_setpoint=800,
    max_outlet_pressure=8000,
    pressure_setpoint=790,
    hiflow_onset=100,
    bp_valve_p_gain=1000,
    bp_valve_i_gain=20,
    bp_valve_d_gain=40,
)


def janis(level: float = 20.0) -> JanisData:
    return JanisData(lhe_level_in=level, lhe_volume_l=1.0, last_update=now())


# --- ATL real captures -------------------------------------------------------


@pytest.mark.parametrize("unit", [81, 82, 83])
def test_parse_real_atl_capture(unit: int) -> None:
    status = parse_varlist((FIXTURES / f"atl_real_{unit}.xml").read_bytes())  # has a UTF-8 BOM
    assert status.dewar_capacity == 160
    assert status.liquid_vol == status.liquid_volume  # the two volume fields are duplicates


# --- DataEngine --------------------------------------------------------------


def test_engine_update_and_snapshot_is_immutable() -> None:
    engine = DataEngine()
    before = engine.read()
    engine.update(Instrument.JANIS, janis(20))
    after = engine.read()
    assert before.get(Instrument.JANIS) is None
    assert after.get(Instrument.JANIS).lhe_level_in == 20
    assert after.link(Instrument.JANIS).connected


def test_engine_rejects_wrong_type() -> None:
    with pytest.raises(TypeError):
        DataEngine().update(Instrument.ATL1, janis())


def test_engine_mark_error_keeps_last_value() -> None:
    engine = DataEngine()
    engine.update(Instrument.JANIS, janis(20))
    engine.mark_error(Instrument.JANIS, "timeout")
    snap = engine.read()
    assert snap.get(Instrument.JANIS).lhe_level_in == 20
    assert not snap.link(Instrument.JANIS).connected
    assert snap.link(Instrument.JANIS).last_error == "timeout"
    engine.init()
    assert engine.read().get(Instrument.JANIS) is None


async def test_engine_subscribe() -> None:
    engine = DataEngine()
    got = []

    async def consume() -> None:
        async with aclosing(engine.subscribe()) as updates:
            async for update in updates:
                got.append(update)
                if len(got) == 2:
                    return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    engine.update(Instrument.JANIS, janis(1))
    engine.mark_error(Instrument.JANIS, "x")
    await asyncio.wait_for(task, 1)
    assert [u.link.connected for u in got] == [True, False]
    assert not engine._subscribers  # unsubscribed on exit


# --- ErrorHandler ------------------------------------------------------------


def test_error_handler_counts_and_clear() -> None:
    errors = ErrorHandler()
    for _ in range(3):
        errors.set(ErrorKind.TCP_READ, "boom")
    assert errors.get(ErrorKind.TCP_READ).count == 3
    assert errors.check(ErrorKind.TCP_READ) and not errors.check(ErrorKind.TCP_READ, threshold=30)
    assert len(errors.log()) == 3
    errors.clear(ErrorKind.TCP_READ)
    assert not errors.check(ErrorKind.TCP_READ)
    assert len(errors.log()) == 3  # history kept


# --- PumpWorker --------------------------------------------------------------


@pytest.fixture
async def plc():
    srv = await FakePcomServer().start()
    srv.registers["ML"].update({5: 114277, 15: 125365, 9: 554})
    srv.registers["MB"][11] = 1
    srv.registers["MI"].update({1900: 1270, 18: 1320, 17: 800, 37: 8000, 1967: 790, 1969: 100})
    yield srv
    await srv.stop()


async def _start_pump(port: int, allow_writes: bool = False):
    engine, errors = DataEngine(), ErrorHandler()
    worker = PumpWorker(MPHub("127.0.0.1", port, allow_writes=allow_writes), engine, errors, interval=0.02)
    task = asyncio.create_task(worker.run())
    for _ in range(100):
        await asyncio.sleep(0.02)
        snap = engine.read()
        if snap.get(Instrument.PUMP_DATA) and snap.get(Instrument.PUMP_SETTINGS):
            break
    return engine, errors, worker, task


async def test_pump_worker_reads_into_engine(plc: FakePcomServer) -> None:
    engine, errors, worker, task = await _start_pump(plc.port)
    try:
        snap = engine.read()
        assert snap.get(Instrument.PUMP_DATA).inlet_pressure == pytest.approx(1.25365)
        assert snap.get(Instrument.PUMP_SETTINGS).inlet_pressure_setpoint == 1270
        assert not errors.check(ErrorKind.TCP_READ)
        assert not any(kind == "S" for _, kind, _, _ in plc.log)  # read-only
    finally:
        task.cancel()


async def test_pump_worker_write_refused_without_allow(plc: FakePcomServer) -> None:
    engine, errors, worker, task = await _start_pump(plc.port)
    try:
        with pytest.raises(DriverError, match="disabled"):
            await worker.set_settings(SETTINGS)
        assert errors.check(ErrorKind.TCP_READ)
        assert not any(kind == "S" for _, kind, _, _ in plc.log)
    finally:
        task.cancel()


async def test_pump_worker_write_and_readback(plc: FakePcomServer) -> None:
    engine, errors, worker, task = await _start_pump(plc.port, allow_writes=True)
    try:
        new = SETTINGS.model_copy(update={"pressure_setpoint": 800})
        readback = await worker.set_settings(new)
        assert readback.pressure_setpoint == 800
        assert engine.read().get(Instrument.PUMP_SETTINGS).pressure_setpoint == 800
    finally:
        task.cancel()


async def test_pump_worker_reconnects_after_connect_failure() -> None:
    srv = await FakePcomServer().start()
    port = srv.port
    await srv.stop()
    engine, errors = DataEngine(), ErrorHandler()
    worker = PumpWorker(
        MPHub("127.0.0.1", port, timeout=0.05), engine, errors, interval=0.02, reconnect_delay=0.05
    )
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.5)  # a refused connect can take ~1 s on Windows; the timeout caps it
    assert errors.get(ErrorKind.TCP_READ).count >= 2  # retried
    assert not engine.read().link(Instrument.PUMP_DATA).connected
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_pump_disconnect_command(plc: FakePcomServer) -> None:
    engine, errors, worker, task = await _start_pump(plc.port)
    try:
        await worker.submit(PumpAction.DISCONNECT)
        assert not worker.connected
        assert not engine.read().link(Instrument.PUMP_DATA).connected
    finally:
        task.cancel()


# --- Pollers -----------------------------------------------------------------


async def test_atl_poller_mixed_results() -> None:
    body = (FIXTURES / "atl_real_81.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith(".82"):
            return httpx.Response(500)
        return httpx.Response(200, content=body)

    engine, errors = DataEngine(), ErrorHandler()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        clients = {
            slot: ATLClient(f"10.0.0.{81 + i}", client=http) for i, slot in enumerate(ATL_SLOTS)
        }
        await ATLPoller(clients, engine, errors).poll_once()
    snap = engine.read()
    assert snap.link(Instrument.ATL1).connected and snap.link(Instrument.ATL3).connected
    assert not snap.link(Instrument.ATL2).connected
    assert errors.get(ErrorKind.HTTP_READ).count == 1


async def test_janis_poller_error_then_recovery() -> None:
    responses = iter([httpx.Response(500), httpx.Response(200, text="<th>LHe</th><th>\n 20.0 in")])
    engine, errors = DataEngine(), ErrorHandler()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: next(responses))) as http:
        poller = JanisPoller(JanisClient("10.0.0.51", client=http), engine, errors)
        await poller.poll_once()
        assert errors.check(ErrorKind.JANIS_READ)
        await poller.poll_once()
    assert not errors.check(ErrorKind.JANIS_READ)
    assert engine.read().get(Instrument.JANIS).lhe_level_in == 20.0
