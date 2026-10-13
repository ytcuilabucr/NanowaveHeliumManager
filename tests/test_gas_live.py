"""Live test of the deployed status_now web app (only with HR_LIVE=1).

Touches only a ZZTEST tab and the Log row it creates, and removes both afterwards.
"""

import os
import uuid

import httpx
import pytest

from heliumrecovery.config import load_dotenv
from heliumrecovery.services.sheets import Row, query_params

load_dotenv()
URL, TOKEN = os.environ.get("HR_STATUS_SHEET_URL"), os.environ.get("HR_STATUS_SHEET_TOKEN")
pytestmark = pytest.mark.skipif(
    not (os.environ.get("HR_LIVE") == "1" and URL and TOKEN), reason="live test: set HR_LIVE=1 and the sheet URL/token"
)


def call(params: list[tuple[str, str]] | dict) -> dict:
    items = list(params.items()) if isinstance(params, dict) else params
    resp = httpx.post(URL, params=[("token", TOKEN), *items], follow_redirects=True, timeout=90)
    resp.raise_for_status()
    return resp.json()


def test_status_log_dump_cleanup_roundtrip() -> None:
    tab = f"ZZTEST{uuid.uuid4().hex[:6]}"
    marker = f"{uuid.uuid4().int % 10**6}.123456"
    rows = [
        Row(tab, "Last Update Time", "", "2026/10/09 12:00:00"),
        Row(tab, "Probe", "V", marker),
        Row(tab, "Flag?", "", "TRUE"),
    ]
    log_row = None
    try:
        assert call(query_params(rows)) == {"ok": True, "values": 3}
        res = call({"action": "log"})
        assert res["ok"] and res["newColumns"] == 3, res
        log_row = res["row"]

        dump = call({"action": "dump", "rows": "1"})
        assert dump["status"][tab] == {"Last Update Time": "2026/10/09 12:00:00", "Probe (V)": float(marker), "Flag?": True}
        last = dump["log"][-1]
        assert last["_row"] == log_row
        assert last[f"{tab}_Probe (V)"] == float(marker)
        assert "Total Volume (L)" in last and "Log Time" in last
    finally:
        cleaned = call({"action": "cleanup", "tab": tab, "row": str(log_row or 0)})
    assert cleaned == {"ok": True, "removedColumns": 3 if log_row else 0}
    after = call({"action": "dump", "rows": "1"})
    assert tab not in after["status"]
    assert not any(k.startswith(tab) for row in after["log"] for k in row)


def test_bad_token_rejected() -> None:
    resp = httpx.post(URL, params={"token": "wrong", "action": "dump"}, follow_redirects=True, timeout=90)
    assert resp.json() == {"ok": False, "error": "bad token"}


def test_transfer_log_roundtrip() -> None:
    """Helium transfer log: writes to a ZZTEST tab of "Helium Recovery System Operation", then removes it."""
    tab = f"ZZTEST{uuid.uuid4().hex[:6]}"
    record = {"date": "2026-10-12", "start": "13:05", "end": "14:20", "operator": f"zztest-{tab}", "atl": "ATL3",
              "atl_start": "80", "atl_end": "52.5", "cryostat": "Janis", "cryo_start": "-2.3", "cryo_end": "18.4",
              "cryo_unit": "in", "notes": "automated test", "source": "test_gas_live"}
    try:
        res = call({"action": "transfer", "tab": tab, **record})
        assert res["ok"] and res["sheet"] == "Helium Recovery System Operation" and res["tab"] == tab, res
        assert res["row"] == 2
        assert call({"action": "operators", "tab": tab})["operators"] == [f"zztest-{tab}"]
    finally:
        cleaned = call({"action": "transfer_cleanup", "tab": tab})
    assert cleaned == {"ok": True, "removed": True}
