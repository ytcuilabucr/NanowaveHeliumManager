"""Setting the ATL liquefaction mode (POST /WRITEPI, MW8081) - httpx.MockTransport only."""

from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from heliumrecovery.core.engine import DataEngine, Instrument
from heliumrecovery.core.errors import ErrorHandler, ErrorKind
from heliumrecovery.core.workers import ATLPoller
from heliumrecovery.drivers.atl import ATL_LIQUEFY_ONLY, ATLClient, LiquidMode, liquid_mode_text
from heliumrecovery.drivers.base import DriverError

REAL = (Path(__file__).parent / "fixtures" / "atl_real_81.xml").read_text(encoding="utf-8-sig")


def varlist(mode: int) -> str:
    import re

    return re.sub(r"<liquidmode>\d+</liquidmode>", f"<liquidmode>{mode}</liquidmode>", REAL)


class FakeATL:
    """Unit that applies a posted mode (or ignores it / drops the connection)."""

    def __init__(self, mode: int = 1, apply: bool = True, drop_post: bool = False) -> None:
        self.mode, self.apply, self.drop_post = mode, apply, drop_post
        self.posts: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/WRITEPI"
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.posts.append(form)
            if self.apply:
                self.mode = int(form["VALUE1"])
            if self.drop_post:
                raise httpx.ReadError("connection reset")
            return httpx.Response(200, text="<html>ok</html>")
        assert request.url.path == "/SSI/varlist.xml"
        return httpx.Response(200, text=varlist(self.mode))


def client(fake: FakeATL, allow_writes: bool = True, **kw) -> ATLClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return ATLClient("atl1.test", client=http, allow_writes=allow_writes, retry_delay=0, **kw)


def test_mode_names_follow_unit_firmware() -> None:
    assert [liquid_mode_text(c) for c in range(6)] == ["Stop", "Recovery", "Slow", "Medium", "Fast", "Purge"]
    assert liquid_mode_text(6, 4) == "Transfer medium"
    assert liquid_mode_text(9) == "code 9" and liquid_mode_text(float("nan")) == "—"


async def test_set_mode_posts_like_the_web_page_and_reads_back() -> None:
    fake = FakeATL(mode=1)
    status = await client(fake).set_liquid_mode(LiquidMode.MEDIUM, poll_interval=0)
    assert fake.posts == [{"VALUE1": "3", "ADR1": "MW8081"}]
    assert status.liquid_mode == 3


async def test_set_mode_refused_without_allow_writes() -> None:
    fake = FakeATL()
    with pytest.raises(DriverError, match="HR_ATL_ALLOW_WRITES"):
        await client(fake, allow_writes=False).set_liquid_mode(LiquidMode.FAST)
    assert fake.posts == []


@pytest.mark.parametrize("mode", [LiquidMode.PURGE, LiquidMode.TRANSFER, 42])
async def test_only_stop_and_the_four_liquefaction_modes_can_be_set(mode) -> None:
    fake = FakeATL()
    with pytest.raises(DriverError):
        await client(fake).set_liquid_mode(mode)
    assert fake.posts == []


async def test_mode_that_does_not_take_raises() -> None:
    fake = FakeATL(mode=1, apply=False)
    with pytest.raises(DriverError, match="did not read back as Fast.*Recovery"):
        await client(fake).set_liquid_mode(LiquidMode.FAST, confirm_timeout=0.05, poll_interval=0.01)
    assert len(fake.posts) == 1  # never re-sent


async def test_dropped_connection_is_judged_by_read_back() -> None:
    fake = FakeATL(mode=1, drop_post=True)  # applied, but the reply was lost
    status = await client(fake).set_liquid_mode(LiquidMode.SLOW, poll_interval=0)
    assert status.liquid_mode == 2 and len(fake.posts) == 1


async def test_poller_set_mode_updates_engine_and_records_failure() -> None:
    fake = FakeATL(mode=1)
    engine, errors = DataEngine(), ErrorHandler()
    poller = ATLPoller({Instrument.ATL1: client(fake)}, engine, errors)
    await poller.set_mode(Instrument.ATL1, LiquidMode.FAST)
    assert engine.read().get(Instrument.ATL1).liquid_mode == 4

    fake.apply = False
    poller.clients[Instrument.ATL1].allow_writes = False
    with pytest.raises(DriverError):
        await poller.set_mode(Instrument.ATL1, LiquidMode.SLOW)
    assert errors.get(ErrorKind.HTTP_POST).count == 1


@pytest.mark.parametrize("mode", [LiquidMode.RECOVERY, LiquidMode.MEDIUM, LiquidMode.FAST])
async def test_atl3_model_only_allows_liquefy(mode) -> None:
    fake = FakeATL(mode=1)
    with pytest.raises(DriverError, match="choose Stop, Liquefy"):
        await client(fake, model=ATL_LIQUEFY_ONLY).set_liquid_mode(mode)
    assert fake.posts == []


async def test_atl3_liquefy_is_code_2() -> None:
    fake = FakeATL(mode=0)
    status = await client(fake, model=ATL_LIQUEFY_ONLY).set_liquid_mode(LiquidMode.SLOW, poll_interval=0)
    assert fake.posts == [{"VALUE1": "2", "ADR1": "MW8081"}]
    assert ATL_LIQUEFY_ONLY.mode_text(status.liquid_mode) == "Liquefy"
    assert ATL_LIQUEFY_ONLY.mode_text(0) == "Stop"  # other codes keep their names


def test_slot_models() -> None:
    from heliumrecovery.ui.common import liquid_mode_name

    assert liquid_mode_name(Instrument.ATL1, 2) == "Slow"
    assert liquid_mode_name(Instrument.ATL3, 2) == "Liquefy"


@pytest.mark.parametrize("model", [None, ATL_LIQUEFY_ONLY])
async def test_stop_is_code_0_like_stop_then_ok_on_the_page(model) -> None:
    fake = FakeATL(mode=2)
    kw = {"model": model} if model else {}
    status = await client(fake, **kw).set_liquid_mode(LiquidMode.STOP, poll_interval=0)
    assert fake.posts == [{"VALUE1": "0", "ADR1": "MW8081"}]
    assert status.liquid_mode == 0


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def test_no_start_during_compressor_cooldown_after_our_stop() -> None:
    fake = FakeATL(mode=2)
    poller = ATLPoller({Instrument.ATL1: client(fake)}, DataEngine(), ErrorHandler())
    poller.clock = clock = Clock()
    await poller.set_mode(Instrument.ATL1, LiquidMode.STOP)
    assert poller.cooldown_left(Instrument.ATL1) == 60
    clock.t += 45
    with pytest.raises(DriverError, match="cooling.*wait 15 s"):
        await poller.set_mode(Instrument.ATL1, LiquidMode.SLOW)
    assert fake.posts == [{"VALUE1": "0", "ADR1": "MW8081"}]  # nothing sent
    await poller.set_mode(Instrument.ATL1, LiquidMode.STOP)  # stopping again is always allowed
    clock.t += 60
    await poller.set_mode(Instrument.ATL1, LiquidMode.SLOW)
    assert fake.mode == 2


async def test_stop_seen_in_a_reading_also_starts_the_cooldown() -> None:
    fake = FakeATL(mode=2)
    poller = ATLPoller({Instrument.ATL1: client(fake)}, DataEngine(), ErrorHandler())
    poller.clock = clock = Clock()
    await poller.poll_once()
    assert poller.cooldown_left(Instrument.ATL1) == 0
    fake.mode = 0  # stopped at the unit itself
    await poller.poll_once()
    assert poller.cooldown_left(Instrument.ATL1) == 60
    await poller.poll_once()  # still stopped: the clock does not restart
    clock.t += 30
    assert poller.cooldown_left(Instrument.ATL1) == 30
