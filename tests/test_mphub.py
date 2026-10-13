"""MPHub end-to-end tests against a fake PCOM server on 127.0.0.1."""

import pytest

from fixtures.pcom_fake import FakePcomServer
from heliumrecovery.drivers.base import DriverError
from heliumrecovery.drivers.mphub import MPHub, PumpSettings

DEFAULTS = dict(
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


@pytest.fixture
async def server():
    srv = await FakePcomServer().start()
    yield srv
    await srv.stop()


async def test_read_data_scaling(server: FakePcomServer) -> None:
    server.registers["ML"].update(
        {5: 105000, 7: 1234567, 9: 456, 10: 789, 12: 25, 15: 98000, 6: 999, 14: 111}
    )
    server.registers["MI"].update({200: 42, 300: 0xFFFF})
    async with MPHub("127.0.0.1", server.port) as hub:
        data = await hub.read_data(user_indices=[200, 300])
    assert data.tank_pressure == pytest.approx(1.05)
    assert data.outlet_pressure == pytest.approx(12.34567)
    assert data.oil_temperature == pytest.approx(45.6)
    assert data.outlet_tank_level == pytest.approx(78.9)
    assert data.helium_flow_in == pytest.approx(2.5)
    assert data.inlet_pressure == pytest.approx(0.98)
    assert [(u.index, u.value) for u in data.user_data] == [(200, 42), (300, 65535)]
    assert data.last_update.tzinfo is not None
    assert server.log == [("ML", "R", 5, []), ("MI", "R", 200, []), ("MI", "R", 300, [])]


async def test_read_settings(server: FakePcomServer) -> None:
    server.registers["MB"][11] = 1
    server.registers["MI"].update({1900: 1270, 18: 1320, 17: 800, 37: 8000, 1967: 790,
                                   1969: 100, 1902: 1000, 1903: 20, 1904: 40})
    async with MPHub("127.0.0.1", server.port) as hub:
        settings = await hub.read_settings()
    assert settings == PumpSettings(**DEFAULTS)
    assert [entry[2] for entry in server.log] == [11, 1900, 18, 17, 37, 1967, 1969, 1902, 1903, 1904]


async def test_writes_refused_by_default(server: FakePcomServer) -> None:
    async with MPHub("127.0.0.1", server.port) as hub:
        with pytest.raises(DriverError, match="allow_writes"):
            await hub.write_settings(PumpSettings(**DEFAULTS))
        with pytest.raises(DriverError, match="allow_writes"):
            await hub.set_enabled(True)
    assert server.log == []


async def test_write_settings_sequence_and_rounding(server: FakePcomServer) -> None:
    settings = PumpSettings(**{**DEFAULTS, "comp_off_setpoint": 800.5, "bp_valve_i_gain": 20.6})
    async with MPHub("127.0.0.1", server.port, allow_writes=True) as hub:
        result = await hub.write_settings(settings)
    writes = [e for e in server.log if e[1] == "S"]
    assert writes == [
        ("MI", "S", 1900, [1270]),
        ("MI", "S", 18, [1320]),
        ("MI", "S", 17, [800]),  # 800.5 rounds half-to-even
        ("MI", "S", 37, [8000]),
        ("MI", "S", 1967, [790]),
        ("MI", "S", 1969, [100]),
        ("MI", "S", 1902, [1000]),
        ("MI", "S", 1903, [21]),
        ("MI", "S", 1904, [40]),
        ("MB", "S", 11, [1]),
    ]
    reads = [e for e in server.log if e[1] == "R"]
    assert len(reads) == 10 and server.log.index(reads[0]) == 10  # read-back after all writes
    assert result.enabled is True and result.comp_off_setpoint == 800 and result.bp_valve_i_gain == 21


async def test_set_enabled(server: FakePcomServer) -> None:
    async with MPHub("127.0.0.1", server.port, allow_writes=True) as hub:
        await hub.set_enabled(False)
        await hub.set_enabled(True)
    assert server.log == [("MB", "S", 11, [0]), ("MB", "S", 11, [1])]


@pytest.mark.parametrize(
    "field, value",
    [("comp_on_setpoint", float("nan")), ("max_outlet_pressure", float("inf")),
     ("pressure_setpoint", -1)],
)
def test_invalid_settings_rejected_by_model(field: str, value: float) -> None:
    with pytest.raises(ValueError):
        PumpSettings(**{**DEFAULTS, field: value})


@pytest.mark.parametrize(
    "field, value",
    [("max_outlet_pressure", 40000), ("comp_on_setpoint", float("nan")), ("hiflow_onset", -3)],
)
async def test_invalid_settings_never_reach_plc(server: FakePcomServer, field: str, value: float) -> None:
    bad = PumpSettings.model_construct(**{**DEFAULTS, field: value})  # bypass pydantic validation
    async with MPHub("127.0.0.1", server.port, allow_writes=True) as hub:
        with pytest.raises(DriverError):
            await hub.write_settings(bad)
    assert server.log == []
