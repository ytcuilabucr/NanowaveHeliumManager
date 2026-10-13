"""Slack alert engine, 10 PM report and forecast (offline; Slack mocked)."""

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from heliumrecovery.core import engine as engine_mod
from heliumrecovery.core.engine import DataEngine, Instrument
from heliumrecovery.core.errors import ErrorHandler
from heliumrecovery.core.trend import Trend
from heliumrecovery.drivers.atl import ATLStatus
from heliumrecovery.drivers.base import now
from heliumrecovery.drivers.janis import JanisData
from heliumrecovery.drivers.mphub import PumpData
from heliumrecovery.services.alerts import AlertConfig, AlertManager
from heliumrecovery.services.forecast import forecast
from heliumrecovery.services.slack import SlackNotifier
from test_services import DEFAULT_PRESETS


def pump(outlet=5.0, inlet=1.3, tank=1.2, oil=60.0) -> PumpData:
    return PumpData(inlet_pressure=inlet, tank_pressure=tank, outlet_pressure=outlet, outlet_tank_level=30,
                    oil_temperature=oil, helium_flow_in=1, last_update=now())


def atl(mode=2, volume=40.0) -> ATLStatus:
    return ATLStatus(diode_temp=4.2, dewar_pressure=1.0, input_gas_flow=1, liquid_vol=volume,
                     liquefaction_rate=20, status_machine=0, liquid_mode=mode, transfer_mode=0, status_leds=0,
                     dewar_capacity=80, liquid_volume=volume, heater_power=10, last_update=now())


class Clock:
    def __init__(self) -> None:
        self.t = time.time()

    def __call__(self) -> float:
        return self.t


CLOCK = Clock()


@pytest.fixture(autouse=True)
def fake_time(monkeypatch):
    """The engine's last_ok timestamps follow the same fake clock as the alert manager."""
    CLOCK.t = time.time()
    monkeypatch.setattr(engine_mod, "now", lambda: datetime.fromtimestamp(CLOCK.t, timezone.utc))


def make(managers=(), archived=("atl2", "wei"), max_per_hour=6, notifier=None, trend=False, state_path=None):
    engine = DataEngine()
    core = SimpleNamespace(
        engine=engine, errors=ErrorHandler(),
        transfer=SimpleNamespace(dialog_open=0, auto_enable_armed=False, armed_since=None),
        pump=SimpleNamespace(last_written=None), atl=SimpleNamespace(requested={}),
        janis=None, valve=None, sheets=None, status_sheet=None,
        config=SimpleNamespace(cryo_stale_after=3600), trend=Trend(engine) if trend else None,
    )
    clock = CLOCK
    m = AlertManager(core, notifier, AlertConfig(managers=managers, archived=frozenset(archived),
                                                 max_per_hour=max_per_hour, state_path=state_path), clock=clock)
    return m, core, clock


def tick(m, clock, seconds):
    """Advance time; instruments that were reading keep reading (re-sent with fresh timestamps)."""
    clock.t += seconds
    snap = m.engine.read()
    for slot in Instrument:
        if snap.link(slot).connected:
            m.engine.update(slot, snap.get(slot))
    m.evaluate()


async def test_outlet_high_debounce_reminder_hysteresis_resolve() -> None:
    m, core, clock = make(managers=("U1",), archived=("atl1", "atl2", "atl3", "wei"))  # ATLs never read here
    core.engine.update(Instrument.PUMP_DATA, pump(outlet=7.6))
    tick(m, clock, 0)
    assert not m.states["outlet_high"].active  # debounce 2 min
    tick(m, clock, 121)
    assert m.states["outlet_high"].active and len(m.queue) == 1 and m.queue[0].mention
    clock.t += 60
    await m.flush()
    assert m.recent[-1][2].startswith("<@U1>") and "Outlet pressure high" in m.recent[-1][2]
    for _ in range(10):  # still high for 50 min: no repeats
        tick(m, clock, 300)
    assert not [o for o in m.queue if "Outlet" in o.text]  # (the never-read ATLs alert meanwhile)
    m.queue.clear()
    tick(m, clock, 700)  # past 1 h -> one reminder
    assert len(m.queue) == 1 and "still" in m.queue[0].text
    m.queue.clear()
    core.engine.update(Instrument.PUMP_DATA, pump(outlet=7.4))  # inside the hysteresis band
    tick(m, clock, 600)
    assert m.states["outlet_high"].active and not m.queue
    core.engine.update(Instrument.PUMP_DATA, pump(outlet=7.2))
    tick(m, clock, 60)
    assert m.states["outlet_high"].active  # clearing is debounced too (2 min)
    tick(m, clock, 121)
    assert not m.states["outlet_high"].active
    assert "resolved" in m.queue[-1].text and not m.queue[-1].mention


async def test_short_spike_never_alerts() -> None:
    m, core, clock = make()
    core.engine.update(Instrument.PUMP_DATA, pump(outlet=7.6))
    tick(m, clock, 0)
    tick(m, clock, 60)
    core.engine.update(Instrument.PUMP_DATA, pump(outlet=6.0))
    tick(m, clock, 10)
    core.engine.update(Instrument.PUMP_DATA, pump(outlet=7.6))
    tick(m, clock, 100)  # true again, but the debounce restarted
    assert not m.states["outlet_high"].active and not m.queue


async def test_bundling_and_hourly_cap() -> None:
    m, core, clock = make(max_per_hour=2)
    for i in range(5):
        m.event(f"e{i}", "pump", "info", f"event {i}")
    await m.flush()
    assert not m.recent  # bundling window not over yet
    clock.t += 61
    await m.flush()
    assert len(m.recent) == 1 and m.recent[0][2].count("event") == 5  # one message
    m.event("x", "pump", "info", "second")
    clock.t += 61
    await m.flush()
    m.event("y", "pump", "info", "third")
    clock.t += 61
    await m.flush()
    assert len(m.recent) == 2 and m.queue  # cap reached: held
    clock.t += 3600
    await m.flush()
    assert len(m.recent) == 3 and "third" in m.recent[-1][2]


async def test_mute_drops_alerts_but_posts_the_notice() -> None:
    m, core, clock = make()
    m.mute(3600)
    m.event("e", "pump", "warning", "something")
    clock.t += 61
    await m.flush()
    statuses = [s for _, s, _ in m.recent]
    assert "muted" in statuses
    assert any("muted until" in text for _, s, text in m.recent if s != "muted")


def test_archived_devices_have_no_rules_or_events() -> None:
    m, core, clock = make()
    keys = {r.key for r in m.rules}
    assert "atl2_offline" not in keys and "wei_stale" not in keys and "atl1_offline" in keys
    m.event("atl2_mode_changed", "atl2", "info", "x")
    assert not m.queue


def test_atl_mode_change_by_hand_vs_by_us() -> None:
    m, core, clock = make()
    core.engine.update(Instrument.ATL1, atl(mode=2))
    tick(m, clock, 0)
    core.engine.update(Instrument.ATL1, atl(mode=4))
    tick(m, clock, 10)
    assert len(m.queue) == 1 and "changed at the unit" in m.queue[0].text
    m.queue.clear()
    core.atl.requested[Instrument.ATL1] = (1, clock.t)
    core.engine.update(Instrument.ATL1, atl(mode=1))
    tick(m, clock, 10)
    assert not m.queue


def test_atl_stopped_only_after_liquefying() -> None:
    m, core, clock = make()
    core.engine.update(Instrument.ATL1, atl(mode=0))  # already stopped at start-up: unknown
    tick(m, clock, 0)
    tick(m, clock, 200)
    assert not m.states["atl1_stopped"].active
    core.engine.update(Instrument.ATL1, atl(mode=3))
    tick(m, clock, 10)
    core.engine.update(Instrument.ATL1, atl(mode=0))
    tick(m, clock, 10)
    tick(m, clock, 121)
    assert m.states["atl1_stopped"].active


def test_pump_settings_changed_on_plc_but_not_our_write() -> None:
    m, core, clock = make()
    core.engine.update(Instrument.PUMP_SETTINGS, DEFAULT_PRESETS[0])
    tick(m, clock, 0)
    ours = DEFAULT_PRESETS[0].model_copy(update={"comp_on_setpoint": 1250})
    core.pump.last_written = ours
    core.engine.update(Instrument.PUMP_SETTINGS, ours)
    tick(m, clock, 10)
    assert not m.queue
    core.engine.update(Instrument.PUMP_SETTINGS, ours.model_copy(update={"comp_on_setpoint": 1200}))
    tick(m, clock, 10)
    assert "changed on the PLC" in m.queue[0].text and "1250" in m.queue[0].text


def test_pump_disabled_long_waits_for_open_dialogs() -> None:
    m, core, clock = make()
    core.engine.update(Instrument.PUMP_SETTINGS, DEFAULT_PRESETS[0].model_copy(update={"enabled": False}))
    core.transfer.dialog_open = 1
    for _ in range(20):
        tick(m, clock, 60)
    assert not m.states["pump_disabled_long"].active
    core.transfer.dialog_open = 0
    tick(m, clock, 60)  # disabled 21 min, but the condition starts counting now
    tick(m, clock, 301)
    assert m.states["pump_disabled_long"].active


def test_state_survives_restart(tmp_path) -> None:
    path = tmp_path / "alerts.json"
    m, core, clock = make(state_path=path)
    core.engine.update(Instrument.PUMP_DATA, pump(outlet=7.6))
    tick(m, clock, 0)
    tick(m, clock, 121)
    assert m.states["outlet_high"].active
    m2, core2, clock2 = make(state_path=path)
    core2.engine.update(Instrument.PUMP_DATA, pump(outlet=7.6))
    tick(m2, clock2, 10)
    assert m2.states["outlet_high"].active and not m2.queue  # not re-sent


async def test_slack_send_and_failure_recorded() -> None:
    bodies = []

    def ok(request):
        bodies.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok)) as http:
        m, core, clock = make(notifier=SlackNotifier("t", "C1", client=http))
        assert await m.send_report()
    assert len(bodies) == 1 and bodies[0].headers["authorization"] == "Bearer t"
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"ok": False, "error": "invalid_auth"}))) as http:
        m, core, clock = make(notifier=SlackNotifier("t", "C1", client=http))
        assert not await m.send_report()
        assert m.errors.list()[2].count == 1  # HTTP_POST


# --- forecast ----------------------------------------------------------------

T0 = datetime(2026, 10, 11, 19, 0, tzinfo=timezone.utc)


def line(slope_h, start=10.0, minutes=180, t0=T0):
    return [(t0 + timedelta(minutes=i), start + slope_h * i / 60) for i in range(minutes)]


def test_forecast_slope_and_crossing() -> None:
    pts = line(-0.5)
    nw = pts[-1][0]
    fc = forecast(pts, nw, nw + timedelta(hours=10), min_step=1, tolerance=0.1)
    assert abs(fc.slope_h + 0.5) < 1e-6 and abs(fc.at_horizon - (fc.value - 5)) < 1e-6
    t = fc.crossing(fc.value - 2, nw, nw + timedelta(hours=10))
    assert abs((t - nw).total_seconds() - 4 * 3600) < 1


def test_forecast_starts_after_a_step_and_after_breaks() -> None:
    pts = line(-0.5)
    pts = pts[:100] + [(t, v + 5) for t, v in pts[100:]]  # transfer at minute 100
    nw = pts[-1][0]
    fc = forecast(pts, nw, nw + timedelta(hours=10), min_step=1, tolerance=0.1)
    assert abs(fc.slope_h + 0.5) < 1e-6 and fc.since == pts[100][0]
    fc = forecast(line(0.2), nw, nw + timedelta(hours=10), min_step=1, tolerance=0.1,
                  breaks=[nw - timedelta(minutes=10)])
    assert fc.slope_h is None and "only" in fc.unclear


def test_forecast_unclear_when_noisy() -> None:
    import random
    rnd = random.Random(1)
    pts = [(t, v + rnd.uniform(-2, 2)) for t, v in line(0.0)]
    nw = pts[-1][0]
    fc = forecast(pts, nw, nw + timedelta(hours=10), min_step=50, tolerance=0.1)
    assert fc.slope_h is None and "unclear" in fc.unclear


def test_report_mentions_managers_when_danger_expected() -> None:
    m, core, clock = make(managers=("U1", "U2"), trend=True)
    start = datetime.fromtimestamp(clock.t, timezone.utc) - timedelta(hours=3)
    for i in range(180):  # Janis falling 1 in/h from 18 in: 15 in within the night
        m.trend.add("janis_level", 18 - i / 60, start + timedelta(minutes=i))
    core.engine.update(Instrument.PUMP_DATA, pump(outlet=5.0))
    core.engine.update(Instrument.JANIS, JanisData(lhe_level_in=15.02, lhe_volume_l=10, last_update=now()))
    text, danger = m.build_report()
    assert danger and text.startswith("⚠️ <@U1> <@U2>")
    assert "ATL2  inactive" in text and "Wei-He3  inactive" in text and "Operation mode: Observer" in text
    assert "reaches ≤ 15 in" in text


def test_report_ok_night_has_no_mention() -> None:
    m, core, clock = make(managers=("U1",), trend=True)
    core.engine.update(Instrument.PUMP_DATA, pump(outlet=5.0))
    text, danger = m.build_report()
    assert not danger and "<@U1>" not in text and "no trend data" not in text.split("Outlet")[0]


def test_switched_off_device_drops_its_alert_and_events() -> None:
    from heliumrecovery.core.modes import ModeController
    m, core, clock = make(archived=())
    core.modes = m.modes = ModeController(inactive=())
    tick(m, clock, 700)  # ATL1 never read -> "not responding"
    assert m.states["atl1_offline"].active
    m.queue.clear()
    core.modes.set_active("atl1", False)
    tick(m, clock, 10)
    assert not m.states["atl1_offline"].active and "atl1_offline" not in {r.key for r in m.rules}
    assert not m.queue  # dropped silently, no "resolved"
    m.event("atl1_mode_changed", "atl1", "info", "x")
    assert not m.queue


def test_transfer_mode_ignores_atl_offline() -> None:
    from heliumrecovery.core.modes import ModeController
    m, core, clock = make(archived=())
    core.modes = m.modes = ModeController(inactive=())
    core.modes.start_transfer()
    tick(m, clock, 700)
    assert not any(m.states[f"atl{i}_offline"].active for i in (1, 2, 3))
    core.modes.end_transfer()
    tick(m, clock, 10)
    assert m.states["atl1_offline"].active


def test_atl80_capacity_is_80_l_whatever_the_unit_reports() -> None:
    m, core, clock = make(archived=("atl2", "atl3", "wei"))
    s = atl(volume=70.0).model_copy(update={"dewar_capacity": 160})  # ATL1 misreports its dewar size
    core.engine.update(Instrument.ATL1, s)
    tick(m, clock, 0)
    tick(m, clock, 601)
    assert m.states["atl1_dewar_full"].active and "70.0 / 80 L (88 %)" in m.states["atl1_dewar_full"].detail
    assert "/ 80 L" in m.build_report()[0]
