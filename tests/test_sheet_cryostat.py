"""CMI / Wei Cryostat levels read from Google Sheet tabs (web app mocked)."""

import json
from datetime import datetime, timedelta

import httpx
import pytest

from heliumrecovery.core.engine import DataEngine, Instrument, Link, Update
from heliumrecovery.core.errors import ErrorHandler, ErrorKind
from heliumrecovery.core.history import History
from heliumrecovery.core.workers import SheetCryostatPoller
from heliumrecovery.drivers.base import DriverError, ProtocolError
from heliumrecovery.drivers.sheet_cryostat import SheetTabClient, parse_tab
from heliumrecovery.services.sheets import external_rows
from heliumrecovery.ui.common import fmt_age

SHEET = "test-cryo-sheet"
NOW = datetime.now().astimezone().replace(microsecond=0)
FRESH = (NOW - timedelta(minutes=5)).strftime("%Y/%m/%d %H:%M:%S")
# Live replies seen on 2026-10-10 (Wei timestamp kept old on purpose).
TABS = {
    "1001": {"ok": True, "name": "CMI",
                   "values": {"Last Update Time": FRESH, "Level (inch)": 20.9, "Volume (L)": 44.01217}},
    "1002": {"ok": True, "name": "Wei Cryostat",
                  "values": {"Last Update Time": "2026/09/03 10:19:39", "He Level (L)": 20.2}},
}


def test_parse_cmi_level_and_volume() -> None:
    c = parse_tab("CMI", TABS["1001"]["values"])
    assert (c.level, c.level_unit, c.volume_l) == (20.9, "inch", 44.01217)
    assert c.source_time.strftime("%Y/%m/%d %H:%M:%S") == FRESH
    assert timedelta(minutes=4) < c.age() < timedelta(minutes=6)


def test_parse_wei_level_in_litres_is_also_volume() -> None:
    w = parse_tab("Wei Cryostat", TABS["1002"]["values"])
    assert (w.level, w.level_unit, w.volume_l) == (20.2, "L", 20.2)


@pytest.mark.parametrize(
    "values",
    [{"Last Update Time": FRESH, "Note": "x"}, {"Last Update Time": "yesterday", "Level (inch)": 1}],
)
def test_parse_rejects_unusable_tabs(values: dict) -> None:
    with pytest.raises(ProtocolError):
        parse_tab("X", values)


def test_parse_tolerates_blank_value() -> None:
    assert parse_tab("CMI", {"Level (inch)": "", "Volume (L)": 3}).level is None


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_client_sends_readsheet_and_parses() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=TABS[request.url.params["gid"]])

    async with _client(handler) as http:
        c = await SheetTabClient("https://s.invalid/exec", "tok", SHEET, "1001", client=http).read()
    assert c.name == "CMI"
    assert seen == [{"token": "tok", "action": "readsheet", "id": SHEET, "gid": "1001"}]


@pytest.mark.parametrize(
    ("reply", "exc"),
    [
        (httpx.Response(200, json={"ok": False, "error": "sheet not allowed"}), ProtocolError),
        (httpx.Response(200, text="<html>sign in</html>"), ProtocolError),
        (httpx.Response(500), DriverError),
    ],
)
async def test_client_errors(reply: httpx.Response, exc: type) -> None:
    async with _client(lambda r: reply) as http:
        with pytest.raises(exc):
            await SheetTabClient("https://s.invalid/exec", "t", SHEET, "1", client=http).read()


async def test_poller_fills_slots_and_flags_stale_source() -> None:
    engine, errors = DataEngine(), ErrorHandler()
    async with _client(lambda r: httpx.Response(200, json=TABS[r.url.params["gid"]])) as http:
        clients = {
            Instrument.CMI: SheetTabClient("https://s.invalid/exec", "t", SHEET, "1001", client=http),
            Instrument.WEI: SheetTabClient("https://s.invalid/exec", "t", SHEET, "1002", client=http),
        }
        await SheetCryostatPoller(clients, engine, errors, stale_after=3600).poll_once()
    snap = engine.read()
    assert snap.get(Instrument.CMI).volume_l == 44.01217 and snap.link(Instrument.CMI).connected
    assert snap.get(Instrument.WEI).level == 20.2  # value kept for the log...
    assert not snap.link(Instrument.WEI).connected  # ...but flagged
    assert "not updated since 2026-09-03" in snap.link(Instrument.WEI).last_error
    assert not errors.check(ErrorKind.SHEET_READ)


async def test_poller_records_read_errors() -> None:
    engine, errors = DataEngine(), ErrorHandler()
    async with _client(lambda r: httpx.Response(500)) as http:
        clients = {Instrument.CMI: SheetTabClient("https://s.invalid/exec", "t", SHEET, "1", client=http)}
        await SheetCryostatPoller(clients, engine, errors).poll_once()
    assert errors.get(ErrorKind.SHEET_READ).count == 1
    assert not engine.read().link(Instrument.CMI).connected


def test_external_rows_reproduce_source_headers() -> None:
    engine = DataEngine()
    for gid, slot in (("1001", Instrument.CMI), ("1002", Instrument.WEI)):
        engine.update(slot, parse_tab(TABS[gid]["name"], TABS[gid]["values"]))
    rows = external_rows(engine.read())
    headers = {(r.instr, f"{r.name} ({r.unit})" if r.unit else r.name): r.value for r in rows}
    assert headers == {
        ("CMI", "Last Update Time"): FRESH,
        ("CMI", "Level (inch)"): "20.900000",
        ("CMI", "Volume (L)"): "44.012170",
        ("Wei Cryostat", "Last Update Time"): "2026/09/03 10:19:39",
        ("Wei Cryostat", "He Level (L)"): "20.200000",
    }


def test_history_skips_repeated_samples() -> None:
    engine = DataEngine()
    h = History(engine)
    w = parse_tab("Wei Cryostat", TABS["1002"]["values"])
    for _ in range(3):
        h.on_update(Update(Instrument.WEI, w, Link(connected=True)))
    assert len(h.get("wei_cryostat_volume")) == 1


def test_fmt_age() -> None:
    assert [fmt_age(timedelta(seconds=s)) for s in (30, 600, 7200, 36 * 86400)] == ["30 s", "10 min", "2.0 h", "36 d"]
    assert fmt_age(None) == "—"
