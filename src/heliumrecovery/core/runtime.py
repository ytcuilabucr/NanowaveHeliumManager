"""Wires drivers, engine, error handler, loops and services together."""

from __future__ import annotations

import asyncio
import logging
from datetime import time as dtime
from pathlib import Path

import httpx

from ..config import Config
from ..drivers.atl import ATLClient
from ..drivers.janis import JanisClient
from ..drivers.mphub import MPHub
from ..drivers.base import DriverError
from ..drivers.sheet_cryostat import SheetTabClient
from ..drivers.tuya_valve import TuyaValve
from ..services.sheets import SheetsUploader
from ..services.sheets_status import StatusPublisher
from ..services.alerts import INFO, AlertConfig, AlertManager
from ..services.operation_log import OperationLog
from ..services.slack import SlackNotifier
from .engine import DataEngine, Instrument
from .errors import ErrorHandler
from .history import History
from .modes import ModeController
from .trend import Trend
from .transfer import TransferController
from .workers import ATL_MODELS, ATL_SLOTS, ATLPoller, JanisPoller, PumpAction, PumpWorker, SheetCryostatPoller, ValvePoller

log = logging.getLogger(__name__)


class Core:
    """The application core. A UI reads `engine`/`errors` and drives `transfer`."""

    def __init__(self, config: Config) -> None:
        cfg = self.config = config
        self.engine = DataEngine()
        self.errors = ErrorHandler()
        self.history = History(self.engine)
        state_dir = Path(cfg.state_dir)
        # operation mode (header) + instrument activation (Internal tab), kept across restarts
        self.modes = ModeController(state_dir / "modes.json", inactive=cfg.alert_archived)
        self.trend = Trend(self.engine, state_dir / "trend.json")
        self.http = httpx.AsyncClient()

        hub = MPHub(cfg.pump_host, cfg.pump_port, allow_writes=cfg.pump_allow_writes)
        self.pump = PumpWorker(hub, self.engine, self.errors, interval=cfg.pump_interval)
        self.transfer = TransferController(self.pump, self.engine, step2_delay=cfg.step2_delay)

        clients = {
            slot: ATLClient(host, cfg.atl_port, client=self.http, auth=cfg.atl_auth,
                            allow_writes=cfg.atl_allow_writes, model=ATL_MODELS[slot])
            for slot, host in zip(ATL_SLOTS, cfg.atl_hosts)
        }
        self.atl = ATLPoller(clients, self.engine, self.errors, interval=cfg.atl_interval)

        self.janis: JanisPoller | None = None
        if cfg.janis_auth:
            user, password = cfg.janis_auth
            janis = JanisClient(cfg.janis_host, username=user, password=password, client=self.http)
            self.janis = JanisPoller(janis, self.engine, self.errors, interval=cfg.janis_interval)

        self.sheets: SheetsUploader | None = None
        if cfg.sheets_url:
            self.sheets = SheetsUploader(
                cfg.sheets_url, self.engine, self.errors, client=self.http, interval=cfg.sheets_interval
            )

        self.status_sheet: StatusPublisher | None = None
        if cfg.status_sheet_url and cfg.status_sheet_token:
            self.status_sheet = StatusPublisher(
                cfg.status_sheet_url,
                cfg.status_sheet_token,
                self.engine,
                self.errors,
                client=self.http,
                status_interval=cfg.status_interval,
                log_interval=cfg.log_interval,
            )

        self.cryostats: SheetCryostatPoller | None = None
        if cfg.status_sheet_url and cfg.status_sheet_token and cfg.cryo_sheet_id:  # read through the same web app
            tabs = {Instrument.CMI: cfg.cmi_gid, Instrument.WEI: cfg.wei_gid}
            clients = {
                slot: SheetTabClient(cfg.status_sheet_url, cfg.status_sheet_token, cfg.cryo_sheet_id, gid, client=self.http)
                for slot, gid in tabs.items()
            }
            self.cryostats = SheetCryostatPoller(
                clients, self.engine, self.errors, interval=cfg.cryo_interval, stale_after=cfg.cryo_stale_after
            )

        self.valve: ValvePoller | None = None
        self.valve_problem: str | None = None
        try:
            valve = TuyaValve.from_devices_json(
                cfg.valve_devices_json, cfg.valve_name, allow_writes=cfg.valve_allow_writes
            )
            self.valve = ValvePoller(
                valve, self.engine, self.errors, interval=cfg.valve_interval, on_means_open=cfg.valve_on_means_open
            )
        except DriverError as exc:  # no devices.json yet, or valve not found: run without it
            self.valve_problem = str(exc)

        # Transfer Step 3 log -> "Helium Recovery System Operation" (local CSV kept as well)
        self.operation_log = OperationLog(
            cfg.status_sheet_url, cfg.status_sheet_token, state_dir / "transfer_log.csv", client=self.http
        )

        # alerts always run; without a Slack token they are only shown on the Internal tab
        notifier = None
        if cfg.slack_token and cfg.slack_channel:
            notifier = SlackNotifier(cfg.slack_token, cfg.slack_channel, client=self.http)
        self.alerts = AlertManager(self, notifier, AlertConfig(
            managers=cfg.slack_managers,
            archived=frozenset(cfg.alert_archived),
            report_at=dtime.fromisoformat(cfg.report_time),
            horizon=dtime.fromisoformat(cfg.report_horizon),
            state_path=state_dir / "alerts.json",
        ))
        self.modes.on_change = lambda old, new, why: self.alerts.event(
            "mode_changed", "program", INFO, f"Operation mode: {old.title} → {new.title} ({why})", min_gap_s=0)
        for worker in (self.pump, self.atl, self.valve):
            if worker is not None:
                worker.on_write_failed = self.alerts.write_failed

    def features(self) -> dict[str, bool]:
        return {
            "pump_writes": self.config.pump_allow_writes,
            "atl_writes": self.config.atl_allow_writes,
            "janis": self.janis is not None,
            "sheets": self.sheets is not None,
            "status_sheet": self.status_sheet is not None,
            "cryostat_sheet": self.cryostats is not None,
            "valve": self.valve is not None,
            "valve_writes": self.valve is not None and self.config.valve_allow_writes,
            "slack": self.alerts.notifier is not None,
        }

    async def refresh_all(self, timeout: float = 15.0) -> dict[str, str | None]:
        """Read every instrument once, now (Internal tab "Refresh").

        Only reads: the pump gets READ_DATA + READ_SETTINGS through its own queue,
        the pollers do one `poll_once`. Results land in the engine as usual.
        Returns {source: None if it read, else the error}.
        """

        async def pump() -> None:
            for action in (PumpAction.READ_DATA, PumpAction.READ_SETTINGS):
                await self.pump.submit(action)

        jobs = {"pump": pump(), "atl": self.atl.poll_once()}
        for name, poller in (("janis", self.janis), ("cryostats", self.cryostats), ("valve", self.valve)):
            if poller is not None:
                jobs[name] = poller.poll_once()
        results = await asyncio.gather(*(asyncio.wait_for(j, timeout) for j in jobs.values()), return_exceptions=True)
        out = {}
        for name, res in zip(jobs, results):
            if isinstance(res, TimeoutError):
                res = f"no answer within {timeout:g} s"
            out[name] = None if res is None else str(res)
        log.info("manual refresh: %s", out)
        return out

    async def run(self) -> None:
        """Run every loop until cancelled."""
        for name, on in self.features().items():
            log.info("%s: %s", name, "on" if on else "off")
        if self.janis is None:
            self.engine.mark_error(Instrument.JANIS, "no credentials configured")
        if self.valve is None:
            self.engine.mark_error(Instrument.VALVE, self.valve_problem or "not configured")
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.history.run(), name="history")
                tg.create_task(self.trend.run(), name="trend")
                tg.create_task(self.pump.run(), name="pump")
                tg.create_task(self.transfer.run(), name="transfer")
                tg.create_task(self.atl.run(), name="atl")
                for name, svc in (("janis", self.janis), ("sheets", self.sheets), ("status_sheet", self.status_sheet), ("cryostats", self.cryostats), ("valve", self.valve), ("alerts", self.alerts)):
                    if svc is not None:
                        tg.create_task(svc.run(), name=name)
        finally:
            await self.http.aclose()
