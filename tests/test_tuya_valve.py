"""NEXGEN smart valve (Tuya local protocol), read-only; tinytuya device faked."""

import json
from pathlib import Path

import pytest

from heliumrecovery.core.engine import DataEngine, Instrument
from heliumrecovery.core.errors import ErrorHandler, ErrorKind
from heliumrecovery.core.workers import ValvePoller
from heliumrecovery.drivers.base import DriverError, InstrumentTimeout, ProtocolError
from heliumrecovery.drivers.tuya_valve import TuyaValve, parse_dps

# Reply seen from the real valve on 2026-10-10.
REAL_DPS = {"1": True, "9": 0, "38": "on", "101": 100, "102": 100}


class FakeDevice:
    """Stands in for tinytuya.Device; records calls, returns queued replies."""

    def __init__(self, dev_id, ip, key, version=3.3, replies=None):
        self.args = (dev_id, ip, key, version)
        self.replies = list(replies or [{"dps": REAL_DPS}])
        self.calls: list[str] = []
        self.timeout = self.retries = None

    def set_socketTimeout(self, t):
        self.timeout = t

    def set_socketRetryLimit(self, n):
        self.retries = n

    def status(self):
        self.calls.append("status")
        if self.switch_to is not None:
            return {"dps": {**REAL_DPS, "1": self.switch_to}}
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    switch_to = None
    ignore_switch = False

    def set_status(self, on, switch=1):
        self.calls.append(f"set_status({on},{switch})")
        if not self.ignore_switch:
            self.switch_to = on
        return {"dps": {"1": on}}

    def __getattr__(self, name):  # anything else (set_value, turn_on, ...) is not expected
        raise AssertionError(f"driver called {name}")


def valve(replies=None, allow_writes=False) -> tuple[TuyaValve, FakeDevice]:
    made = {}

    def factory(*a, **kw):
        made["dev"] = FakeDevice(*a, replies=replies, **kw)
        return made["dev"]

    v = TuyaValve("NEXGEN Valve", "id1", "192.0.2.105", "k", allow_writes=allow_writes, device_factory=factory)
    return v, made["dev"]


def test_parse_real_reply() -> None:
    s = parse_dps("NEXGEN Valve", REAL_DPS)
    assert (s.switch_on, s.countdown_s, s.power_on_state) == (True, 0, "on")
    assert s.dps["101"] == 100


def test_parse_without_switch_dp_fails() -> None:
    with pytest.raises(ProtocolError):
        parse_dps("v", {"9": 0})


async def test_read_uses_only_status_with_short_timeout() -> None:
    v, dev = valve()
    s = await v.read()
    assert s.switch_on is True
    assert dev.calls == ["status"] and dev.timeout == 3.0 and dev.retries == 1


@pytest.mark.parametrize(
    ("reply", "exc"),
    [
        ({"Error": "Network Error: Device Unreachable", "Err": "905"}, InstrumentTimeout),
        ({"Error": "Check device key or version", "Err": "914"}, DriverError),
        ("garbage", ProtocolError),
    ],
)
async def test_read_errors(reply, exc) -> None:
    v, _ = valve([reply])
    with pytest.raises(exc):
        await v.read()


def test_from_devices_json(tmp_path: Path) -> None:
    f = tmp_path / "devices.json"
    f.write_text(json.dumps([
        {"name": "AFM humidifier", "id": "x", "key": "k", "ip": "", "version": ""},
        {"name": "NEXGEN Valve", "id": "id1", "key": "secret", "ip": "192.0.2.105", "version": "3.3"},
    ]), encoding="utf-8")
    made = []
    v = TuyaValve.from_devices_json(f, "NEXGEN Valve", device_factory=lambda *a, **k: made.append((a, k)) or FakeDevice(*a, **k))
    assert made[0] == (("id1", "192.0.2.105", "secret"), {"version": 3.3}) and v.ip == "192.0.2.105"
    with pytest.raises(DriverError, match="no IP"):
        TuyaValve.from_devices_json(f, "AFM humidifier")
    with pytest.raises(DriverError, match="no device"):
        TuyaValve.from_devices_json(f, "missing")
    with pytest.raises(DriverError, match="wizard"):
        TuyaValve.from_devices_json(tmp_path / "nope.json", "NEXGEN Valve")


async def test_poller_updates_then_flags_errors() -> None:
    v, dev = valve([{"dps": REAL_DPS}, {"Error": "Network Error: Device Unreachable", "Err": "905"}])
    engine, errors = DataEngine(), ErrorHandler()
    poller = ValvePoller(v, engine, errors)
    await poller.poll_once()
    assert engine.read().get(Instrument.VALVE).switch_on is True
    assert engine.read().link(Instrument.VALVE).connected
    await poller.poll_once()
    assert not engine.read().link(Instrument.VALVE).connected
    assert engine.read().get(Instrument.VALVE).switch_on is True  # last good value kept
    assert errors.get(ErrorKind.VALVE_READ).count == 1


async def test_switch_refused_without_allow_writes() -> None:
    v, dev = valve()
    with pytest.raises(DriverError, match="HR_VALVE_ALLOW_WRITES"):
        await v.set_switch(False)
    assert dev.calls == []  # nothing sent


async def test_switch_writes_dp1_and_reads_back() -> None:
    v, dev = valve(allow_writes=True)
    s = await v.set_switch(False)
    assert dev.calls == ["set_status(False,1)", "status"]
    assert s.switch_on is False and s.status_number == 100


async def test_switch_that_does_not_take_raises() -> None:
    v, dev = valve(allow_writes=True)
    dev.ignore_switch = True  # device acks but stays ON
    with pytest.raises(DriverError, match="read back"):
        await v.set_switch(False)


@pytest.mark.parametrize(("on_means_open", "expected_dp1"), [(True, False), (False, True)])
async def test_poller_close_maps_to_switch(on_means_open, expected_dp1) -> None:
    v, dev = valve(allow_writes=True)
    engine, errors = DataEngine(), ErrorHandler()
    poller = ValvePoller(v, engine, errors, on_means_open=on_means_open)
    s = await poller.set_open(False)
    assert s.switch_on is expected_dp1
    assert poller.is_open(engine.read().get(Instrument.VALVE).switch_on) is False


async def test_poller_set_open_failure_is_recorded() -> None:
    v, _ = valve()  # writes disabled
    engine, errors = DataEngine(), ErrorHandler()
    with pytest.raises(DriverError):
        await ValvePoller(v, engine, errors).set_open(True)
    assert errors.get(ErrorKind.VALVE_READ).count == 1


async def test_poller_polls_fast_after_a_switch() -> None:
    import asyncio

    v, dev = valve(allow_writes=True)
    poller = ValvePoller(v, DataEngine(), ErrorHandler(), interval=60, fast_interval=0.05, fast_for=0.5)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.1)
    before = dev.calls.count("status")
    await poller.set_open(False)
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert dev.calls.count("status") - before >= 4  # read-back + several fast polls, not one per minute


def test_valve_rows_for_status_sheet() -> None:
    from heliumrecovery.services.sheets import valve_rows

    engine = DataEngine()
    assert valve_rows(engine.read()) == []
    engine.update(Instrument.VALVE, parse_dps("NEXGEN Valve", REAL_DPS))
    rows = {(r.instr, r.name): r.value for r in valve_rows(engine.read())}
    assert rows == {("ATL3", "Inlet Valve Switch"): "TRUE", ("ATL3", "Inlet Valve Status"): "100"}
