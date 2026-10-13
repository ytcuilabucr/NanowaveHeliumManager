"""Command line.

``python -m heliumrecovery``          desktop window showing the service's console; same as ``view`` (main.py)
``python -m heliumrecovery serve``    web UI + acquisition: what the Windows service runs
``python -m heliumrecovery desktop``  web UI + acquisition in a desktop window (only when the service is NOT running)
``python -m heliumrecovery run``    headless acquisition with a console summary
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime

from .config import Config
from .core.engine import Instrument, Snapshot
from .core.runtime import Core


def _summary(snap: Snapshot) -> str:
    def age(slot: Instrument) -> str:
        link = snap.link(slot)
        state = "ok" if link.connected else ("ERR" if link.last_error else "--")
        return state

    parts = []
    if (p := snap.get(Instrument.PUMP_DATA)) is not None:
        parts.append(
            f"pump[{age(Instrument.PUMP_DATA)}] in={p.inlet_pressure:.3f} tank={p.tank_pressure:.3f} "
            f"out={p.outlet_pressure:.3f} bar oil={p.oil_temperature:.1f}C flow={p.helium_flow_in:.1f}"
        )
    for slot in (Instrument.ATL1, Instrument.ATL2, Instrument.ATL3):
        if (a := snap.get(slot)) is not None:
            parts.append(f"{slot}[{age(slot)}] {a.diode_temp:.2f}K {a.liquid_volume:.1f}L")
    if (j := snap.get(Instrument.JANIS)) is not None:
        parts.append(f"janis[{age(Instrument.JANIS)}] {j.lhe_level_in:.1f}in {j.lhe_volume_l:.1f}L")
    else:
        parts.append(f"janis[{age(Instrument.JANIS)}]")
    for slot, name in ((Instrument.CMI, "cmi"), (Instrument.WEI, "wei")):
        if (c := snap.get(slot)) is not None:
            parts.append(f"{name}[{age(slot)}] {c.volume_l if c.volume_l is not None else '-'}L")
    return " | ".join(parts)


async def _run(seconds: float | None, every: float) -> Core:
    core = Core(Config.from_env())
    task = asyncio.create_task(core.run())
    start = asyncio.get_running_loop().time()
    try:
        while seconds is None or asyncio.get_running_loop().time() - start < seconds:
            await asyncio.sleep(every)
            print(datetime.now().strftime("%H:%M:%S"), _summary(core.engine.read()), flush=True)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return core


async def _log_atl(folder: str, interval: float | None, seconds: float | None, notion: bool = False) -> None:
    import httpx

    from .core.engine import DataEngine
    from .core.errors import ErrorHandler
    from .core.workers import ATL_SLOTS, ATLPoller
    from .drivers.atl import ATLClient
    from .services.csv_logger import ATLCsvLogger

    cfg = Config.from_env()
    engine, errors = DataEngine(), ErrorHandler()
    logger = ATLCsvLogger(engine, folder)
    async with httpx.AsyncClient() as http:
        clients = {
            slot: ATLClient(host, cfg.atl_port, client=http, auth=cfg.atl_auth)
            for slot, host in zip(ATL_SLOTS, cfg.atl_hosts)
        }
        poller = ATLPoller(clients, engine, errors, interval=interval or cfg.atl_interval)
        tasks = [asyncio.create_task(logger.run()), asyncio.create_task(poller.run())]
        if notion:
            import os

            from .services.notion_sync import ATLNotionSync, NotionClient

            sync = ATLNotionSync(
                NotionClient(os.environ["NOTION_TOKEN"], http),
                folder,
                os.environ["NOTION_ATL_STATUS_DB"],
                os.environ["NOTION_ATL_LOG_DB"],
                page_id=os.environ.get("NOTION_ATL_PAGE"),
            )
            tasks.append(asyncio.create_task(sync.run()))
        try:
            await asyncio.sleep(seconds if seconds is not None else float("inf"))
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    print(f"{logger.rows_written} rows written to {logger.folder}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="heliumrecovery")
    sub = parser.add_subparsers(dest="cmd")  # no command: viewer window
    run = sub.add_parser("run", help="run the acquisition core and print a live summary")
    run.add_argument("--seconds", type=float, default=None, help="stop after N seconds")
    run.add_argument("--every", type=float, default=2.0, help="print interval (s)")
    lg = sub.add_parser("log-atl", help="log ATL readings to daily CSV files")
    lg.add_argument("--dir", required=True, help="output folder (e.g. a Google Drive folder)")
    lg.add_argument("--interval", type=float, default=None, help="poll interval in s (default 10)")
    lg.add_argument("--seconds", type=float, default=None, help="stop after N seconds")
    lg.add_argument(
        "--notion", action="store_true",
        help="also mirror to Notion (needs NOTION_TOKEN, NOTION_ATL_STATUS_DB, NOTION_ATL_LOG_DB)",
    )
    serve = sub.add_parser("serve", help="run the web UI and acquisition core")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--show", action="store_true", help="open a browser")
    vw = sub.add_parser("view", help="desktop window showing the console of the running service (no acquisition)")
    vw.add_argument("--port", type=int, default=None)
    vw.add_argument("--size", default="1600x960", help="window size WxH (default 1600x960)")
    desk = sub.add_parser("desktop", help="acquisition + console in a desktop window (only without the service)")
    desk.add_argument("--port", type=int, default=None)
    desk.add_argument("--size", default="1600x960", help="window size WxH (default 1600x960)")
    args = parser.parse_args(argv)
    if args.cmd is None:
        args = parser.parse_args(["view"])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.cmd == "log-atl":
        asyncio.run(_log_atl(args.dir, args.interval, args.seconds, args.notion))
    elif args.cmd == "serve":
        from .ui.app import serve as serve_ui

        cfg = Config.from_env()
        serve_ui(Core(cfg), args.host or cfg.ui_host, args.port or cfg.ui_port, show=args.show)
    elif args.cmd == "view":
        from .viewer import view

        cfg = Config.from_env()
        w, h = (int(v) for v in args.size.lower().split("x"))
        view(args.port or cfg.ui_port, (w, h))
    elif args.cmd == "desktop":
        from .ui.app import serve as serve_ui

        from .viewer import service_up

        cfg = Config.from_env()
        port = args.port or cfg.ui_port
        if service_up("127.0.0.1", port):
            raise SystemExit(f"Something already serves port {port} (the HeliumRecovery service?). "
                             "Use main.py / 'heliumrecovery view' to show it; two acquisition copies "
                             "would both talk to the pump.")
        w, h = (int(v) for v in args.size.lower().split("x"))
        serve_ui(Core(cfg), cfg.ui_host, port, native=True, window_size=(w, h))
    elif args.cmd == "run":
        core = asyncio.run(_run(args.seconds, args.every))
        for e in core.errors.list():
            if e.count:
                print(f"error {e.kind}: {e.count}x, last: {e.message}")


if __name__ == "__main__":
    main()
