"""status_now / Log Google Sheet publisher (Apps Script web app mocked).

Headers are checked against the production CurrentData sheet and against the production HeliumRecoveryStatusLog column layout in the .gs file.
"""

import re
from pathlib import Path

import httpx

from heliumrecovery.core.engine import DataEngine, Instrument
from heliumrecovery.core.errors import ErrorHandler, ErrorKind
from heliumrecovery.drivers.atl import parse_varlist
from heliumrecovery.drivers.base import now
from heliumrecovery.drivers.janis import JanisData
from heliumrecovery.drivers.mphub import PumpData, PumpSettings
from heliumrecovery.services.sheets import query_params, snapshot_rows
from heliumrecovery.services.sheets_status import StatusPublisher

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"

# Headers of the production CurrentData tabs (read 2026-10-09).
PROD_HEADERS = {
    "Janis": ["Last Update Time", "LHe Level (in)", "LHe Volume (L)"],
    "ATL1": ["Last Update Time", "Heater Power (%)", "Dewar Pres (psi)", "Diorde T (K)", "Input Gas Flow (Lpm)",
             "Liquid Vol (L)", "LiQ Rate (L/Day)", "Status Machine", "Liquid Mode", "Transfer Mode",
             "Status LEDs", "Dewar Capacity (L)", "Liquid Volume (L)"],
    "MPHUB": ["Last Update Time", "Inlet Pressure (bar)", "Tank Pressure (bar)", "Outlet Pressure (bar)", "Enabled?",
              "Outlet Tank Level (%)", "Oil Temperature (deg C)", "Helium Flow In (Lpm)", "User Data",
              "Inlet Pressure Setpoint (mbar)", "Comp on Setpoint (mbar)", "Comp off Setpoint (mbar)",
              "Max Outlet Pressure (mbar)", "Pressure Setpoint (mbar)", "HiFlow Onset (Lpm)", "BP Valve P Gain",
              "BP Valve I Gain", "BP Valve D Gain"],
}


def engine_with_everything() -> DataEngine:
    e = DataEngine()
    e.update(
        Instrument.PUMP_DATA,
        PumpData(inlet_pressure=1.30243, tank_pressure=1.21396, outlet_pressure=2.67543, outlet_tank_level=33.4,
                 oil_temperature=55.2, helium_flow_in=1.8, last_update=now()),
    )
    e.update(
        Instrument.PUMP_SETTINGS,
        PumpSettings(enabled=True, inlet_pressure_setpoint=1270, comp_on_setpoint=1320, comp_off_setpoint=800,
                     max_outlet_pressure=8000, pressure_setpoint=790, hiflow_onset=100, bp_valve_p_gain=1000,
                     bp_valve_i_gain=20, bp_valve_d_gain=40),
    )
    for i, slot in enumerate((Instrument.ATL1, Instrument.ATL2, Instrument.ATL3)):
        e.update(slot, parse_varlist((FIXTURES / f"atl_real_{81 + i}.xml").read_bytes()))
    e.update(Instrument.JANIS, JanisData(lhe_level_in=-2.09, lhe_volume_l=0.0, last_update=now()))
    return e


def headers_by_tab() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in snapshot_rows(engine_with_everything().read()):
        out.setdefault(r.instr, []).append(f"{r.name} ({r.unit})" if r.unit else r.name)
    return out


def test_headers_match_production_currentdata() -> None:
    got = headers_by_tab()
    for tab, expected in PROD_HEADERS.items():
        assert sorted(got[tab]) == sorted(expected), tab
    assert sorted(got["ATL2"]) == sorted(got["ATL3"]) == sorted(PROD_HEADERS["ATL1"])


def test_values_formatted_for_sheets() -> None:
    values = {(r.instr, r.name): r.value for r in snapshot_rows(engine_with_everything().read())}
    assert values[("MPHUB", "Enabled?")] == "TRUE"
    assert values[("MPHUB", "Inlet Pressure")] == "1.302430"
    assert values[("MPHUB", "User Data")] == "0"
    assert re.fullmatch(r"\d{4}/\d\d/\d\d \d\d:\d\d:\d\d", values[("ATL1", "Last Update Time")])


def _gs() -> str:
    return (ROOT / "apps_script" / "HeliumStatus.gs").read_text(encoding="utf-8")


def _log_header() -> list[str]:
    gs = _gs()
    atl = re.findall(r"'([^']+)'", re.search(r"const ATL_COLS = \[(.*?)\];", gs, re.S).group(1))
    mphub = re.findall(r"'([^']+)'", re.search(r"'Last Update Time', 'Inlet Pressure \(bar\)'.*?\]", gs, re.S).group(0))
    janis = ["Last Update Time", "LHe Level (in)", "LHe Volume (L)"]
    return ([f"Janis_{h}" for h in janis] + [f"ATL{n}_{h}" for n in (1, 2, 3) for h in atl]
            + [f"MPHUB_{h}" for h in mphub])


def test_every_published_value_has_its_production_log_column() -> None:
    log_cols = set(_log_header())
    published = {f"{tab}_{h}" for tab, hs in headers_by_tab().items() for h in hs}
    assert published - log_cols == set()  # nothing lands in an "extra" column
    assert log_cols - published == {"MPHUB_Equiv. Vol (L)"}  # computed by the script


def test_chart_columns_exist_in_log_header() -> None:
    gs = _gs()
    specs = re.search(r"const CHART_SPECS = \[(.*?)\n\];", gs, re.S).group(1)
    groups = re.findall(r"\['[^']*', '[^']*', \[([^\]]*)\]\]", specs)
    cols = {c for grp in groups for c in re.findall(r"'([^']+)'", grp)}
    known = set(_log_header()) | {"Total Volume (L)"}
    assert cols and cols <= known, cols - known


async def test_publisher_status_then_log() -> None:
    seen: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params)
        return httpx.Response(200, json={"ok": True})

    errors = ErrorHandler()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        pub = StatusPublisher("https://script.invalid/exec", "tok", engine_with_everything(), errors, client=http)
        assert await pub.push_status()
        assert await pub.push_log()
    status, log = seen
    assert status["token"] == "tok" and "action" not in status
    assert status["Instr0"] == "Janis" and status["pName0"] == "LHe Level" and status["unit0"] == "in"
    n = len(query_params(snapshot_rows(engine_with_everything().read()))) // 4
    assert f"Instr{n - 1}" in status and f"Instr{n}" not in status
    assert dict(log) == {"token": "tok", "action": "log"}
    assert pub.last_status and pub.last_log and not errors.check(ErrorKind.HTTP_POST)


async def test_publisher_reports_script_errors() -> None:
    replies = iter([
        httpx.Response(200, json={"ok": False, "error": "bad token"}),
        httpx.Response(200, text="<html>Sign in</html>"),  # web app not shared with Anyone
    ])
    errors = ErrorHandler()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: next(replies))) as http:
        pub = StatusPublisher("https://script.invalid/exec", "x", engine_with_everything(), errors, client=http)
        assert not await pub.push_status()
        assert not await pub.push_status()
    assert errors.get(ErrorKind.HTTP_POST).count == 2
    assert "Anyone" in errors.get(ErrorKind.HTTP_POST).message


def test_system_rows_carry_mode_inactive_and_alerts() -> None:
    import json
    from types import SimpleNamespace

    from heliumrecovery.core.modes import ModeController, OperationMode
    from heliumrecovery.services.sheets_status import system_rows

    modes = ModeController(None, inactive=("wei", "atl2"))
    modes.set(OperationMode.TRANSFER)
    rule = lambda key, sev: SimpleNamespace(key=key, device="pump", severity=sev, title=key.title())  # noqa: E731
    alerts = SimpleNamespace(
        active=lambda: [(rule("outlet_low", "warning"), SimpleNamespace(detail="2.25 bar"))],
        pending=lambda: [(rule("pump_offline", "critical"), SimpleNamespace(detail="")),
                         (rule("ignored", "info"), SimpleNamespace(detail=""))],
    )
    rows = {r.name: r for r in system_rows(modes, alerts)}
    assert {r.instr for r in rows.values()} == {"_System"}
    assert rows["Mode"].value == "transfer"
    assert rows["Inactive"].value == "atl2,wei"
    assert json.loads(rows["Alerts"].value) == {"alerts": [
        ["warning", "outlet_low", "pump", "Outlet_Low", "2.25 bar", True],
        ["critical", "pump_offline", "pump", "Pump_Offline", "", False]]}
    assert re.fullmatch(r"\d{4}/\d\d/\d\d \d\d:\d\d:\d\d", rows["Last Update Time"].value)


async def test_publisher_sends_extra_rows_and_survives_their_failure() -> None:
    from heliumrecovery.services.sheets import Row

    seen: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        pub = StatusPublisher("https://script.invalid/exec", "tok", engine_with_everything(), ErrorHandler(), client=http)
        pub.extra_rows = lambda: [Row("_System", "Mode", "", "observer")]
        assert await pub.push_status()
        pub.extra_rows = lambda: 1 / 0
        assert await pub.push_status()
    instrs = [[q[k] for k in q if k.startswith("Instr")] for q in seen]
    assert instrs[0][-1] == "_System"
    assert "_System" not in instrs[1]  # status still sent without the extras
