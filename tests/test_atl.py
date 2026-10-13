"""Offline tests for the ATL liquefier driver (httpx.MockTransport only)."""

import math
from pathlib import Path

import httpx
import pytest

from heliumrecovery.drivers.atl import ATLClient, ATLStatus, parse_varlist, read_all
from heliumrecovery.drivers.base import DriverError, InstrumentTimeout, ProtocolError

FIXTURES = Path(__file__).parent / "fixtures"
GOOD = (FIXTURES / "atl_varlist.xml").read_text()
MISSING = (FIXTURES / "atl_varlist_missing.xml").read_text()
HOSTS = {"ATL1": "atl1.test", "ATL2": "atl2.test", "ATL3": "atl3.test"}


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_parse_happy_path():
    s = parse_varlist(GOOD)
    assert s.diode_temp == 4.62
    assert s.dewar_pressure == 1.25
    assert s.input_gas_flow == 12.4
    assert s.liquid_vol == 87.3
    assert s.liquefaction_rate == 21.6
    assert (s.status_machine, s.liquid_mode, s.transfer_mode, s.status_leds) == (3, 1, 0, 5)
    assert s.dewar_capacity == 160
    assert s.liquid_volume == 87.3
    assert s.heater_power == 12.5
    assert s.last_update is not None and s.last_update.tzinfo is not None


def test_parse_tolerant_case_namespace_and_nesting():
    xml = """<ns:Root xmlns:ns="urn:x"><Vars>
      <ns:StatusVar>1</ns:StatusVar><ns:StatusVar>2</ns:StatusVar>
      <ns:StatusVar>3</ns:StatusVar><ns:StatusVar>4</ns:StatusVar>
      <ns:StatusVar>5</ns:StatusVar></Vars>
      <STATUSMACHINE>1</STATUSMACHINE><LiquidMode>2</LiquidMode><TransferMode>3</TransferMode>
      <StatusLEDs>4</StatusLEDs><DewarCapacity>5</DewarCapacity>
      <LiquidVolume>6</LiquidVolume><HeaterPower>7</HeaterPower></ns:Root>"""
    s = parse_varlist(xml)
    assert [s.diode_temp, s.dewar_pressure, s.input_gas_flow, s.liquid_vol, s.liquefaction_rate] == [1, 2, 3, 4, 5]
    assert s.heater_power == 7


def test_parse_delimited_statusvar():
    xml = GOOD.split("<statusvar>")[0] + "<statusvar>4.6, 1.2, 12, 80, 20</statusvar>" + GOOD.split("</statusvar>")[-1]
    s = parse_varlist(xml)
    assert s.liquefaction_rate == 20


def test_parse_missing_fields():
    with pytest.raises(ProtocolError, match="statusvar"):
        parse_varlist(MISSING)
    no_heater = GOOD.replace("<heaterpower>12.5</heaterpower>", "")
    with pytest.raises(ProtocolError, match="heaterpower"):
        parse_varlist(no_heater)


def test_parse_malformed_xml():
    with pytest.raises(ProtocolError):
        parse_varlist("<varlist><statusvar>1</varlist")
    with pytest.raises(ProtocolError, match="not a number"):
        parse_varlist(GOOD.replace("<heaterpower>12.5", "<heaterpower>abc"))


def test_unavailable_placeholder():
    u = ATLStatus.unavailable()
    assert math.isnan(u.diode_temp) and math.isnan(u.heater_power)
    assert u.last_update is None


async def test_read_status_url_and_auth():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, text=GOOD)

    async with mock_client(handler) as http:
        atl = ATLClient("atl1.test", client=http, auth=("u", "p"))
        s = await atl.read_status()
    assert s.diode_temp == 4.62
    assert str(seen[0].url) == "http://atl1.test:8080/SSI/varlist.xml"
    assert seen[0].headers["authorization"].startswith("Basic ")


async def test_no_auth_by_default():
    def handler(req: httpx.Request) -> httpx.Response:
        assert "authorization" not in req.headers
        return httpx.Response(200, text=GOOD)

    async with mock_client(handler) as http:
        await ATLClient("atl1.test", client=http).read_status()


async def test_http_500():
    async with mock_client(lambda r: httpx.Response(500, text="err")) as http:
        with pytest.raises(ProtocolError, match="500"):
            await ATLClient("atl1.test", client=http).read_status()


async def test_timeout():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=req)

    async with mock_client(handler) as http:
        with pytest.raises(InstrumentTimeout):
            await ATLClient("atl1.test", client=http).read_status()


async def test_connect_error_is_driver_error():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    async with mock_client(handler) as http:
        with pytest.raises(DriverError):
            await ATLClient("atl1.test", client=http).read_status()


async def test_context_manager_does_not_close_injected_client():
    async with mock_client(lambda r: httpx.Response(200, text=GOOD)) as http:
        async with ATLClient("atl1.test", client=http) as atl:
            await atl.read_status()
        assert not http.is_closed


async def test_read_all_shared_client():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "atl2.test":
            return httpx.Response(500)
        if req.url.host == "atl3.test":
            raise httpx.ConnectTimeout("slow", request=req)
        return httpx.Response(200, text=GOOD)

    async with mock_client(handler) as http:
        clients = {name: ATLClient(host, client=http) for name, host in HOSTS.items()}
        results = await read_all(clients)
    assert list(results) == ["ATL1", "ATL2", "ATL3"]
    assert isinstance(results["ATL1"], ATLStatus)
    assert isinstance(results["ATL2"], ProtocolError)
    assert isinstance(results["ATL3"], InstrumentTimeout)


async def test_transport_error_is_retried_then_succeeds() -> None:
    body = (Path(__file__).parent / "fixtures" / "atl_real_81.xml").read_bytes()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadError("connection dropped", request=request)
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        c = ATLClient("10.0.0.81", client=http, retry_delay=0)
        status = await c.read_status()
    assert status.dewar_capacity == 160 and calls["n"] == 3 and c.retries_used == 2


async def test_retries_exhausted_raise_driver_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection dropped", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        c = ATLClient("10.0.0.81", client=http, retries=1, retry_delay=0)
        with pytest.raises(DriverError, match="ReadError"):
            await c.read_status()


async def test_http_errors_are_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ProtocolError):
            await ATLClient("10.0.0.81", client=http, retry_delay=0).read_status()
    assert calls["n"] == 1
