"""ATL helium liquefier driver: reads `/SSI/varlist.xml` and sets the liquefaction mode over HTTP.

Reading parses the status variables of ``/SSI/varlist.xml``. Setting the mode copies what the unit's
own web page does (``Liquify.html`` / ``Stop.html``, the same on all three
units): a form POST to ``/WRITEPI`` with ``ADR1=MW8081`` and ``VALUE1=<code>``.
Mode codes and names are those of the unit's ``scripts.js``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from types import TracebackType
from typing import Self

import httpx
from pydantic import BaseModel, Field

from .base import DriverError, InstrumentTimeout, ProtocolError, now

log = logging.getLogger(__name__)

DEFAULT_PORT = 8080
DEFAULT_TIMEOUT = 2.0
VARLIST_PATH = "/SSI/varlist.xml"
WRITE_PATH = "/WRITEPI"
MODE_REGISTER = "MW8081"  # liquefaction mode word, as posted by the unit's own pages
STATUSVAR_COUNT = 5


class LiquidMode(IntEnum):
    """`liquidmode` codes (unit firmware, scripts.js)."""

    STOP = 0
    RECOVERY = 1  # "Direct recovery"
    SLOW = 2  # "Liquefy slow"
    MEDIUM = 3
    FAST = 4
    PURGE = 5
    TRANSFER = 6  # precool / transfer; detail in `transfermode`


LIQUID_MODE_NAMES = {
    LiquidMode.STOP: "Stop", LiquidMode.RECOVERY: "Recovery", LiquidMode.SLOW: "Slow",
    LiquidMode.MEDIUM: "Medium", LiquidMode.FAST: "Fast", LiquidMode.PURGE: "Purge",
    LiquidMode.TRANSFER: "Transfer",
}
TRANSFER_MODE_NAMES = ("Precool slow", "Precool medium", "Precool fast",
                       "Transfer slow", "Transfer medium", "Transfer fast")


@dataclass(frozen=True)
class ATLModel:
    """What a liquefier model accepts and how it names its modes.

    All units serve the same web page and codes, but the models differ: an
    ATL80 runs Recovery/Slow/Medium/Fast, while the ATL3 unit only liquefies
    (code 2, which it calls "Liquefy"). Both can be stopped (code 0, "Stop"
    then "OK" on the unit's page); the compressor then needs
    `COMPRESSOR_COOLDOWN_S` before it may be started again.
    """

    name: str
    settable: tuple[LiquidMode, ...]
    names: Mapping[LiquidMode, str] = field(default_factory=dict)

    def mode_name(self, mode: LiquidMode) -> str:
        return self.names.get(mode, LIQUID_MODE_NAMES[mode])

    def mode_text(self, mode: float | None, transfer_mode: float | None = None) -> str:
        return liquid_mode_text(mode, transfer_mode, self.names)


ATL80 = ATLModel("ATL80", (LiquidMode.STOP, LiquidMode.RECOVERY, LiquidMode.SLOW, LiquidMode.MEDIUM,
                          LiquidMode.FAST))
ATL_LIQUEFY_ONLY = ATLModel("ATL (liquefy only)", (LiquidMode.STOP, LiquidMode.SLOW), {LiquidMode.SLOW: "Liquefy"})
COMPRESSOR_COOLDOWN_S = 60.0  # after a stop, the compressor must cool this long before any start


def liquid_mode_text(
    mode: float | None, transfer_mode: float | None = None, names: Mapping[LiquidMode, str] | None = None
) -> str:
    """Display name of a `liquidmode` code ("Slow", "Transfer fast", "code 9", "—").

    `names` overrides the default names for a model (e.g. ATL3: 2 -> "Liquefy").
    """
    if mode is None or math.isnan(mode):
        return "—"
    code = int(mode)
    if code == LiquidMode.TRANSFER and transfer_mode is not None and not math.isnan(transfer_mode):
        t = int(transfer_mode)
        if 0 <= t < len(TRANSFER_MODE_NAMES):
            return TRANSFER_MODE_NAMES[t]
    try:
        m = LiquidMode(code)
    except ValueError:
        return f"code {code}"
    return (names or {}).get(m, LIQUID_MODE_NAMES[m])

_SCALAR_TAGS = (
    "statusmachine",
    "liquidmode",
    "transfermode",
    "statusleds",
    "dewarcapacity",
    "liquidvolume",
    "heaterpower",
)
_LIST_SPLIT = re.compile(r"[,;\s]+")


class ATLStatus(BaseModel):
    """One ATL liquefier reading (mirrors `ATL Status Cluster.ctl`)."""

    diode_temp: float = Field(description="Diode temperature, K (statusvar[0]; 'Diorde T' in the sheets).")
    dewar_pressure: float = Field(description="Dewar pressure, psi (statusvar[1]).")
    input_gas_flow: float = Field(description="Input gas flow, unit unconfirmed (statusvar[2]).")
    liquid_vol: float = Field(description="Liquid volume from statusvar[3], L (assumed).")
    liquefaction_rate: float = Field(description="Liquefaction rate, L/day (statusvar[4]).")
    status_machine: float = Field(description="State-machine code (raw).")
    liquid_mode: float = Field(description="Liquid mode code (raw).")
    transfer_mode: float = Field(description="Transfer mode code (raw).")
    status_leds: float = Field(description="Status LED bitfield/code (raw).")
    dewar_capacity: float = Field(description="Dewar capacity, L.")
    liquid_volume: float = Field(description="Liquid volume from `liquidvolume` tag, L (assumed).")
    heater_power: float = Field(description="Heater power, %.")
    last_update: datetime | None = Field(description="UTC time of the reading; None when unavailable.")

    @classmethod
    def unavailable(cls) -> ATLStatus:
        """All-NaN placeholder for a failed read."""
        nan = math.nan
        return cls(
            diode_temp=nan, dewar_pressure=nan, input_gas_flow=nan, liquid_vol=nan,
            liquefaction_rate=nan, status_machine=nan, liquid_mode=nan, transfer_mode=nan,
            status_leds=nan, dewar_capacity=nan, liquid_volume=nan, heater_power=nan,
            last_update=None,
        )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _to_float(text: str | None, tag: str) -> float:
    try:
        return float((text or "").strip())
    except ValueError as exc:
        raise ProtocolError(f"ATL <{tag}> is not a number: {text!r}") from exc


def parse_varlist(xml_text: str | bytes) -> ATLStatus:
    """Parse an ATL `varlist.xml` body into an `ATLStatus`.

    Tags are matched anywhere in the document, case-insensitively and ignoring
    namespaces. `statusvar` may be repeated elements (document order) or a
    single element holding a comma/semicolon/space separated list.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ProtocolError(f"ATL varlist is not valid XML: {exc}") from exc

    statusvar: list[float] = []
    scalars: dict[str, float] = {}
    for elem in root.iter():
        tag = _local(elem.tag)
        if tag == "statusvar":
            text = (elem.text or "").strip()
            parts = [p for p in _LIST_SPLIT.split(text) if p] if text else []
            if not parts:
                raise ProtocolError("ATL <statusvar> is empty")
            statusvar.extend(_to_float(p, tag) for p in parts)
        elif tag in _SCALAR_TAGS and tag not in scalars:
            scalars[tag] = _to_float(elem.text, tag)

    if len(statusvar) < STATUSVAR_COUNT:
        raise ProtocolError(
            f"ATL varlist has {len(statusvar)} statusvar values, need {STATUSVAR_COUNT}"
        )
    missing = [t for t in _SCALAR_TAGS if t not in scalars]
    if missing:
        raise ProtocolError(f"ATL varlist missing tags: {', '.join(missing)}")

    return ATLStatus(
        diode_temp=statusvar[0],
        dewar_pressure=statusvar[1],
        input_gas_flow=statusvar[2],
        liquid_vol=statusvar[3],
        liquefaction_rate=statusvar[4],
        status_machine=scalars["statusmachine"],
        liquid_mode=scalars["liquidmode"],
        transfer_mode=scalars["transfermode"],
        status_leds=scalars["statusleds"],
        dewar_capacity=scalars["dewarcapacity"],
        liquid_volume=scalars["liquidvolume"],
        heater_power=scalars["heaterpower"],
        last_update=now(),
    )


class ATLClient:
    """HTTP client for one ATL liquefier.

    Pass a shared `httpx.AsyncClient` to poll several units over one
    connection pool; the injected client is never closed by this class.
    `auth` is any httpx auth (e.g. `("user", "user")` or `httpx.DigestAuth`).
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
        retries: int = 2,
        retry_delay: float = 0.5,
        allow_writes: bool = False,
        model: ATLModel = ATL80,
    ) -> None:
        self.host = host
        self.allow_writes = allow_writes  # HR_ATL_ALLOW_WRITES; reading never needs it
        self.model = model
        self.port = port
        self.timeout = timeout
        self.auth = auth
        self.retries, self.retry_delay = retries, retry_delay
        self.retries_used = 0  # transient failures recovered by a retry (diagnostics)
        self._client = client
        self._owns_client = client is None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{VARLIST_PATH}"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP client if this instance created it."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def fetch_xml(self) -> str:
        """GET the raw varlist.xml body.

        The ATL's embedded web server occasionally drops a connection mid-request
        (seen as httpx.ReadError ~100 ms after sending, often on all units at once;
        measured ~1 in 160 requests with keep-alive, far more often with a new
        connection per request). Transport errors are therefore retried a couple
        of times after a short pause; HTTP and parse errors are not.
        """
        kwargs: dict = {"timeout": self.timeout}
        if self.auth is not None:
            kwargs["auth"] = self.auth
        for attempt in range(self.retries + 1):
            try:
                resp = await self._get_client().get(self.url, **kwargs)
                break
            except httpx.TransportError as exc:
                if attempt < self.retries:
                    self.retries_used += 1
                    log.debug("ATL %s: %s, retrying", self.host, type(exc).__name__)
                    await asyncio.sleep(self.retry_delay)
                    continue
                if isinstance(exc, httpx.TimeoutException):
                    raise InstrumentTimeout(f"ATL {self.host}: no reply within {self.timeout} s") from exc
                raise DriverError(f"ATL {self.host}: {type(exc).__name__}: {exc}") from exc
            except httpx.HTTPError as exc:
                raise DriverError(f"ATL {self.host}: {type(exc).__name__}: {exc}") from exc
        if resp.status_code != 200:
            raise ProtocolError(f"ATL {self.host}: HTTP {resp.status_code}")
        return resp.text

    async def read_status(self) -> ATLStatus:
        """Fetch and parse the current status."""
        return parse_varlist(await self.fetch_xml())

    async def set_liquid_mode(
        self, mode: LiquidMode | int, *, confirm_timeout: float = 15.0, poll_interval: float = 1.0
    ) -> ATLStatus:
        """Switch the liquefaction mode and wait until `liquidmode` reads it back.

        Sends exactly one POST (no automatic resend: a dropped connection may
        still have been applied, so the read-back decides). Raises DriverError
        when writes are disabled, the mode is not one the unit's pages offer,
        or the unit does not report the new mode within `confirm_timeout` s.
        """
        try:
            mode = LiquidMode(int(mode))
        except ValueError as exc:
            raise DriverError(f"ATL {self.host}: unknown liquid mode {mode!r}") from exc
        if mode not in self.model.settable:
            allowed = ", ".join(self.model.mode_name(m) for m in self.model.settable)
            raise DriverError(f"ATL {self.host} ({self.model.name}): mode {mode.name} not allowed; "
                              f"choose {allowed}")
        if not self.allow_writes:
            raise DriverError("ATL writes are disabled (set HR_ATL_ALLOW_WRITES=1)")

        log.warning("ATL %s: set liquid mode -> %s (%d)", self.host, self.model.mode_name(mode), mode)
        kwargs: dict = {"timeout": self.timeout, "data": {"VALUE1": str(int(mode)), "ADR1": MODE_REGISTER}}
        if self.auth is not None:
            kwargs["auth"] = self.auth
        send_error: str | None = None
        try:
            resp = await self._get_client().post(f"http://{self.host}:{self.port}{WRITE_PATH}", **kwargs)
            if resp.status_code >= 400:
                raise ProtocolError(f"ATL {self.host}: mode write refused, HTTP {resp.status_code}")
        except httpx.TransportError as exc:  # may still have been applied; the read-back decides
            send_error = f"{type(exc).__name__}: {exc}"
            log.warning("ATL %s: mode write connection error (%s), checking read-back", self.host, send_error)
        except httpx.HTTPError as exc:
            raise DriverError(f"ATL {self.host}: {type(exc).__name__}: {exc}") from exc

        loop = asyncio.get_running_loop()
        deadline = loop.time() + confirm_timeout
        last: ATLStatus | None = None
        while True:
            try:
                last = await self.read_status()
                if int(last.liquid_mode) == mode:
                    return last
            except DriverError:
                pass  # transient read failure; keep checking until the deadline
            if loop.time() >= deadline:
                break
            await asyncio.sleep(poll_interval)
        seen = "no reading" if last is None else self.model.mode_text(last.liquid_mode, last.transfer_mode)
        cause = f" (send error: {send_error})" if send_error else ""
        raise DriverError(
            f"ATL {self.host}: mode did not read back as {self.model.mode_name(mode)} "
            f"within {confirm_timeout:g} s, unit reports {seen}{cause}"
        )


async def read_all(clients: Mapping[str, ATLClient]) -> dict[str, ATLStatus | DriverError]:
    """Read several units concurrently; failures are returned, not raised."""
    names = list(clients)
    results = await asyncio.gather(
        *(clients[n].read_status() for n in names), return_exceptions=True
    )
    out: dict[str, ATLStatus | DriverError] = {}
    for name, res in zip(names, results):
        if isinstance(res, (ATLStatus, DriverError)):
            out[name] = res
        else:
            raise res
    return out
