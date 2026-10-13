"""Push ATL data from the Drive CSV log to the Notion monitor page.

Every `interval` seconds:
- the "ATL Live Status" database gets one row per unit updated in place;
- each completed minute not yet sent is added to the "ATL Log" database;
- every `plot_every` cycles the trend PNG is re-rendered (also saved next to
  the log on Drive) and swapped into the first image block of the page.

Uses the public Notion REST API with an internal integration token
(``NOTION_TOKEN``); the integration must be connected to the monitor page.
Sent minutes are remembered in ``<log folder>/.notion_sent.json`` so a restart
does not duplicate rows.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .atl_summary import latest_rows, minute_rows, read_log

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TITLES = {"Unit", "Sample"}
SELECTS = {"Unit", "State", "Liquid mode"}
TEXTS = {"Minute"}


def to_properties(row: dict[str, Any], title: str) -> dict[str, Any]:
    """Convert our flat row dict (``date:<name>:start`` style) to Notion API properties."""
    props: dict[str, Any] = {}
    for key, value in row.items():
        if key.startswith("date:"):
            _, name, part = key.split(":")
            if part == "start" and value:
                props[name] = {"date": {"start": value}}
            continue
        if key == title:
            props[key] = {"title": [{"text": {"content": str(value)}}]}
        elif key in SELECTS:
            props[key] = {"select": {"name": value} if value else None}
        elif key in TEXTS:
            props[key] = {"rich_text": [{"text": {"content": str(value)}}]}
        else:
            props[key] = {"number": value}
    return props


class NotionClient:
    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self.client = client
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    async def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in range(3):
            resp = await self.client.request(method, f"{API}{path}", headers=self.headers, json=body, timeout=30)
            if resp.status_code == 429:  # rate limited: back off as told
                await asyncio.sleep(float(resp.headers.get("Retry-After", "1")) * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    async def find_by_title(self, database_id: str, title_prop: str, title: str) -> str | None:
        body = {"filter": {"property": title_prop, "title": {"equals": title}}, "page_size": 1}
        res = await self._call("POST", f"/databases/{database_id}/query", body)
        return res["results"][0]["id"] if res["results"] else None

    async def create(self, database_id: str, properties: dict) -> str:
        res = await self._call("POST", "/pages", {"parent": {"database_id": database_id}, "properties": properties})
        return res["id"]

    async def update(self, page_id: str, properties: dict) -> None:
        await self._call("PATCH", f"/pages/{page_id}", {"properties": properties})

    async def upload_png(self, path: Path) -> str:
        """Single-part file upload; returns the file_upload id."""
        upload = await self._call("POST", "/file_uploads", {"filename": path.name, "content_type": "image/png"})
        headers = {k: v for k, v in self.headers.items() if k != "Content-Type"}
        resp = await self.client.post(
            f"{API}/file_uploads/{upload['id']}/send",
            headers=headers,
            files={"file": (path.name, path.read_bytes(), "image/png")},
            timeout=60,
        )
        resp.raise_for_status()
        return upload["id"]

    async def first_image_block(self, page_id: str) -> str | None:
        res = await self._call("GET", f"/blocks/{page_id}/children?page_size=100")
        return next((b["id"] for b in res["results"] if b["type"] == "image"), None)

    async def set_image(self, block_id: str, upload_id: str) -> None:
        body = {"image": {"type": "file_upload", "file_upload": {"id": upload_id}}}
        await self._call("PATCH", f"/blocks/{block_id}", body)


class ATLNotionSync:
    """Mirrors the CSV log folder into the two Notion databases."""

    def __init__(
        self,
        notion: NotionClient,
        log_folder: Path | str,
        status_db: str,
        log_db: str,
        *,
        interval: float = 60.0,
        page_id: str | None = None,
        plot_every: int = 5,
    ) -> None:
        self.page_id, self.plot_every, self._cycle = page_id, plot_every, 0
        self.notion, self.folder = notion, Path(log_folder)
        self.status_db, self.log_db, self.interval = status_db, log_db, interval
        self.state_path = self.folder / ".notion_sent.json"
        self.sent: set[str] = set()
        self._status_pages: dict[str, str] = {}
        if self.state_path.exists():
            self.sent = set(json.loads(self.state_path.read_text(encoding="utf-8")))

    def _save_state(self) -> None:
        self.state_path.write_text(json.dumps(sorted(self.sent)), encoding="utf-8")

    async def sync_once(self) -> tuple[int, int]:
        """Returns (status rows updated, log rows added)."""
        rows = read_log(self.folder)
        updated = 0
        for row in latest_rows(rows):
            unit = row["Unit"]
            page = self._status_pages.get(unit) or await self.notion.find_by_title(self.status_db, "Unit", unit)
            props = to_properties(row, "Unit")
            if page:
                await self.notion.update(page, props)
            else:
                page = await self.notion.create(self.status_db, props)
            self._status_pages[unit] = page
            updated += 1
        added = 0
        for row in minute_rows(rows):
            if row["Sample"] in self.sent:
                continue
            await self.notion.create(self.log_db, to_properties(row, "Sample"))
            self.sent.add(row["Sample"])
            added += 1
        if added:
            self._save_state()
        self._cycle += 1
        if self.page_id and rows and (self._cycle - 1) % self.plot_every == 0:
            await self.refresh_plot(rows)
        return updated, added

    async def refresh_plot(self, rows: list[dict]) -> None:
        from .atl_plots import render_trends

        png = render_trends(rows, self.folder.parent / "plots" / "atl_trends.png")
        block = await self.notion.first_image_block(self.page_id)
        if block is None:
            log.warning("no image block on the Notion page to update")
            return
        await self.notion.set_image(block, await self.notion.upload_png(png))

    async def run(self) -> None:
        while True:
            try:
                updated, added = await self.sync_once()
                log.info("notion sync: %d status rows, %d new log rows", updated, added)
            except (httpx.HTTPError, OSError) as exc:
                log.warning("notion sync failed: %s", exc)
            await asyncio.sleep(self.interval)
