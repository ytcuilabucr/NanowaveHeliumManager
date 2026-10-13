"""Janis cryostat liquid-helium level meter (LM-510-style web page).

HTTP GET of the meter's status page, scrape the LHe level
(inches) out of the HTML table, convert to litres.
"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel

from .base import DriverError, InstrumentTimeout, ProtocolError, now

DEFAULT_HOST = os.environ.get("HR_JANIS_HOST", "")
DEFAULT_TIMEOUT_S = 10.0

LOWER_SECTION_HEIGHT_IN = 16.9
"""Height (in) of the narrow bottom section of the cryostat helium reservoir."""
LOWER_SECTION_VOLUME_L = 10.4
"""Volume (L) held by the bottom section when filled to LOWER_SECTION_HEIGHT_IN."""
UPPER_SECTION_L_PER_IN = 4.4
"""Volume per inch (L/in) of the wider section above LOWER_SECTION_HEIGHT_IN."""

_LEVEL_CELL_RE = re.compile(
    r"LHe\s*</th\s*>\s*<th\b[^>]*>(?P<cell>[^<]*)", re.IGNORECASE
)
_NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


class JanisAuthError(DriverError):
    """The level meter rejected the username/password (HTTP 401/403)."""


class JanisData(BaseModel):
    """One reading of the Janis LHe level meter (mirrors ``JanisData.ctl``)."""

    lhe_level_in: float
    lhe_volume_l: float
    last_update: datetime
    lhe_level_raw: str = ""


def level_to_volume(level_in: float) -> float:
    """Convert LHe level (inches) to volume (litres); piecewise-linear, 0 below 0."""
    if level_in < 0:
        return 0.0
    if level_in < LOWER_SECTION_HEIGHT_IN:
        return level_in / LOWER_SECTION_HEIGHT_IN * LOWER_SECTION_VOLUME_L
    return LOWER_SECTION_VOLUME_L + (level_in - LOWER_SECTION_HEIGHT_IN) * UPPER_SECTION_L_PER_IN


def parse_level_with_raw(html: str) -> tuple[float, str]:
    """Return ``(level_in, raw_text)`` from the first ``<th>LHe</th><th>...`` cell.

    Tolerates whitespace/CRLF and attributes on the ``<th>`` tag.
    """
    m = _LEVEL_CELL_RE.search(html)
    if m is None:
        raise ProtocolError("Janis page: 'LHe</th><th>' level cell not found")
    raw = m.group("cell").strip()
    num = _NUMBER_RE.match(raw)
    if num is None:
        raise ProtocolError(f"Janis page: LHe level is not a number: {raw!r}")
    value = float(num.group())
    if not math.isfinite(value):
        raise ProtocolError(f"Janis page: LHe level is not finite: {raw!r}")
    return value, raw


def parse_level(html: str) -> float:
    """Return the LHe level in inches scraped from the meter's HTML page."""
    return parse_level_with_raw(html)[0]


def _normalize_url(host: str) -> str:
    return host if re.match(r"^[a-z][a-z0-9+.-]*://", host, re.IGNORECASE) else f"http://{host}"


class JanisClient:
    """Async client for the Janis LHe level meter web page."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        username: str | None = None,
        password: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        client: httpx.AsyncClient | None = None,
        auth_scheme: Literal["basic", "digest"] = "basic",
    ) -> None:
        self.url = _normalize_url(host)
        self.timeout = timeout
        self._auth = self._make_auth(username, password, auth_scheme)
        self._client = client
        self._owns_client = client is None

    @classmethod
    def from_env(cls, host: str = DEFAULT_HOST, **kwargs) -> JanisClient:
        """Build a client with credentials from ``JANIS_USERNAME`` / ``JANIS_PASSWORD``."""
        return cls(
            host,
            username=os.environ.get("JANIS_USERNAME"),
            password=os.environ.get("JANIS_PASSWORD"),
            **kwargs,
        )

    @staticmethod
    def _make_auth(
        username: str | None, password: str | None, scheme: str
    ) -> httpx.Auth | None:
        if username is None and password is None:
            return None
        user, pw = username or "", password or ""
        if scheme == "basic":
            return httpx.BasicAuth(user, pw)
        if scheme == "digest":
            return httpx.DigestAuth(user, pw)
        raise ValueError(f"unknown auth_scheme {scheme!r}")

    async def __aenter__(self) -> JanisClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this instance created it."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # certificate checks are off for the meter's web server
            self._client = httpx.AsyncClient(verify=False)
        return self._client

    async def fetch_html(self) -> str:
        """GET the status page and return its body text."""
        client = self._get_client()
        kwargs = {"timeout": self.timeout}
        if self._auth is not None:
            kwargs["auth"] = self._auth
        try:
            resp = await client.get(self.url, **kwargs)
        except httpx.TimeoutException as e:
            raise InstrumentTimeout(f"Janis level meter at {self.url} timed out") from e
        except httpx.HTTPError as e:
            raise DriverError(f"Janis level meter at {self.url} unreachable: {e}") from e
        if resp.status_code in (401, 403):
            raise JanisAuthError(
                f"Janis level meter rejected credentials (HTTP {resp.status_code})"
            )
        if resp.status_code >= 400:
            raise ProtocolError(f"Janis level meter returned HTTP {resp.status_code}")
        return resp.text

    async def read(self) -> JanisData:
        """Fetch the page and return level, volume and timestamp."""
        html = await self.fetch_html()
        level, raw = parse_level_with_raw(html)
        return JanisData(
            lhe_level_in=level,
            lhe_volume_l=level_to_volume(level),
            last_update=now(),
            lhe_level_raw=raw,
        )
