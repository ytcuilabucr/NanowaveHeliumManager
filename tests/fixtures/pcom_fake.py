"""Fake Unitronics PCOM ASCII/TCP server for offline tests (127.0.0.1 only)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

WIDTH = {"W": 4, "NL": 8, "B": 1}
OPERAND = {"W": "MI", "NL": "ML", "B": "MB"}


def _checksum(body: bytes) -> str:
    return format(sum(body) % 256, "02X")


@dataclass
class FakePcomServer:
    """Emulates MI/ML/MB register banks. Records every request as (op, kind, addr, values)."""

    registers: dict[str, dict[int, int]] = field(
        default_factory=lambda: {"MI": {}, "ML": {}, "MB": {}}
    )
    log: list[tuple[str, str, int, list[int]]] = field(default_factory=list)
    frames: list[bytes] = field(default_factory=list)
    mangle: str | None = None  # "header", "short", "silent"
    server: asyncio.Server | None = None
    port: int = 0

    async def start(self) -> FakePcomServer:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                header = await reader.readexactly(6)
                assert header[2:4] == bytes([101, 0])
                frame = await reader.readexactly(int.from_bytes(header[4:6], "little"))
                self.frames.append(header + frame)
                reply = self._process(frame)
                if self.mangle == "silent":
                    continue
                if self.mangle == "header":
                    header = bytes([header[0] ^ 0xFF]) + header[1:]
                writer.write(header[:4] + (len(reply) + 6).to_bytes(2, "little") + reply)  # real PLC counts the header too
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    def _process(self, frame: bytes) -> bytes:
        text = frame.decode("ascii")
        assert text[0] == "/" and text[-1] == "\r"
        body, crc = text[1:-3], text[-3:-1]
        assert crc.upper() == _checksum(body.encode()), f"bad checksum in {frame!r}"
        unit, kind, rest = body[:2], body[2], body[3:]
        code = "NL" if rest.startswith("NL") else rest[0]
        rest = rest[len(code) :]
        addr, count, data = int(rest[:4], 16), int(rest[4:6], 16), rest[6:]
        width, bank = WIDTH[code], self.registers[OPERAND[code]]
        if kind == "S":
            values = [int(data[i : i + width], 16) for i in range(0, len(data), width)]
            assert len(values) == count
            for i, v in enumerate(values):
                bank[addr + i] = v
            payload = ""
        else:
            values = []
            n = count - (1 if self.mangle == "short" else 0)
            payload = "".join(format(bank.get(addr + i, 0), f"0{width}X") for i in range(n))
        self.log.append((OPERAND[code], kind, addr, values))
        # Like the real PLC: 2-char command echo, checksum over the bytes after "/A".
        echo = f"{unit}{kind}{code[0]}{payload}"
        return f"/A{echo}{_checksum(echo.encode())}\r".encode("ascii")
