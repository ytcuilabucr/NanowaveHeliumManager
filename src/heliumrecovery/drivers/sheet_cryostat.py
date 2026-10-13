"""Cryostat helium levels recorded by other systems in a Google Sheet.

The CMI and Wei Cryostat level meters are not read by this PC; their own
loggers write them into tabs of the production CurrentData sheet (row 1 =
"Name (unit)" headers, row 2 = latest values). This driver reads such a tab
through the status_now Apps Script web app (``action=readsheet``, which runs as
the sheet owner and only allows read access to listed spreadsheets).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel

from .base import DriverError, InstrumentTimeout, ProtocolError, now

HEADER = re.compile(r"^(?P<name>.*?)\s*\((?P<unit>[^)]*)\)\s*$")
TIME_FORMAT = "%Y/%m/%d %H:%M:%S"


class CryostatLevel(BaseModel):
    """Latest helium level of one externally logged cryostat."""

    name: str
    level: float | None = None
    level_unit: str = ""
    volume_l: float | None = None
    source_time: datetime | None = None  # when the cryostat's own logger last wrote it
    raw: dict[str, Any] = {}  # header -> value, exactly as in the sheet
    last_update: datetime  # when this PC read it

    def age(self, at: datetime | None = None) -> timedelta | None:
        return None if self.source_time is None else (at or now()) - self.source_time


def _number(v: Any) -> float | None:
    if isinstance(v, bool) or v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_tab(name: str, values: dict[str, Any]) -> CryostatLevel:
    """Map a header->value row to a CryostatLevel.

    Level = first "...Level (unit)" column; volume = "...Volume (L)". A level
    already in litres (Wei Cryostat: "He Level (L)") doubles as the volume.
    """
    level = unit = volume = None
    for header, value in values.items():
        m = HEADER.match(str(header))
        if not m:
            continue
        label, u = m["name"].lower(), m["unit"]
        if "level" in label and level is None:
            level, unit = _number(value), u
        elif "volume" in label and u.upper() == "L" and volume is None:
            volume = _number(value)
    if level is None and volume is None:
        raise ProtocolError(f"{name}: no level/volume column in {list(values)}")
    if volume is None and (unit or "").upper() == "L":
        volume = level
    source_time = None
    stamp = values.get("Last Update Time")
    if stamp:
        try:
            source_time = datetime.strptime(str(stamp), TIME_FORMAT).astimezone()
        except ValueError as exc:
            raise ProtocolError(f"{name}: bad Last Update Time {stamp!r}") from exc
    return CryostatLevel(
        name=name, level=level, level_unit=unit or "", volume_l=volume,
        source_time=source_time, raw=dict(values), last_update=now(),
    )


class SheetTabClient:
    """Reads one tab (by gid) of an allow-listed spreadsheet via the web app."""

    def __init__(
        self, url: str, token: str, sheet_id: str, gid: str, *, client: httpx.AsyncClient, timeout: float = 60.0
    ) -> None:
        self.url, self.token, self.sheet_id, self.gid = url, token, sheet_id, str(gid)
        self.client, self.timeout = client, timeout

    async def read(self) -> CryostatLevel:
        params = {"token": self.token, "action": "readsheet", "id": self.sheet_id, "gid": self.gid}
        try:
            resp = await self.client.post(self.url, params=params, follow_redirects=True, timeout=self.timeout)
            resp.raise_for_status()
            body = resp.json()
        except httpx.TimeoutException as exc:
            raise InstrumentTimeout(f"sheet tab {self.gid}: timed out") from exc
        except httpx.HTTPError as exc:
            raise DriverError(f"sheet tab {self.gid}: {exc}") from exc
        except ValueError as exc:
            raise ProtocolError(f"sheet tab {self.gid}: non-JSON reply {resp.text[:100]!r}") from exc
        if not body.get("ok"):
            raise ProtocolError(f"sheet tab {self.gid}: {body.get('error')}")
        return parse_tab(body["name"], body["values"])
