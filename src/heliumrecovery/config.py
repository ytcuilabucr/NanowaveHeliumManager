"""Runtime configuration from environment variables (prefix ``HR_``).

Defaults suit the lab installation. Secrets have no defaults: set ``HR_ATL_USER`` / ``HR_ATL_PASSWORD`` and ``JANIS_USERNAME`` /
``JANIS_PASSWORD``, ``HR_SHEETS_URL``, ``SLACK_BOT_TOKEN`` / ``SLACK_CHANNEL``
in the environment or in a git-ignored ``.env`` file next to pyproject.toml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def load_dotenv(path: str = ".env") -> None:
    """Minimal KEY=VALUE loader; real environment variables take precedence."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return tuple(x.strip().lower() if name == "HR_ALERT_ARCHIVED" else x.strip() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Config:
    pump_host: str = ""  # required: HR_PUMP_HOST
    pump_port: int = 502
    pump_interval: float = 0.2  # 200 ms
    pump_allow_writes: bool = False
    atl_hosts: tuple[str, ...] = ()  # required: HR_ATL_HOSTS (ATL1,ATL2,ATL3)
    atl_port: int = 8080
    atl_interval: float = 10.0
    atl_allow_writes: bool = False  # liquefaction mode changes (Table View "Mode selection")
    atl_auth: tuple[str, str] | None = None
    janis_host: str = ""  # required with JANIS_USERNAME: HR_JANIS_HOST
    janis_interval: float = 180.0  # the LHe level changes slowly, 3 min is plenty
    janis_auth: tuple[str, str] | None = field(default=None, repr=False)
    step2_delay: float = 5.0  # Transfer Step 2 countdown, s
    sheets_url: str | None = field(default=None, repr=False)
    sheets_interval: float = 53.0
    status_sheet_url: str | None = field(default=None, repr=False)
    status_sheet_token: str | None = field(default=None, repr=False)
    status_interval: float = 30.0
    log_interval: float = 180.0
    # CMI / Wei Cryostat levels, logged by other systems into a Google Sheet (off when unset)
    cryo_sheet_id: str = ""
    cmi_gid: str = ""
    wei_gid: str = ""
    cryo_interval: float = 180.0
    cryo_stale_after: float = 3600.0
    # Smart Life / Tuya valve, keys from the tinytuya wizard's devices.json (git-ignored)
    valve_devices_json: str = "devices.json"
    valve_name: str = "NEXGEN Valve"
    valve_interval: float = 30.0
    valve_allow_writes: bool = False
    valve_on_means_open: bool = True  # DP1 True = open (assumed; check once in the Smart Life app)
    slack_token: str | None = field(default=None, repr=False)
    slack_channel: str | None = None
    slack_managers: tuple[str, ...] = ()  # Slack user IDs @mentioned on critical alerts / danger forecasts
    # devices switched off at first start (no alerts, greyed out); afterwards the
    # Internal tab toggles are kept in state/modes.json
    alert_archived: tuple[str, ...] = ("atl2", "wei", "janis")
    report_time: str = "22:00"  # nightly Slack report, local time
    report_horizon: str = "08:00"  # the report forecasts until this time next morning
    state_dir: str = "state"  # alert state + 24 h trend, kept across restarts
    ui_host: str = "0.0.0.0"
    ui_port: int = 8080

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv()
        atl_user, janis_user = _env("HR_ATL_USER"), _env("JANIS_USERNAME")
        missing = [name for name, ok in (
            ("HR_PUMP_HOST", _env("HR_PUMP_HOST")),
            ("HR_ATL_HOSTS (three addresses, comma separated)", len(_list("HR_ATL_HOSTS", ())) == 3),
            ("HR_JANIS_HOST (needed with JANIS_USERNAME)", _env("HR_JANIS_HOST") or not janis_user),
        ) if not ok]
        if missing:
            raise ValueError("missing in .env: " + "; ".join(missing))
        return cls(
            pump_host=_env("HR_PUMP_HOST", cls.pump_host),
            pump_interval=_float("HR_PUMP_INTERVAL", cls.pump_interval),
            pump_allow_writes=_env("HR_PUMP_ALLOW_WRITES", "0") == "1",
            atl_hosts=_list("HR_ATL_HOSTS", cls.atl_hosts),
            atl_interval=_float("HR_ATL_INTERVAL", cls.atl_interval),
            atl_allow_writes=_env("HR_ATL_ALLOW_WRITES", "0") == "1",
            atl_auth=(atl_user, _env("HR_ATL_PASSWORD", "")) if atl_user else None,
            janis_host=_env("HR_JANIS_HOST", cls.janis_host),
            janis_interval=_float("HR_JANIS_INTERVAL", cls.janis_interval),
            janis_auth=(janis_user, _env("JANIS_PASSWORD", "")) if janis_user else None,
            step2_delay=_float("HR_STEP2_DELAY", cls.step2_delay),
            sheets_url=_env("HR_SHEETS_URL"),
            sheets_interval=_float("HR_SHEETS_INTERVAL", cls.sheets_interval),
            status_sheet_url=_env("HR_STATUS_SHEET_URL"),
            status_sheet_token=_env("HR_STATUS_SHEET_TOKEN"),
            status_interval=_float("HR_STATUS_INTERVAL", cls.status_interval),
            log_interval=_float("HR_LOG_INTERVAL", cls.log_interval),
            cryo_sheet_id=_env("HR_CRYO_SHEET_ID", cls.cryo_sheet_id),
            cmi_gid=_env("HR_CMI_GID", cls.cmi_gid),
            wei_gid=_env("HR_WEI_GID", cls.wei_gid),
            cryo_interval=_float("HR_CRYO_INTERVAL", cls.cryo_interval),
            cryo_stale_after=_float("HR_CRYO_STALE_AFTER", cls.cryo_stale_after),
            valve_devices_json=_env("HR_VALVE_DEVICES_JSON", cls.valve_devices_json),
            valve_name=_env("HR_VALVE_NAME", cls.valve_name),
            valve_interval=_float("HR_VALVE_INTERVAL", cls.valve_interval),
            valve_allow_writes=_env("HR_VALVE_ALLOW_WRITES", "0") == "1",
            valve_on_means_open=_env("HR_VALVE_ON_MEANS_OPEN", "1") == "1",
            slack_token=_env("SLACK_BOT_TOKEN"),
            slack_channel=_env("SLACK_CHANNEL"),
            slack_managers=_list("HR_SLACK_MANAGERS", ()),
            alert_archived=_list("HR_ALERT_ARCHIVED", cls.alert_archived),
            report_time=_env("HR_REPORT_TIME", cls.report_time),
            report_horizon=_env("HR_REPORT_HORIZON", cls.report_horizon),
            state_dir=_env("HR_STATE_DIR", cls.state_dir),
            ui_host=_env("HR_UI_HOST", cls.ui_host),
            ui_port=int(_env("HR_UI_PORT", str(cls.ui_port))),
        )
