"""JSON API (QueryMPHUBData replacement) without starting the web server."""

import json

from heliumrecovery.config import Config
from heliumrecovery.core.engine import Instrument
from heliumrecovery.core.runtime import Core
from heliumrecovery.drivers.base import now
from heliumrecovery.drivers.mphub import PumpData
from heliumrecovery.ui import app as ui_app


def test_query_mphub_and_snapshot() -> None:
    core = Core(Config(pump_host="127.0.0.1", atl_hosts=("127.0.0.1",) * 3))
    ui_app.core = core
    assert ui_app.api_query_mphub() == {}
    core.engine.update(
        Instrument.PUMP_DATA,
        PumpData(
            inlet_pressure=1.25,
            tank_pressure=0.8,
            outlet_pressure=2.7,
            outlet_tank_level=33.0,
            oil_temperature=55.0,
            helium_flow_in=1.8,
            last_update=now(),
        ),
    )
    mphub = ui_app.api_query_mphub()
    assert list(mphub) == [
        "Inlet Pressure (bar)",
        "Tank Pressure (bar)",
        "Outlet Pressure (bar)",
        "Outlet Tank Level (% Full)",
        "Oil Temperature (C)",
        "Helium Flow In (l/min)",
    ]
    snap = ui_app.api_snapshot()
    json.dumps(snap)  # serialisable
    assert snap["links"]["pump_data"]["connected"] and snap["values"]["atl1"] is None
