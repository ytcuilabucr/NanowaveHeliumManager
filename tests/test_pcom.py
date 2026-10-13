"""PCOM codec golden frames and client error handling (offline only)."""

import pytest

from fixtures.pcom_fake import FakePcomServer
from heliumrecovery.drivers.base import DriverError, InstrumentTimeout, ProtocolError
from heliumrecovery.drivers.pcom import (
    Operand,
    PcomClient,
    build_command,
    checksum,
    decode_response,
    wrap_ethernet,
)

# Checksum = sum of bytes after '/' mod 256 (Unitronics spec, confirmed on the real PLC).
# "00RNL00050B": 48+48+82+78+76+48+48+48+53+48+66 = 643 -> 643 % 256 = 131 = 0x83


def test_checksum_matches_hand_calc() -> None:
    assert checksum("00RNL00050B") == "83"
    assert checksum("00RNL00050b", uppercase=False) == "a3"  # 675 % 256 = 163


def test_checksum_mod255_variant() -> None:
    # Byte-sum 0xFF: Unitronics spec (default) gives FF; the legacy extra remainder by 255 gives 00.
    assert checksum(b"\xff") == "FF"
    assert checksum(b"\xff", mod255=True) == "00"
    assert checksum(b"\x80\x80") == checksum(b"\x80\x80", mod255=True) == "00"


def test_read_ml_frame() -> None:
    frame, reply_len = build_command(Operand.ML, 5, 11)
    assert frame == b"/00RNL00050B83\r"
    assert reply_len == 4 + 2 + 11 * 8 + 3


def test_read_ml_frame_lowercase() -> None:
    frame, _ = build_command(Operand.ML, 5, 11, uppercase=False)
    assert frame == b"/00RNL00050ba3\r"


def test_read_mi_and_mb_frames() -> None:
    # "00RW076C01": 48+48+82+87 + 48+55+54+67 + 48+49 = 586 -> 586-512 = 74 = 0x4A
    assert build_command(Operand.MI, 1900, 1) == (b"/00RW076C014A\r", 13)
    # "00RB000B01": 48+48+82+66 + 48+48+48+66 + 48+49 = 551 -> 39 = 0x27
    assert build_command(Operand.MB, 11, 1) == (b"/00RB000B0127\r", 10)


def test_write_frames() -> None:
    # "00SW076C0104F6": 266 + 224 + 97 + 224 = 811 -> 811-768 = 43 = 0x2B
    assert build_command(Operand.MI, 1900, 1, [1270]) == (b"/00SW076C0104F62B\r", 9)
    # "00SB000B011": 48+48+83+66 + 210 + 97 + 49 = 601 -> 89 = 0x59
    assert build_command(Operand.MB, 11, 1, [1]) == (b"/00SB000B01159\r", 9)
    frame, _ = build_command(Operand.ML, 0, 2, [1, 0xDEADBEEF])
    assert frame.startswith(b"/00SNL00000200000001DEADBEEF")


def test_write_value_range_checked() -> None:
    with pytest.raises(ValueError):
        build_command(Operand.MI, 0, 1, [0x10000])
    with pytest.raises(ValueError):
        build_command(Operand.MB, 0, 1, [-1])
    with pytest.raises(ValueError):
        build_command(Operand.MI, 0, 2, [1])


def test_wrap_ethernet_little_endian() -> None:
    frame = b"/00RNL00050B83\r"
    wrapped = wrap_ethernet(frame, 0x0102)
    assert wrapped[:6] == bytes([0x02, 0x01, 101, 0, len(frame), 0])
    assert wrapped[6:] == frame


def _reply(tid: int, plc: bytes) -> bytes:
    """Wrap a PLC reply, replacing a literal "xx" placeholder with the real checksum."""
    if b"xx" in plc:
        plc = plc.replace(b"xx", checksum(plc[2 : plc.index(b"xx")]).encode())
    return wrap_ethernet(plc, tid)


REQ = wrap_ethernet(b"/00RW0000020?\r", 7)


def test_decode_ok_case_insensitive() -> None:
    reply = _reply(7, b"/A00RW04f6FFFFxx\r")
    assert decode_response(reply, REQ, Operand.MI, 2) == [1270, 0xFFFF]


def test_decode_ml() -> None:
    reply = _reply(7, b"/A00RN0001E2400000000Axx\r")
    assert decode_response(reply, REQ, Operand.ML, 2) == [123456, 10]


def test_decode_header_mismatch() -> None:
    reply = _reply(8, b"/A00RW04F6FFFFxx\r")
    with pytest.raises(ProtocolError, match="Transaction header"):
        decode_response(reply, REQ, Operand.MI, 2)


def test_decode_count_mismatch() -> None:
    reply = _reply(7, b"/A00RW04F6xx\r")
    with pytest.raises(ProtocolError, match="data count"):
        decode_response(reply, REQ, Operand.MI, 2)


def test_decode_bad_framing_and_hex() -> None:
    with pytest.raises(ProtocolError):
        decode_response(_reply(7, b"XA00RW04F6FFFFxx\r"), REQ, Operand.MI, 2)
    with pytest.raises(ProtocolError):
        decode_response(_reply(7, b"/A00RW04F6FFFFxx\n"), REQ, Operand.MI, 2)
    with pytest.raises(ProtocolError, match="Non-hex"):
        decode_response(_reply(7, b"/A00RW04G6FFFFxx\r"), REQ, Operand.MI, 2)


@pytest.fixture
async def server():
    srv = await FakePcomServer().start()
    yield srv
    await srv.stop()


async def test_client_roundtrip_and_tids(server: FakePcomServer) -> None:
    server.registers["MI"].update({100: 1, 101: 0xBEEF})
    async with PcomClient("127.0.0.1", server.port) as client:
        assert await client.read(Operand.MI, 100, 2) == [1, 0xBEEF]
        await client.write(Operand.ML, 3, [0x12345678])
        assert await client.read(Operand.ML, 3, 1) == [0x12345678]
    tids = [int.from_bytes(f[:2], "little") for f in server.frames]
    assert tids == [0, 1, 2]


async def test_client_header_mismatch(server: FakePcomServer) -> None:
    server.mangle = "header"
    async with PcomClient("127.0.0.1", server.port) as client:
        with pytest.raises(ProtocolError):
            await client.read(Operand.MI, 0, 1)
        with pytest.raises(ProtocolError):
            await client.write(Operand.MI, 0, [1])


async def test_client_timeout_on_silence(server: FakePcomServer) -> None:
    server.mangle = "silent"
    async with PcomClient("127.0.0.1", server.port, timeout=0.2) as client:
        with pytest.raises(InstrumentTimeout):
            await client.read(Operand.MI, 0, 1)


async def test_client_short_reply_is_protocol_error(server: FakePcomServer) -> None:
    server.mangle = "short"
    async with PcomClient("127.0.0.1", server.port, timeout=0.2) as client:
        with pytest.raises(ProtocolError, match="data count"):
            await client.read(Operand.ML, 5, 11)
        server.mangle = None
        assert await client.read(Operand.ML, 5, 1) == [0]  # reconnects cleanly


async def test_client_connect_refused() -> None:
    srv = await FakePcomServer().start()
    port = srv.port
    await srv.stop()
    with pytest.raises(DriverError):
        await PcomClient("127.0.0.1", port, timeout=0.5).connect()


# Frames captured from the real MP Hub PLC on 2026-10-08.
REAL = [
    (
        Operand.ML,
        11,
        bytes.fromhex("01006500 0f00") + b"/00RNL00050B83\r",
        bytes.fromhex("01006500 6700")
        + b"/A00RN0001492400014D7500041C6A00041C8200000241000001510005"
        + b"97F10000001200000000FFFE79600001E3FEF8\r",
    ),
    (
        Operand.MB,
        1,
        bytes.fromhex("03006500 0e00") + b"/00RB000B0127\r",
        bytes.fromhex("03006500 1000") + b"/A00RB125\r",
    ),
    (
        Operand.MI,
        1,
        bytes.fromhex("04006500 0e00") + b"/00RW076C014A\r",
        bytes.fromhex("04006500 1300") + b"/A00RW04F6E9\r",
    ),
]


@pytest.mark.parametrize(("operand", "count", "request_bytes", "reply"), REAL)
def test_real_plc_frames(operand: Operand, count: int, request_bytes: bytes, reply: bytes) -> None:
    start = {Operand.ML: 5, Operand.MB: 11, Operand.MI: 1900}[operand]
    frame, _ = build_command(operand, start, count)
    assert wrap_ethernet(frame, request_bytes[0]) == request_bytes
    assert len(decode_response(reply, request_bytes, operand, count)) == count


def test_real_plc_values() -> None:
    ml = decode_response(REAL[0][3], REAL[0][2], Operand.ML, 11)
    assert ml[0] == 0x14924 and ml[10] == 0x1E3FE
    assert decode_response(REAL[1][3], REAL[1][2], Operand.MB, 1) == [1]
    assert decode_response(REAL[2][3], REAL[2][2], Operand.MI, 1) == [1270]


def test_real_plc_rejects_bad_checksum() -> None:
    bad = REAL[2][3].replace(b"E9\r", b"E8\r")
    with pytest.raises(ProtocolError, match="checksum"):
        decode_response(bad, REAL[2][2], Operand.MI, 1)
