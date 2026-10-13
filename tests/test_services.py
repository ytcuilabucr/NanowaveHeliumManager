"""Transfer control, Sheets upload, Slack alerts and history (offline only)."""

import asyncio
from pathlib import Path

import httpx
import pytest

from fixtures.pcom_fake import FakePcomServer
from heliumrecovery.core.engine import DataEngine, Instrument, Link, Update
from heliumrecovery.core.errors import ErrorHandler, ErrorKind
from heliumrecovery.core.history import History
from heliumrecovery.core.transfer import DEFAULT_PRESETS, TransferController, check_alarms
from heliumrecovery.core.workers import PumpWorker
from heliumrecovery.drivers.atl import parse_varlist
from heliumrecovery.drivers.base import DriverError, now
from heliumrecovery.drivers.mphub import MPHub, PumpData
from heliumrecovery.services.sheets import SheetsUploader, format_value, query_params, snapshot_rows

FIXTURES = Path(__file__).parent / "fixtures"


def pump(inlet=1.25, tank=0.8, outlet=2.7) -> PumpData:
    return PumpData(
        inlet_pressure=inlet,
        tank_pressure=tank,
        outlet_pressure=outlet,
        outlet_tank_level=33.0,
        oil_temperature=55.0,
        helium_flow_in=1.8,
        last_update=now(),
    )


# --- Transfer Control --------------------------------------------------------


def test_alarm_thresholds() -> None:
    assert check_alarms(pump(inlet=1.4, tank=1.35)).inlet_overlimit
    assert check_alarms(pump(inlet=1.4, tank=1.35)).tank_overlimit
    a = check_alarms(pump(inlet=1.399, tank=1.349))
    assert not a.inlet_overlimit and not a.tank_overlimit


@pytest.fixture
async def rig():
    srv = await FakePcomServer().start()
    srv.registers["MB"][11] = 1
    srv.registers["MI"].update({1900: 1270, 18: 1320, 17: 800, 37: 8000, 1967: 790, 1969: 100})
    engine, errors = DataEngine(), ErrorHandler()
    worker = PumpWorker(MPHub("127.0.0.1", srv.port, allow_writes=True), engine, errors, interval=0.02)
    task = asyncio.create_task(worker.run())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if engine.read().get(Instrument.PUMP_SETTINGS):
            break
    tc = TransferController(worker, engine, step2_delay=0.05)
    yield srv, engine, tc
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await srv.stop()


def writes(srv: FakePcomServer) -> list[tuple[str, int, list[int]]]:
    return [(op, addr, vals) for op, kind, addr, vals in srv.log if kind == "S"]


async def test_transfer_steps(rig) -> None:
    srv, engine, tc = rig
    tc.select_preset(1)
    assert tc.to_set == DEFAULT_PRESETS[1]

    s = await tc.step1()
    assert not s.enabled and s.pressure_setpoint == 350
    assert writes(srv)[-1] == ("MB", 11, [0])

    srv.log.clear()
    s = await tc.step2()
    assert not s.enabled and s.pressure_setpoint == 350  # current settings, disabled

    s = await tc.step3()
    assert s == DEFAULT_PRESETS[0] and tc.preset_index == 0
    assert srv.registers["MB"][11] == 1 and srv.registers["MI"][1967] == 790


async def test_auto_reenable_fires_once(rig) -> None:
    srv, engine, tc = rig
    tc.select_preset(1)
    await tc.step1()
    srv.log.clear()
    tc.auto_enable_armed = True
    tc.on_pump_data(pump(inlet=1.30))
    assert tc.auto_enable_armed  # below 1.38: nothing
    tc.on_pump_data(pump(inlet=1.39))
    assert not tc.auto_enable_armed
    for _ in range(100):
        await asyncio.sleep(0.02)
        if srv.registers["MB"].get(11) == 1:
            break
    assert srv.registers["MB"][11] == 1
    assert srv.registers["MI"][1967] == 350  # "Pump Settings to set" with Enabled=True
    n = len(writes(srv))
    tc.on_pump_data(pump(inlet=1.5))
    await asyncio.sleep(0.1)
    assert len(writes(srv)) == n  # disarmed: no second write


async def test_step1_start_disables_arms_and_reenables(rig) -> None:
    srv, engine, tc = rig
    s = await tc.step1_start(2)
    assert not s.enabled and tc.preset_index == 2 and tc.auto_enable_armed
    assert srv.registers["MB"][11] == 0 and tc.auto_enable_future is None
    tc.on_pump_data(pump(inlet=1.39))  # the inlet jump
    assert not tc.auto_enable_armed and tc.auto_enable_future is not None
    readback = await asyncio.wait_for(tc.auto_enable_future, 2)
    assert readback.enabled and readback.comp_on_setpoint == 800  # preset 2, enabled
    assert srv.registers["MB"][11] == 1


async def test_disarm_keeps_pump_disabled(rig) -> None:
    srv, engine, tc = rig
    await tc.step1_start(1)
    tc.disarm()
    tc.on_pump_data(pump(inlet=1.5))
    await asyncio.sleep(0.1)
    assert tc.auto_enable_future is None and srv.registers["MB"][11] == 0


async def test_disable_now_rewrites_current_settings(rig) -> None:
    srv, engine, tc = rig
    s = await tc.disable_now()
    assert not s.enabled and s.pressure_setpoint == 790  # current PLC values, only Enabled changed


async def test_transfer_refused_when_writes_disabled() -> None:
    srv = await FakePcomServer().start()
    engine, errors = DataEngine(), ErrorHandler()
    worker = PumpWorker(MPHub("127.0.0.1", srv.port), engine, errors, interval=0.02)
    task = asyncio.create_task(worker.run())
    try:
        with pytest.raises(DriverError):
            await TransferController(worker, engine).set_pars()
        assert not writes(srv)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await srv.stop()


# --- Google Sheets -----------------------------------------------------------


def test_format_value_for_sheets() -> None:
    assert format_value(True) == "TRUE"
    assert format_value(1.25365) == "1.253650"  # %f
    assert format_value(float("nan")) == "NaN"


def _engine_with_data() -> DataEngine:
    engine = DataEngine()
    engine.update(Instrument.PUMP_DATA, pump())
    engine.update(Instrument.ATL1, parse_varlist((FIXTURES / "atl_real_81.xml").read_bytes()))
    return engine


def test_snapshot_rows_use_sheet_labels() -> None:
    rows = snapshot_rows(_engine_with_data().read())
    names = {(r.instr, r.name) for r in rows}
    assert ("ATL1", "Diorde T") in names and ("MPHUB", "Inlet Pressure") in names
    unit = {r.name: r.unit for r in rows}
    assert unit["Diorde T"] == "K" and unit["Inlet Pressure"] == "bar" and unit["Dewar Pres"] == "psi"
    assert rows[0].instr == "ATL1"  # Janis absent, so ATL1 first


def test_query_params_numbering() -> None:
    rows = snapshot_rows(_engine_with_data().read())
    params = dict(query_params(rows))
    assert params["Instr0"] == "ATL1" and params["pName0"] == "Diorde T"
    assert f"unit{len(rows) - 1}" in params and f"Instr{len(rows)}" not in params


async def test_sheets_upload_posts_query_params() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    errors = ErrorHandler()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        up = SheetsUploader("https://example.invalid/exec", _engine_with_data(), errors, client=http)
        assert await up.upload_once()
    req = seen[0]
    assert req.method == "POST" and req.content == b""
    assert req.url.params["Instr0"] == "ATL1"
    assert up.last_upload is not None and not errors.check(ErrorKind.HTTP_POST)


async def test_sheets_upload_failure_counts_error() -> None:
    errors = ErrorHandler()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
        up = SheetsUploader("https://example.invalid/exec", _engine_with_data(), errors, client=http)
        assert not await up.upload_once()
    assert errors.get(ErrorKind.HTTP_POST).count == 1


# --- History -----------------------------------------------------------------


def test_history_decimates_pump_and_records_atl() -> None:
    engine = DataEngine()
    h = History(engine, pump_every=5)
    for _ in range(10):
        h.on_update(Update(Instrument.PUMP_DATA, pump(), Link(connected=True)))
    assert len(h.get("inlet_pressure")) == 2
    atl = parse_varlist((FIXTURES / "atl_real_81.xml").read_bytes())
    h.on_update(Update(Instrument.ATL1, atl, Link(connected=True)))
    assert h.get("atl1_liquid_volume")[0][1] == atl.liquid_volume
    h.on_update(Update(Instrument.ATL2, atl, Link(connected=False)))
    assert h.get("atl2_liquid_volume") == []


async def test_sim_transfer_never_writes(monkeypatch) -> None:
    from heliumrecovery.ui import transfer_dialogs as td

    monkeypatch.setattr(td, "SIM_WRITE_S", 0)

    class Real:
        limits, presets, step2_delay = None, DEFAULT_PRESETS, 5.0

        def __getattr__(self, name):
            raise AssertionError(f"simulation touched the real controller: {name}")

    sim = td.SimTransfer(Real())
    await sim.step1_start(1)
    assert sim.auto_enable_armed
    sim.inlet_jump()
    assert not sim.auto_enable_armed
    await sim.auto_enable_future
    await sim.disable_now()
    await sim.step3()


async def test_pump_worker_rereads_settings_periodically() -> None:
    srv = await FakePcomServer().start()
    srv.registers["MB"][11] = 1
    srv.registers["MI"].update({1900: 1300, 18: 1270, 17: 800, 37: 8000, 1967: 790, 1969: 100})
    engine = DataEngine()
    worker = PumpWorker(MPHub("127.0.0.1", srv.port), engine, ErrorHandler(), interval=0.02, settings_interval=0.2)
    task = asyncio.create_task(worker.run())
    try:
        for _ in range(100):
            await asyncio.sleep(0.02)
            if engine.read().get(Instrument.PUMP_SETTINGS):
                break
        srv.registers["MI"][18] = 1250  # changed on the PLC panel
        for _ in range(100):
            await asyncio.sleep(0.02)
            if engine.read().get(Instrument.PUMP_SETTINGS).comp_on_setpoint == 1250:
                break
        assert engine.read().get(Instrument.PUMP_SETTINGS).comp_on_setpoint == 1250
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await srv.stop()
