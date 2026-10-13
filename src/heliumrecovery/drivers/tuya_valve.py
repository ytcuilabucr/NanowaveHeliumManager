"""NEXGEN smart valve (Tuya / Smart Life, model FK-V02-WIFI-3) over the LAN.

Installed as the inlet valve of ATL3.

Uses tinytuya's local protocol (TCP 6668, protocol 3.3) with the device's
`local_key` from ``devices.json`` (written by ``python -m tinytuya wizard``;
git-ignored). No cloud access is needed at runtime.

Data points seen on the device (2026-10-10):
  DP1   switch_1      bool   valve switch (True = on; assumed to mean open - verify)
  DP9   countdown_1   int s  auto-off countdown (0 = none)
  DP38  relay_status  enum   state after power loss: off / on / last
  DP101, DP102        int    undocumented, both 100 (possibly position %)

Switching (`set_switch`) is refused unless the client is built with
``allow_writes=True`` and always reads the state back afterwards.
tinytuya is blocking, so calls run in a worker thread (`asyncio.to_thread`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .base import DriverError, InstrumentTimeout, ProtocolError, now

# tinytuya error codes that mean "no answer" rather than "bad answer"
TIMEOUT_CODES = {"901", "902", "905"}


class ValveStatus(BaseModel):
    name: str
    switch_on: bool | None  # DP1
    countdown_s: int | None = None  # DP9
    power_on_state: str | None = None  # DP38
    dps: dict[str, Any] = {}  # every data point, as reported
    last_update: datetime

    @property
    def status_number(self) -> Any:
        """DP101, shown as the valve's status number (meaning not documented)."""
        return self.dps.get("101")


def parse_dps(name: str, dps: dict[str, Any]) -> ValveStatus:
    if "1" not in dps:
        raise ProtocolError(f"{name}: no switch data point (DP1) in {dps}")
    return ValveStatus(
        name=name,
        switch_on=bool(dps["1"]),
        countdown_s=dps.get("9"),
        power_on_state=dps.get("38"),
        dps=dict(dps),
        last_update=now(),
    )


class TuyaValve:
    """Local client for one Tuya valve."""

    def __init__(
        self,
        name: str,
        device_id: str,
        ip: str,
        local_key: str,
        *,
        version: float = 3.3,
        timeout: float = 3.0,
        allow_writes: bool = False,
        device_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.name, self.ip, self.allow_writes = name, ip, allow_writes
        if device_factory is None:
            import tinytuya

            device_factory = tinytuya.Device
        self._dev = device_factory(device_id, ip, local_key, version=version)
        self._dev.set_socketTimeout(timeout)
        self._dev.set_socketRetryLimit(1)
        self._lock = asyncio.Lock()  # tinytuya devices are not safe for concurrent use

    @classmethod
    def from_devices_json(cls, path: str | Path, name: str, **kw: Any) -> TuyaValve:
        """Build from the tinytuya wizard's devices.json (keys never leave this process)."""
        try:
            devices = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DriverError(f"{path} not found: run `python -m tinytuya wizard`") from exc
        d = next((x for x in devices if x.get("name") == name), None)
        if d is None:
            raise DriverError(f"no device named {name!r} in {path}")
        if not d.get("ip"):
            raise DriverError(f"{name}: no IP in {path}; rerun the wizard with local polling")
        return cls(name, d["id"], d["ip"], d["key"], version=float(d.get("version") or 3.3), **kw)

    async def read(self) -> ValveStatus:
        async with self._lock:
            reply = await asyncio.to_thread(self._dev.status)
        if not isinstance(reply, dict):
            raise ProtocolError(f"{self.name}: unexpected reply {reply!r}")
        if "Error" in reply:
            code = str(reply.get("Err", ""))
            err = InstrumentTimeout if code in TIMEOUT_CODES else DriverError
            raise err(f"{self.name} ({self.ip}): {reply['Error']} [{code}]")
        return parse_dps(self.name, reply.get("dps", {}))

    async def set_switch(self, on: bool) -> ValveStatus:
        """Set DP1 and return the state read back; raises if it did not take."""
        if not self.allow_writes:
            raise DriverError("valve writes are disabled; set HR_VALVE_ALLOW_WRITES=1")
        async with self._lock:
            reply = await asyncio.to_thread(self._dev.set_status, bool(on), 1)
        if isinstance(reply, dict) and "Error" in reply:
            code = str(reply.get("Err", ""))
            err = InstrumentTimeout if code in TIMEOUT_CODES else DriverError
            raise err(f"{self.name} ({self.ip}): switch failed: {reply['Error']} [{code}]")
        status = await self.read()
        if status.switch_on is not bool(on):
            raise DriverError(f"{self.name}: asked for {'ON' if on else 'OFF'}, read back {status.switch_on}")
        return status
