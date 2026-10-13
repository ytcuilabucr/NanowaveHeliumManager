"""Offline tests for the Janis LHe level meter driver."""

import base64
from pathlib import Path

import httpx
import pytest

from heliumrecovery.drivers.base import DriverError, InstrumentTimeout, ProtocolError
from heliumrecovery.drivers.janis import (
    LOWER_SECTION_HEIGHT_IN,
    JanisAuthError,
    JanisClient,
    JanisData,
    level_to_volume,
    parse_level,
    parse_level_with_raw,
)

FIXTURE = Path(__file__).parent / "fixtures" / "janis_lm510.html"
USER, PW = "user-placeholder", "pass-placeholder"


@pytest.fixture
def page() -> str:
    return FIXTURE.read_text()


# --- level_to_volume ---------------------------------------------------------

@pytest.mark.parametrize(
    ("level", "vol"),
    [(-1.0, 0.0), (0.0, 0.0), (8.45, 5.2), (16.9, 10.4), (20.0, 10.4 + 3.1 * 4.4)],
)
def test_level_to_volume(level: float, vol: float) -> None:
    assert level_to_volume(level) == pytest.approx(vol)


def test_level_to_volume_continuous_at_breakpoint() -> None:
    eps = 1e-9
    below = level_to_volume(LOWER_SECTION_HEIGHT_IN - eps)
    above = level_to_volume(LOWER_SECTION_HEIGHT_IN + eps)
    assert below == pytest.approx(above, abs=1e-6)


# --- parse_level -------------------------------------------------------------

def test_parse_fixture(page: str) -> None:
    level, raw = parse_level_with_raw(page)
    assert level == pytest.approx(23.4)
    assert raw == "23.4 in"


@pytest.mark.parametrize(
    "html",
    [
        "<th>LHe</th><th>\n23.4 in</th>",
        "<th>LHe</th><th>\r\n 23.4 in</th>",
        "<th>LHe</th><th>23.4</th>",
        "<th>LHe </th>\n  <th class='v'>\n\t23.4in</th>",
        "<TH>LHe</TH><TH>\n+23.40 in</TH>",
    ],
)
def test_parse_variations(html: str) -> None:
    assert parse_level(html) == pytest.approx(23.4)


def test_parse_negative_and_integer() -> None:
    assert parse_level("<th>LHe</th><th>\n-0.5 in") == pytest.approx(-0.5)
    assert parse_level("<th>LHe</th><th>\n7 in") == pytest.approx(7.0)


def test_parse_first_match_wins() -> None:
    html = "<th>LHe</th><th>\n1.0 in</th><th>LHe</th><th>\n2.0 in</th>"
    assert parse_level(html) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<html><body>no level here</body></html>",
        "<th>LN2</th><th>\n85.0 %</th>",
        "<th>LHe</th><th>\n---- in</th>",
        "<th>LHe</th><th>\n</th>",
        "<th>LHe</th><th>\nnan</th>",
    ],
)
def test_parse_failures(html: str) -> None:
    with pytest.raises(ProtocolError):
        parse_level(html)


# --- JanisClient -------------------------------------------------------------

def _client(handler, **kwargs) -> JanisClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs.setdefault("host", "192.0.2.51")
    return JanisClient(client=http, **kwargs)


async def test_read_sends_basic_auth(page: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=page)

    async with _client(handler, username=USER, password=PW) as jc:
        data = await jc.read()

    assert isinstance(data, JanisData)
    assert data.lhe_level_in == pytest.approx(23.4)
    assert data.lhe_volume_l == pytest.approx(level_to_volume(23.4))
    assert data.lhe_level_raw == "23.4 in"
    assert data.last_update.tzinfo is not None
    req = seen[0]
    assert (req.url.scheme, req.url.host) == ("http", "192.0.2.51")
    expected = base64.b64encode(f"{USER}:{PW}".encode()).decode()
    assert req.headers["Authorization"] == f"Basic {expected}"


async def test_no_credentials_sends_no_auth(page: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=page)

    async with _client(handler, host="https://meter.example:8443/status") as jc:
        await jc.read()
    assert "Authorization" not in seen[0].headers
    assert str(seen[0].url) == "https://meter.example:8443/status"


async def test_401_raises_auth_error() -> None:
    async with _client(lambda r: httpx.Response(401), username=USER, password=PW) as jc:
        with pytest.raises(JanisAuthError):
            await jc.read()
    assert issubclass(JanisAuthError, DriverError)


async def test_500_raises_protocol_error() -> None:
    async with _client(lambda r: httpx.Response(500)) as jc:
        with pytest.raises(ProtocolError):
            await jc.read()


async def test_unparseable_page_raises_protocol_error() -> None:
    async with _client(lambda r: httpx.Response(200, text="<html></html>")) as jc:
        with pytest.raises(ProtocolError):
            await jc.read()


async def test_timeout_raises_instrument_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with _client(handler) as jc:
        with pytest.raises(InstrumentTimeout):
            await jc.read()


async def test_connect_error_raises_driver_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with _client(handler) as jc:
        with pytest.raises(DriverError):
            await jc.read()


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JANIS_USERNAME", USER)
    monkeypatch.setenv("JANIS_PASSWORD", PW)
    jc = JanisClient.from_env()
    assert isinstance(jc._auth, httpx.BasicAuth)


def test_digest_scheme() -> None:
    jc = JanisClient(username=USER, password=PW, auth_scheme="digest")
    assert isinstance(jc._auth, httpx.DigestAuth)


def test_parse_real_lm510_page() -> None:
    """Captured from the lab's Cryomagnetics LM-510 on 2026-10-10."""
    page = (Path(__file__).parent / "fixtures" / "janis_real_lm510.html").read_text(encoding="latin-1")
    level, raw = parse_level_with_raw(page)
    assert (level, raw) == (-2.15, "-2.15 in")
    assert level_to_volume(level) == 0.0
