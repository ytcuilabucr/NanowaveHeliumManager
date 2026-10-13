"""Publish live values to the status_now sheet and trigger the 3-minute log row.

Talks to the Apps Script web app in ``apps_script/HeliumStatus.gs``, which keeps
the production layout (CurrentData-style tabs + HeliumRecoveryStatusLog-style
"Log"). Uploads use the sheet parameter format (``Instr{i}``, ``pName{i}``,
``pValue{i}``, ``unit{i}``) built by `services.sheets`, plus a ``token``:
- every `status_interval` s: all current values -> status_now tabs;
- every `log_interval` s: ``action=log`` -> one row appended to the Log tab.

Config: ``HR_STATUS_SHEET_URL`` (the /exec URL) and ``HR_STATUS_SHEET_TOKEN``.
Keep the status interval >= 30 s: a consumer Google account allows about
90 min of Apps Script run time per day.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx

from ..core.engine import DataEngine
from ..core.errors import ErrorHandler, ErrorKind
from .sheets import external_rows, query_params, snapshot_rows, valve_rows

log = logging.getLogger(__name__)


class StatusPublisher:
    def __init__(
        self,
        url: str,
        token: str,
        engine: DataEngine,
        errors: ErrorHandler,
        *,
        client: httpx.AsyncClient,
        status_interval: float = 30.0,
        log_interval: float = 180.0,
    ) -> None:
        self.url, self.token, self.engine, self.errors, self.client = url, token, engine, errors, client
        self.status_interval, self.log_interval = status_interval, log_interval
        self.last_status: datetime | None = None
        self.last_log: datetime | None = None

    async def _post(self, params: list[tuple[str, str]]) -> dict[str, Any]:
        resp = await self.client.post(
            self.url, params=[("token", self.token), *params], timeout=60, follow_redirects=True
        )
        resp.raise_for_status()
        try:
            result = resp.json()
        except ValueError as exc:
            raise httpx.HTTPError(
                f"non-JSON reply (is the web app deployed for 'Anyone'?): {resp.text[:120]}"
            ) from exc
        if not result.get("ok"):
            raise httpx.HTTPError(f"sheet error: {result.get('error')}")
        return result

    async def push_status(self) -> bool:
        snap = self.engine.read()
        rows = snapshot_rows(snap) + external_rows(snap) + valve_rows(snap)
        if not rows:
            return False
        try:
            await self._post(query_params(rows))
        except httpx.HTTPError as exc:
            self.errors.set(ErrorKind.HTTP_POST, f"status_now: {exc}", source="sheets")
            log.warning("status_now update failed: %s", exc)
            return False
        self.errors.clear(ErrorKind.HTTP_POST)
        self.last_status = datetime.now().astimezone()
        return True

    async def push_log(self) -> bool:
        try:
            await self._post([("action", "log")])
        except httpx.HTTPError as exc:
            self.errors.set(ErrorKind.HTTP_POST, f"log: {exc}", source="sheets")
            log.warning("log append failed: %s", exc)
            return False
        self.last_log = datetime.now().astimezone()
        return True

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        next_log = loop.time() + self.log_interval
        await asyncio.sleep(min(15.0, self.status_interval))  # let the first readings arrive
        while True:
            ok = await self.push_status()
            if ok and loop.time() >= next_log:
                await self.push_log()
                next_log += self.log_interval
            await asyncio.sleep(self.status_interval)
