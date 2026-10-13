"""Helium transfer log (Transfer Step 3, page 4): pre-fill, guesses, upload + local CSV."""

import asyncio
import csv
from datetime import datetime
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlparse

import httpx

from heliumrecovery.core.engine import Instrument
from heliumrecovery.services import operation_log as oplog


class Snap:
    def __init__(self, atl=(50.0, 0.0, 80.0), janis=(-2.3, 0.0), cmi=(9.3, 7.5)):
        self.values = {
            Instrument.ATL1: NS(liquid_vol=atl[0]), Instrument.ATL2: NS(liquid_vol=atl[1]),
            Instrument.ATL3: NS(liquid_vol=atl[2]),
            Instrument.JANIS: NS(lhe_level_in=janis[0], lhe_volume_l=janis[1]),
            Instrument.CMI: NS(level=cmi[0], level_unit="inch", volume_l=cmi[1]),
        }

    def get(self, slot):
        return self.values.get(slot)


START = datetime(2026, 10, 12, 13, 5)
NOW = datetime(2026, 10, 12, 14, 20)


def test_draft_guesses_atl_and_cryostat_from_level_changes() -> None:
    start = oplog.TransferStart(START, oplog.levels(Snap()))
    # ATL3 dropped 80 -> 52 L, Janis rose -2.3 in (0 L) -> 18.4 in (16.4 L); ATL1/CMI only drift
    rec = oplog.draft(start, oplog.levels(Snap(atl=(49.8, 0.0, 52.0), janis=(18.4, 16.4), cmi=(9.2, 7.4))), NOW)
    assert (rec.date, rec.start, rec.end) == ("2026-10-12", "13:05", "14:20")
    assert (rec.atl, rec.atl_start, rec.atl_end) == ("ATL3", 80.0, 52.0)
    assert (rec.cryostat, rec.cryo_start, rec.cryo_end, rec.cryo_unit) == ("Janis", -2.3, 18.4, "in")
    assert rec.missing() == ["operator"]


def test_draft_leaves_blank_what_it_cannot_know() -> None:
    rec = oplog.draft(None, oplog.levels(Snap()), NOW)  # no Step 1 since the program started
    assert rec.date == "2026-10-12" and rec.end == "14:20"
    assert rec.missing() == ["start", "operator", "atl", "atl_start", "atl_end", "cryostat", "cryo_start", "cryo_end"]
    same = oplog.TransferStart(START, oplog.levels(Snap()))
    rec = oplog.draft(same, oplog.levels(Snap()), NOW)  # nothing changed: no guess
    assert rec.atl == "" and rec.cryostat == ""


def test_choosing_another_atl_or_cryostat_fills_its_levels() -> None:
    start = oplog.TransferStart(START, oplog.levels(Snap()))
    rec = oplog.TransferRecord(atl="ATL1", cryostat="CMI")
    now = oplog.levels(Snap(atl=(30.0, 0.0, 80.0), cmi=(20.0, 15.0)))
    oplog.fill_atl(rec, start, now)
    oplog.fill_cryostat(rec, start, now)
    assert (rec.atl_start, rec.atl_end, rec.cryo_start, rec.cryo_end, rec.cryo_unit) == (50.0, 30.0, 9.3, 20.0, "inch")
    rec.cryostat = "Other dewar"  # typed by the operator: nothing known
    oplog.fill_cryostat(rec, start, now)
    assert (rec.cryo_start, rec.cryo_end) == (None, None)


FULL = oplog.TransferRecord(date="2026-10-12", start="13:05", end="14:20", operator="Alice", atl="ATL3",
                            atl_start=80.0, atl_end=52.0, cryostat="Janis", cryo_start=-2.3, cryo_end=18.4,
                            cryo_unit="in")


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_log_transfer_uploads_and_keeps_local_copy(tmp_path) -> None:
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query, keep_blank_values=True).items()}
        seen.append(q)
        if q["action"] == "operators":
            return httpx.Response(200, json={"ok": True, "operators": ["Bob", "Alice"]})
        return httpx.Response(200, json={"ok": True, "row": 2, "sheet": oplog.__name__})

    async def run():
        log = oplog.OperationLog("https://x/exec", "tok", tmp_path / "t.csv", client=_client(handler))
        await log.log_transfer(FULL)
        return await log.operators()

    names = asyncio.run(run())
    q = seen[0]
    assert q["token"] == "tok" and q["action"] == "transfer"
    assert (q["operator"], q["atl"], q["atl_start"], q["cryo_end"], q["notes"]) == ("Alice", "ATL3", "80", "18.4", "")
    rows = list(csv.DictReader((tmp_path / "t.csv").open(encoding="utf-8")))
    assert rows[0]["operator"] == "Alice" and rows[0]["uploaded"] == "yes"
    assert names == ["Bob", "Alice"]


def test_failed_upload_is_kept_locally_and_operators_still_offered(tmp_path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def run():
        log = oplog.OperationLog("https://x/exec", "tok", tmp_path / "t.csv", client=_client(handler))
        try:
            await log.log_transfer(FULL)
        except httpx.HTTPError:
            pass
        else:
            raise AssertionError("upload should fail")
        return await log.operators()

    assert asyncio.run(run()) == ["Alice"]  # from the local CSV
    rows = list(csv.DictReader((tmp_path / "t.csv").open(encoding="utf-8")))
    assert rows[0]["uploaded"] == "no"


def test_without_sheet_url_record_is_saved_locally(tmp_path) -> None:
    log = oplog.OperationLog(None, None, tmp_path / "t.csv")
    try:
        asyncio.run(log.log_transfer(FULL))
    except RuntimeError as exc:
        assert "saved only" in str(exc)
    assert (tmp_path / "t.csv").exists()
