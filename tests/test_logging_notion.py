"""CSV logger, minute aggregation and Notion sync (offline; Notion API mocked)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from heliumrecovery.core.engine import Instrument
from heliumrecovery.drivers.atl import parse_varlist
from heliumrecovery.services.atl_summary import latest_rows, minute_rows, read_log
from heliumrecovery.services.csv_logger import ATLCsvLogger
from heliumrecovery.services.notion_sync import ATLNotionSync, NotionClient, to_properties
from heliumrecovery.core.engine import DataEngine

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 10, 8, 12, 0, 5, tzinfo=timezone.utc)


def _fill_log(folder: Path, minutes: int = 3) -> None:
    logger = ATLCsvLogger(DataEngine(), folder)
    base = parse_varlist((FIXTURES / "atl_real_81.xml").read_bytes())
    for i in range(minutes * 6):  # every 10 s
        t = T0 + timedelta(seconds=10 * i)
        for slot, vol in ((Instrument.ATL1, 60 + i), (Instrument.ATL3, 40.0)):
            logger.write(slot, base.model_copy(update={"last_update": t, "liquid_volume": float(vol)}))


def test_csv_log_and_minute_averages(tmp_path: Path) -> None:
    _fill_log(tmp_path)
    rows = read_log(tmp_path)
    assert len(rows) == 36
    minutes = minute_rows(rows)
    assert len(minutes) == 4  # 3 minutes x 2 units, newest (still filling) minute dropped
    first = minutes[0]
    assert first["Unit"] == "ATL1" and first["Readings"] == 6
    assert first["Liquid vol (L)"] == 62.5  # mean of 60..65
    assert first["Minute"].count(":") == 1 and "-" in first["Minute"]


def test_latest_rows_state_and_mode(tmp_path: Path) -> None:
    _fill_log(tmp_path, minutes=1)
    latest = {r["Unit"]: r for r in latest_rows(read_log(tmp_path))}
    # fixture captured from ATL1 with liquidmode 2 = "LIQUEFY SLOW" on the unit's own page
    assert latest["ATL1"]["State"] == "Cold" and latest["ATL1"]["Liquid mode"] == "Slow"
    assert latest["ATL3"]["Liquid mode"] == "Liquefy"  # same code 2, but the ATL3 model calls it Liquefy


def test_to_properties() -> None:
    props = to_properties(
        {"Unit": "ATL1", "State": "Cold", "Diode T (K)": 4.2, "Minute": "10-08 12:00",
         "date:Last update:start": "2026-10-08T12:00:00+00:00", "date:Last update:is_datetime": 1},
        "Unit",
    )
    assert props["Unit"]["title"][0]["text"]["content"] == "ATL1"
    assert props["State"] == {"select": {"name": "Cold"}}
    assert props["Diode T (K)"] == {"number": 4.2}
    assert props["Last update"] == {"date": {"start": "2026-10-08T12:00:00+00:00"}}
    assert props["Minute"]["rich_text"][0]["text"]["content"] == "10-08 12:00"


async def test_notion_sync_upserts_and_dedupes(tmp_path: Path) -> None:
    _fill_log(tmp_path)
    calls: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.method, request.url.path, body))
        assert request.headers["authorization"] == "Bearer secret"
        if request.url.path.endswith("/query"):
            title = body["filter"]["title"]["equals"]
            return httpx.Response(200, json={"results": [{"id": f"page-{title}"}] if title == "ATL1" else []})
        return httpx.Response(200, json={"id": "new-page"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sync = ATLNotionSync(NotionClient("secret", http), tmp_path, "status-db", "log-db")
        assert await sync.sync_once() == (2, 4)
        patched = [c for c in calls if c[0] == "PATCH"]
        assert [c[1] for c in patched] == ["/v1/pages/page-ATL1"]  # ATL1 existed: updated
        created = [c for c in calls if c[0] == "POST" and c[1] == "/v1/pages"]
        assert sum(c[2]["parent"]["database_id"] == "log-db" for c in created) == 4
        calls.clear()
        assert await sync.sync_once() == (2, 0)  # nothing re-sent
        assert not any(c[1].endswith("/query") for c in calls)  # status page ids cached

    again = ATLNotionSync(NotionClient("secret", httpx.AsyncClient()), tmp_path, "s", "l")
    assert len(again.sent) == 4  # remembered across restarts


async def test_notion_sync_refreshes_plot(tmp_path: Path) -> None:
    log_dir = tmp_path / "atl_log"
    _fill_log(log_dir)
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json={"results": []})
        if request.url.path == "/v1/blocks/page-1/children":
            return httpx.Response(200, json={"results": [{"id": "txt", "type": "paragraph"}, {"id": "img", "type": "image"}]})
        if request.url.path == "/v1/file_uploads":
            return httpx.Response(200, json={"id": "up-1"})
        return httpx.Response(200, json={"id": "x"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sync = ATLNotionSync(NotionClient("t", http), log_dir, "s", "l", page_id="page-1", plot_every=5)
        await sync.sync_once()
        assert (tmp_path / "plots" / "atl_trends.png").stat().st_size > 10_000
        assert ("POST", "/v1/file_uploads/up-1/send") in seen
        assert ("PATCH", "/v1/blocks/img") in seen
        seen.clear()
        await sync.sync_once()  # cycle 2 of 5: no re-upload
        assert ("PATCH", "/v1/blocks/img") not in seen
