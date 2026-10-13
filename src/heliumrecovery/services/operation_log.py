"""Helium transfer log: Transfer Step 3, page 4 -> "Helium Recovery System Operation".

The Apps Script web app (``apps_script/HeliumStatus.gs``, actions ``transfer`` /
``operators``) keeps the spreadsheet next to status_log on Google Drive, tab
"Helium Transfer", one row per transfer. Every record is first appended to a
local CSV (``state/transfer_log.csv``), so nothing is lost when the upload fails.

The form is pre-filled from the levels recorded when Step 1 started
(`TransferStart`) and the levels now: the ATL whose liquid volume dropped most
and the cryostat whose volume rose most are guessed, the rest is left blank
for the operator.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

import httpx

from ..core.engine import Instrument, Snapshot

log = logging.getLogger(__name__)

ATLS = {"ATL1": Instrument.ATL1, "ATL2": Instrument.ATL2, "ATL3": Instrument.ATL3}
CRYOSTATS = {"Janis": Instrument.JANIS, "CMI": Instrument.CMI, "Wei-He3": Instrument.WEI}
MIN_ATL_DROP_L = 1.0  # smaller changes are not taken as "this ATL was transferred from"
MIN_CRYO_RISE_L = 0.5


@dataclass(frozen=True)
class Level:
    value: float
    unit: str
    volume_l: float | None  # for comparing cryostats with different level units


def levels(snap: Snapshot) -> dict[str, Level]:
    """ATL liquid volumes and cryostat levels in `snap`, by display name."""
    out: dict[str, Level] = {}
    for name, slot in ATLS.items():
        d = snap.get(slot)
        if d is not None and d.liquid_vol is not None:
            out[name] = Level(float(d.liquid_vol), "L", float(d.liquid_vol))
    j = snap.get(Instrument.JANIS)
    if j is not None:
        out["Janis"] = Level(j.lhe_level_in, "in", j.lhe_volume_l)
    for name in ("CMI", "Wei-He3"):
        d = snap.get(CRYOSTATS[name])
        if d is not None and d.level is not None:
            out[name] = Level(d.level, d.level_unit or "", d.volume_l)
    return out


@dataclass
class TransferStart:
    """When Step 1 started and the levels at that moment."""

    at: datetime
    levels: dict[str, Level]


def guess(start: dict[str, Level], now: dict[str, Level], names, min_change: float, sign: int) -> str | None:
    """The name in `names` whose volume changed most in direction `sign` (at least `min_change` L)."""
    best, best_change = None, min_change
    for name in names:
        a, b = start.get(name), now.get(name)
        if a is None or b is None or a.volume_l is None or b.volume_l is None:
            continue
        change = sign * (b.volume_l - a.volume_l)
        if change >= best_change:
            best, best_change = name, change
    return best


@dataclass
class TransferRecord:
    date: str = ""  # YYYY-MM-DD
    start: str = ""  # HH:MM
    end: str = ""
    operator: str = ""
    atl: str = ""
    atl_start: float | None = None
    atl_end: float | None = None
    cryostat: str = ""
    cryo_start: float | None = None
    cryo_end: float | None = None
    cryo_unit: str = ""
    notes: str = ""
    source: str = field(default="Transfer Step 3")

    REQUIRED = ("date", "start", "end", "operator", "atl", "atl_start", "atl_end",
                "cryostat", "cryo_start", "cryo_end")

    def missing(self) -> list[str]:
        return [k for k in self.REQUIRED if getattr(self, k) in (None, "")]

    def params(self) -> list[tuple[str, str]]:
        return [(k, "" if v is None else f"{v:g}" if isinstance(v, float) else str(v))
                for k, v in asdict(self).items()]


def draft(start: TransferStart | None, now_levels: dict[str, Level], now: datetime) -> TransferRecord:
    """Pre-filled record; what cannot be known is left blank."""
    rec = TransferRecord(date=(start.at if start else now).strftime("%Y-%m-%d"), end=now.strftime("%H:%M"))
    if start is None:
        return rec
    rec.start = start.at.strftime("%H:%M")
    rec.atl = guess(start.levels, now_levels, ATLS, MIN_ATL_DROP_L, -1) or ""
    rec.cryostat = guess(start.levels, now_levels, CRYOSTATS, MIN_CRYO_RISE_L, +1) or ""
    fill_atl(rec, start, now_levels)
    fill_cryostat(rec, start, now_levels)
    return rec


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


def fill_atl(rec: TransferRecord, start: TransferStart | None, now_levels: dict[str, Level]) -> None:
    """Start/end levels of `rec.atl` (blank when unknown)."""
    a = start.levels.get(rec.atl) if start else None
    b = now_levels.get(rec.atl)
    rec.atl_start, rec.atl_end = _round(a.value if a else None), _round(b.value if b else None)


def fill_cryostat(rec: TransferRecord, start: TransferStart | None, now_levels: dict[str, Level]) -> None:
    a = start.levels.get(rec.cryostat) if start else None
    b = now_levels.get(rec.cryostat)
    rec.cryo_start, rec.cryo_end = _round(a.value if a else None), _round(b.value if b else None)
    rec.cryo_unit = (b or a).unit if (a or b) else rec.cryo_unit


class OperationLog:
    """Uploads transfer records and remembers operators (local CSV + the sheet)."""

    CSV_FIELDS = ["logged_at", *(f.name for f in fields(TransferRecord)), "uploaded"]

    def __init__(self, url: str | None, token: str | None, local_path: Path, *,
                 client: httpx.AsyncClient | None = None, tab: str | None = None) -> None:
        self.url, self.token, self.local_path, self.client, self.tab = url, token, Path(local_path), client, tab

    @property
    def remote(self) -> bool:
        return bool(self.url and self.token and self.client)

    async def _post(self, params: list[tuple[str, str]]) -> dict:
        extra = [("tab", self.tab)] if self.tab else []
        resp = await self.client.post(self.url, params=[("token", self.token), *params, *extra],
                                      timeout=60, follow_redirects=True)
        resp.raise_for_status()
        try:
            result = resp.json()
        except ValueError as exc:
            raise httpx.HTTPError(f"non-JSON reply: {resp.text[:120]}") from exc
        if not result.get("ok"):
            raise httpx.HTTPError(f"sheet error: {result.get('error')}")
        return result

    def _local_rows(self) -> list[dict[str, str]]:
        try:
            with self.local_path.open(newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f))
        except FileNotFoundError:
            return []

    def _append_local(self, rec: TransferRecord, uploaded: bool) -> None:
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.local_path.exists()
        with self.local_path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.CSV_FIELDS)
            if new:
                w.writeheader()
            w.writerow({"logged_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        **dict(rec.params()), "uploaded": "yes" if uploaded else "no"})

    async def operators(self) -> list[str]:
        """Earlier operators, most recent first: the sheet's list, then local-only names."""
        names: list[str] = []
        if self.remote:
            try:
                names = list((await self._post([("action", "operators")])).get("operators", []))
            except httpx.HTTPError as exc:
                log.warning("operator list from the sheet failed: %s", exc)
        for row in reversed(self._local_rows()):
            name = (row.get("operator") or "").strip()
            if name and name not in names:
                names.append(name)
        return names

    async def log_transfer(self, rec: TransferRecord) -> dict:
        """Upload `rec`; it is kept in the local CSV either way. Raises on upload failure."""
        if not self.remote:
            self._append_local(rec, uploaded=False)
            raise RuntimeError("no HR_STATUS_SHEET_URL/TOKEN: saved only in " + str(self.local_path))
        try:
            result = await self._post([("action", "transfer"), *rec.params()])
        except httpx.HTTPError:
            self._append_local(rec, uploaded=False)
            raise
        self._append_local(rec, uploaded=True)
        log.info("helium transfer logged: row %s of %s", result.get("row"), result.get("sheet"))
        return result
