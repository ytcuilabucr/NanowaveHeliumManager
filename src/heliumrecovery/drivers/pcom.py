"""Unitronics PCOM ASCII-over-TCP codec and async client.

Builds and decodes PCOM frames (checksum, command message, query, response,
read / write data). Verified against the real PLC (2026-10-08): hex must be uppercase, ML replies
echo only "RN", and reply checksums are sum(bytes after "/A") mod 256.
"""

from __future__ import annotations

import asyncio
from enum import Enum

from .base import DriverError, InstrumentTimeout, ProtocolError

ETHERNET_PROTOCOL_ASCII = 101
ETH_HEADER_LEN = 6
PLC_HEADER_LEN = 6  # "/A" + unit id (2) + command echo (2)
PLC_TRAILER_LEN = 3  # checksum (2) + "\r"


class Operand(Enum):
    """PCOM operand types: (command code, hex digits per value)."""

    MI = ("W", 4)
    ML = ("NL", 8)
    MB = ("B", 1)

    @property
    def code(self) -> str:
        return self.value[0]

    @property
    def width(self) -> int:
        return self.value[1]


def checksum(body: str | bytes, *, mod255: bool = False, uppercase: bool = True) -> str:
    """Two-hex-digit checksum of `body` (the text between '/' and the checksum).

    The Unitronics spec (and the PLC's own replies) use plain mod 256.
    ``mod255=True`` is a legacy variant with an extra remainder-by-255, which sends
    "00" instead of "FF" (only reachable for some writes).
    """
    data = body.encode("ascii") if isinstance(body, str) else body
    value = sum(data) % 256
    if mod255:
        value %= 255
    return format(value, "02X" if uppercase else "02x")


def build_command(
    operand: Operand,
    start: int,
    count: int,
    values: list[int] | None = None,
    *,
    unit_id: int = 0,
    uppercase: bool = True,
    mod255: bool = False,
) -> tuple[bytes, int]:
    """Build a PCOM ASCII frame; return (frame, expected reply length w/o Ethernet header).

    ``values=None`` builds a read ("R" prefix), otherwise a write ("S" prefix).
    """
    if not 0 <= unit_id <= 99:
        raise ValueError(f"unit_id {unit_id} out of range 0..99")
    if not 0 <= start <= 0xFFFF:
        raise ValueError(f"start address {start} out of range")
    if not 0 <= count <= 0xFF:
        raise ValueError(f"count {count} out of range 0..255")
    x = "X" if uppercase else "x"
    if values is None:
        action, data, reply_data = "R", "", count * operand.width
    else:
        if len(values) != count:
            raise ValueError("len(values) must equal count")
        limit = 1 << (4 * operand.width)
        for v in values:
            if not isinstance(v, int) or not 0 <= v < limit:
                raise ValueError(f"value {v!r} does not fit operand {operand.name}")
        data = "".join(format(v, f"0{operand.width}{x}") for v in values)
        action, reply_data = "S", 0
    body = f"{unit_id:02d}{action}{operand.code}{start:04{x}}{count:02{x}}{data}"
    frame = f"/{body}{checksum(body, mod255=mod255, uppercase=uppercase)}\r"
    return frame.encode("ascii"), PLC_HEADER_LEN + reply_data + PLC_TRAILER_LEN


def wrap_ethernet(frame: bytes, transaction_id: int) -> bytes:
    """Prefix the 6-byte Ethernet header: [tid lo, tid hi, 101, 0, len lo, len hi]."""
    if not 0 <= transaction_id <= 0xFFFF:
        raise ValueError("transaction_id must fit in U16")
    tid = transaction_id.to_bytes(2, "little")
    length = len(frame).to_bytes(2, "little")
    return tid + bytes([ETHERNET_PROTOCOL_ASCII, 0]) + length + frame


def _framed(plc: bytes) -> bool:
    return plc.startswith(b"/A") and plc.endswith(b"\r")


def check_reply_checksum(plc: bytes) -> None:
    """The PLC checksums the bytes between "/A" and the checksum, mod 256."""
    if not _framed(plc) or len(plc) < 5:
        raise ProtocolError(f"Malformed PLC reply: {plc!r}")
    if plc[-3:-1].upper() != checksum(plc[2:-3]).encode("ascii"):
        raise ProtocolError(f"Bad reply checksum: {plc!r}")


def check_header(reply: bytes, request: bytes) -> None:
    """Compare the first 4 header bytes of reply and request ."""
    if reply[:4] != request[:4]:
        raise ProtocolError("Transaction header doesn't match")


def decode_response(reply: bytes, request: bytes, operand: Operand, count: int) -> list[int]:
    """Decode a full read reply (Ethernet header included) into unsigned ints."""
    check_header(reply, request)
    plc = reply[ETH_HEADER_LEN:]
    check_reply_checksum(plc)
    data = plc[PLC_HEADER_LEN:-PLC_TRAILER_LEN] if len(plc) >= PLC_HEADER_LEN + PLC_TRAILER_LEN else b""
    if len(data) != (count & 0xFF) * operand.width:
        raise ProtocolError("Response data count doesn't match")
    w = operand.width
    try:
        return [int(data[i : i + w], 16) for i in range(0, len(data), w)]
    except ValueError as exc:
        raise ProtocolError(f"Non-hex data in reply: {data!r}") from exc


class PcomClient:
    """Async PCOM ASCII client over TCP. Requests are serialised with a lock."""

    def __init__(
        self,
        host: str,
        port: int = 502,
        *,
        unit_id: int = 0,
        timeout: float = 1.0,
        uppercase: bool = True,
        mod255: bool = False,
    ) -> None:
        self.host, self.port, self.unit_id, self.timeout = host, port, unit_id, timeout
        self.uppercase, self.mod255 = uppercase, mod255
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._tid = 0

    async def connect(self) -> None:
        if self._writer is not None:
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )
        except TimeoutError as exc:
            raise InstrumentTimeout(f"connect to {self.host}:{self.port} timed out") from exc
        except OSError as exc:
            raise DriverError(f"connect to {self.host}:{self.port} failed: {exc}") from exc

    async def close(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def __aenter__(self) -> PcomClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _next_tid(self) -> int:
        tid, self._tid = self._tid, (self._tid + 1) & 0xFFFF
        return tid

    async def _transact(self, frame: bytes, reply_len: int) -> tuple[bytes, bytes]:
        async with self._lock:
            await self.connect()
            assert self._reader is not None and self._writer is not None
            request = wrap_ethernet(frame, self._next_tid())
            try:
                self._writer.write(request)
                await self._writer.drain()
                # Read up to the terminating CR rather than a fixed length so
                # error replies (shorter than expected) don't stall until timeout.
                reply = await asyncio.wait_for(self._read_reply(), self.timeout)
            except TimeoutError as exc:
                await self.close()  # stream state unknown; reconnect next time
                raise InstrumentTimeout(f"no PCOM reply within {self.timeout}s") from exc
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError) as exc:
                await self.close()
                raise DriverError(f"PCOM connection lost: {exc}") from exc
            return request, reply

    async def _read_reply(self) -> bytes:
        assert self._reader is not None
        header = await self._reader.readexactly(ETH_HEADER_LEN)
        return header + await self._reader.readuntil(b"\r")

    async def read(self, operand: Operand, start: int, qty: int) -> list[int]:
        """Read `qty` consecutive registers starting at `start`."""
        frame, reply_len = build_command(
            operand, start, qty, unit_id=self.unit_id, uppercase=self.uppercase, mod255=self.mod255
        )
        request, reply = await self._transact(frame, reply_len)
        try:
            return decode_response(reply, request, operand, qty)
        except ProtocolError:
            await self.close()
            raise

    async def write(self, operand: Operand, start: int, values: list[int]) -> None:
        """Write `values` to consecutive registers starting at `start`."""
        frame, reply_len = build_command(
            operand,
            start,
            len(values),
            list(values),
            unit_id=self.unit_id,
            uppercase=self.uppercase,
            mod255=self.mod255,
        )
        request, reply = await self._transact(frame, reply_len)
        try:
            check_header(reply, request)
            check_reply_checksum(reply[ETH_HEADER_LEN:])
        except ProtocolError:
            await self.close()
            raise
